import datetime
import logging
import os
import queue
import subprocess
import tempfile
import threading

from typing import Callable, Optional

import nanopore_uploader.core as core
import nanopore_uploader.db as db
from nanopore_uploader.config import load_config

DEFAULT_SCAN_INTERVAL_SECONDS = 3600.0

log = logging.getLogger(__name__)


def reload_config(config_path: os.PathLike, current_config: dict[str, object]) -> dict[str, object]:
    """
    Reload the config file, so that changes take effect without restarting.
    If the config can't be loaded, continue with the last valid config.

    :param config_path: Path to the config file.
    :param current_config: The last valid config.
    :return: The reloaded config, or the current config if reloading failed.
    """
    try:
        config = load_config(config_path)
        log.info({"event_type": "config_loaded", "config_file": os.path.abspath(config_path)})
        return config
    except Exception as e:
        log.error({"event_type": "load_config_failed", "config_file": os.path.abspath(config_path), "error": str(e)})
        return current_config


class Uploader:
    """
    The scan/upload loop.

    In headless mode, `run()` is called on the main thread and Ctrl+C (KeyboardInterrupt) stops it.
    In TUI mode, `run()` is called on a worker thread, and the TUI uses `scan_now()`, `requeue()`,
    `exclude()` and `stop()` to control it. Requests from the TUI are queued and carried out
    on the worker thread between runs, so the worker is the only thing that writes to the db.
    """

    def __init__(
        self,
        config_path: os.PathLike,
        config: dict[str, object],
        on_progress: Optional[Callable[[dict[str, object]], None]] = None,
        capture_rsync_stderr: bool = False,
    ):
        """
        :param config_path: Path to the config file (reloaded as the loop runs).
        :param config: The initial config.
        :param on_progress: Called with each rsync progress update (see `core.upload_run`).
        :param capture_rsync_stderr: Log rsync's stderr instead of letting it write to the terminal.
        """
        self.config_path = config_path
        self.config = config
        self.on_progress = on_progress
        self.capture_rsync_stderr = capture_rsync_stderr
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._requests: queue.Queue = queue.Queue()
        self._rsync_process: Optional[subprocess.Popen] = None
        self._rsync_process_lock = threading.Lock()
        self._uploading_run_id: Optional[str] = None

    def run(self):
        """
        Scan for runs and upload them, until `stop()` is called.
        """
        while not self._stop_event.is_set():
            self._wake_event.clear()
            self._handle_requests()
            scan_start_timestamp = datetime.datetime.now()
            log.info({"event_type": "scan_started"})
            for run in core.scan(self.config):
                if self._stop_event.is_set():
                    break
                log.info({
                    "event_type": "found_run",
                    "sequencing_run_id": run['sequencing_run_id'],
                    "instrument_type": run['instrument_type'],
                    "instrument_id": run['instrument_id'],
                    "protocol_group_id": run['protocol_group_id'],
                    "run_directory": run['run_directory'],
                })
                self.config = reload_config(self.config_path, self.config)
                if run['sequencing_run_id'] in self.config.get('excluded_runs', []):
                    continue
                try:
                    run['status'] = core.check_run_status(self.config, run)
                    log.debug({
                        "event_type": "run_status_checked",
                        "sequencing_run_id": run['sequencing_run_id'],
                        "status": run['status'],
                    })
                    if run['status'] == 'QUEUED_FOR_UPLOAD':
                        log.info({
                            "event_type": "upload_starting",
                            "sequencing_run_id": run['sequencing_run_id'],
                        })
                        self._upload_run(run)
                # A problem with one run shouldn't stop other runs from being checked.
                except OSError as e:
                    # Expected, environmental problems (missing files, permissions, network storage).
                    # The message says what went wrong, so a traceback wouldn't add anything.
                    log.error({
                        "event_type": "upload_failed",
                        "sequencing_run_id": run['sequencing_run_id'],
                        "error_type": type(e).__name__, "error": str(e)
                    })
                except Exception as e:
                    # Unexpected problems are probably bugs, so include the traceback.
                    log.error({
                        "event_type": "upload_failed",
                        "sequencing_run_id": run['sequencing_run_id'],
                        "error_type": type(e).__name__,
                        "error": str(e)
                    }, exc_info=True)
                self._handle_requests()

            if self._stop_event.is_set():
                break

            scan_complete_timestamp = datetime.datetime.now()
            scan_duration_seconds = (scan_complete_timestamp - scan_start_timestamp).total_seconds()
            try:
                scan_interval = float(self.config.get('scan_interval_seconds', DEFAULT_SCAN_INTERVAL_SECONDS))
            except (TypeError, ValueError):
                scan_interval = DEFAULT_SCAN_INTERVAL_SECONDS
            next_scan_timestamp = scan_start_timestamp + datetime.timedelta(seconds=scan_interval)
            log.info({
                "event_type": "scan_complete",
                "scan_duration_seconds": scan_duration_seconds,
                "timestamp_next_scan_start": next_scan_timestamp.isoformat()
            })

            # Waiting on an event (rather than sleeping) lets scan_now() and stop() interrupt the wait.
            self._wake_event.wait(max(0, (next_scan_timestamp - datetime.datetime.now()).total_seconds()))

    def scan_now(self):
        """
        Start the next scan now, instead of waiting for the scan interval to elapse.
        If a scan is in progress, another one starts as soon as it's finished.
        """
        self._wake_event.set()

    def requeue(self, run_id: str):
        """
        Set a run's status back to QUEUED_FOR_UPLOAD, so that it's uploaded again, and start a scan.
        """
        self._requests.put(('requeue', run_id))
        self._wake_event.set()

    def exclude(self, run_id: str):
        """
        Add a run to the `excluded_runs_list` file, so that it's skipped from now on.
        """
        self._requests.put(('exclude', run_id))
        self._wake_event.set()

    @property
    def uploading_run_id(self) -> Optional[str]:
        """
        :return: The ID of the run being uploaded, or None if there isn't an upload in progress.
        """
        return self._uploading_run_id

    def can_exclude(self) -> bool:
        """
        :return: Whether an `excluded_runs_list` file is configured.
        """
        return bool(self.config.get('excluded_runs_list'))

    def stop(self):
        """
        Stop the loop. An upload in progress is stopped and the run is marked UPLOAD_INCOMPLETE.
        Safe to call from any thread.
        """
        self._stop_event.set()
        self._wake_event.set()
        with self._rsync_process_lock:
            if self._rsync_process is not None and self._rsync_process.poll() is None:
                self._rsync_process.terminate()

    def _set_rsync_process(self, process: Optional[subprocess.Popen]):
        with self._rsync_process_lock:
            self._rsync_process = process
            # In case stop() was called while rsync was starting.
            if process is not None and self._stop_event.is_set():
                process.terminate()

    def _upload_run(self, run: dict[str, object]):
        rsync_stderr = tempfile.TemporaryFile(mode='w+') if self.capture_rsync_stderr else None
        self._uploading_run_id = run['sequencing_run_id']
        try:
            core.upload_run(
                self.config,
                run,
                on_progress=self.on_progress,
                on_process_started=self._set_rsync_process,
                cancel_event=self._stop_event,
                rsync_stderr=rsync_stderr,
            )
        finally:
            self._set_rsync_process(None)
            self._uploading_run_id = None
            log.debug({"event_type": "upload_run_finished", "sequencing_run_id": run['sequencing_run_id']})
            if rsync_stderr is not None:
                rsync_stderr.seek(0)
                stderr_text = rsync_stderr.read().strip()
                rsync_stderr.close()
                if stderr_text:
                    log.warning({
                        "event_type": "rsync_stderr",
                        "sequencing_run_id": run['sequencing_run_id'],
                        "stderr": stderr_text,
                    })

    def _handle_requests(self):
        while True:
            try:
                request, run_id = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                if request == 'requeue':
                    db.update_run_status_by_id(self.config, run_id, 'QUEUED_FOR_UPLOAD')
                    log.info({"event_type": "run_requeued", "sequencing_run_id": run_id})
                elif request == 'exclude':
                    self._exclude_run(run_id)
            except Exception as e:
                log.error({
                    "event_type": f"{request}_failed",
                    "sequencing_run_id": run_id,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })

    def _exclude_run(self, run_id: str):
        excluded_runs_list = self.config.get('excluded_runs_list')
        if not excluded_runs_list:
            log.warning({
                "event_type": "exclude_failed",
                "sequencing_run_id": run_id,
                "error": "No 'excluded_runs_list' file is configured",
            })
            return
        excluded_runs = self.config.setdefault('excluded_runs', [])
        if run_id not in excluded_runs:
            # Don't join the new run ID onto a last line that has no trailing newline.
            needs_newline = os.path.exists(excluded_runs_list) and os.path.getsize(excluded_runs_list) > 0
            if needs_newline:
                with open(excluded_runs_list, 'rb') as f:
                    f.seek(-1, os.SEEK_END)
                    needs_newline = f.read(1) != b'\n'
            with open(excluded_runs_list, 'a') as f:
                f.write(('\n' if needs_newline else '') + run_id + '\n')
            excluded_runs.append(run_id)
        log.info({"event_type": "run_excluded", "sequencing_run_id": run_id, "excluded_runs_list": excluded_runs_list})
