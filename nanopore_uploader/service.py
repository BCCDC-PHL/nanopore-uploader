import datetime
import logging
import os
import queue
import subprocess
import tempfile
import threading

from typing import Callable, Optional

import paramiko

import nanopore_uploader.core as core
from nanopore_uploader.config import load_config

DEFAULT_SCAN_INTERVAL_SECONDS = 3600.0
DEFAULT_MAX_UPLOAD_ATTEMPTS = 3

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

    Nothing about a run's state is stored: on each scan, each complete run is checked against its
    sequencer's destination servers (see `core.list_remote_runs`). A run is uploaded to the destinations
    where it's ABSENT or PARTIAL. A destination that can't be checked (UNKNOWN) is left alone.

    In headless mode, `run()` is called on the main thread and Ctrl+C (KeyboardInterrupt) stops it.
    In TUI mode, `run()` is called on a worker thread, and the TUI uses `scan_now()`, `requeue()`,
    `exclude()` and `stop()` to control it. Requests from the TUI are queued and carried out
    on the worker thread between runs.

    A run whose upload fails is left PARTIAL (or ABSENT), so it's tried again on later scans, up to
    `max_upload_attempts` uploads per run (default 3) since the uploader started. After that it's left
    alone until it's re-queued, or the uploader is restarted.
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
        # Uploads started for each run since the uploader started, reset when a run is uploaded or re-queued.
        self._upload_attempts: dict[str, int] = {}
        self._retries_exhausted_logged: set[str] = set()
        # Runs re-queued to be uploaded again, even if they're already uploaded.
        self._forced_uploads: set[str] = set()

    def run(self):
        """
        Scan for runs and upload them, until `stop()` is called.
        """
        while not self._stop_event.is_set():
            self._wake_event.clear()
            self._handle_requests()
            scan_start_timestamp = datetime.datetime.now()
            log.info({"event_type": "scan_started"})
            try:
                self._scan_and_upload()
            except Exception as e:
                # Problems with individual runs are handled in _scan_and_upload, so this is probably a bug,
                # or a problem with the config. Keep going: the next scan may work (eg. after the config is fixed).
                log.error({"event_type": "scan_failed", "error_type": type(e).__name__, "error": str(e)}, exc_info=True)

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

        # Don't lose a request (eg. an exclusion) made just before stopping.
        self._handle_requests()

    def _scan_and_upload(self):
        # Each destination is listed at most once per scan, the first time a run needs checking against it.
        remote_runs_by_destination: dict[tuple, Optional[dict[str, str]]] = {}
        for run in core.scan(self.config):
            if self._stop_event.is_set():
                break
            run_id = run['sequencing_run_id']
            log.info({
                "event_type": "found_run",
                "sequencing_run_id": run_id,
                "instrument_type": run['instrument_type'],
                "instrument_id": run['instrument_id'],
                "protocol_group_id": run['protocol_group_id'],
                "run_directory": run['run_directory'],
            })
            self.config = reload_config(self.config_path, self.config)
            if run_id in self.config.get('excluded_runs', []):
                continue
            try:
                destinations = run['sequencer']['destination_servers']
                destination_states = {}
                for destination_server in destinations:
                    remote_runs = self._remote_runs(destination_server, remote_runs_by_destination)
                    if remote_runs is None:
                        state = core.UNKNOWN
                    else:
                        state = remote_runs.get(run_id, core.ABSENT)
                    destination_states[server_id(destination_server)] = state
                state = core.run_state(destination_states)
                log.debug({
                    "event_type": "run_checked",
                    "sequencing_run_id": run_id,
                    "state": state,
                    "destinations": destination_states,
                })

                if run_id in self._forced_uploads:
                    # Re-queued: upload again to every destination that can be reached, even if it's already there.
                    to_upload = [d for d in destinations if destination_states[server_id(d)] != core.UNKNOWN]
                    should_upload = bool(to_upload)
                else:
                    to_upload = [d for d in destinations if destination_states[server_id(d)] in (core.ABSENT, core.PARTIAL)]
                    should_upload = bool(to_upload) and self._should_upload(run_id, state)

                if should_upload:
                    self._forced_uploads.discard(run_id)
                    log.info({
                        "event_type": "upload_starting",
                        "sequencing_run_id": run_id,
                        "server_ids": [server_id(d) for d in to_upload],
                    })
                    self._upload_run(run, to_upload)
            # A problem with one run shouldn't stop other runs from being checked.
            except OSError as e:
                # Expected, environmental problems (missing files, permissions, network storage).
                # The message says what went wrong, so a traceback wouldn't add anything.
                log.error({
                    "event_type": "upload_failed",
                    "sequencing_run_id": run_id,
                    "error_type": type(e).__name__, "error": str(e)
                })
            except Exception as e:
                # Unexpected problems are probably bugs, so include the traceback.
                log.error({
                    "event_type": "upload_failed",
                    "sequencing_run_id": run_id,
                    "error_type": type(e).__name__,
                    "error": str(e)
                }, exc_info=True)
            self._handle_requests()

    def _remote_runs(self, destination_server: dict[str, object], cache: dict[tuple, Optional[dict[str, str]]]) -> Optional[dict[str, str]]:
        """
        :return: The runs on a destination (see `core.list_remote_runs`), or None if it couldn't be checked.
        """
        key = (destination_server.get('server_address'), destination_server.get('login_id'), destination_server.get('destination_dir'))
        if key not in cache:
            try:
                cache[key] = core.list_remote_runs(destination_server)
            except (core.RemoteListingError, OSError, paramiko.SSHException) as e:
                log.error({
                    "event_type": "remote_listing_failed",
                    "server_id": server_id(destination_server),
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                cache[key] = None
        return cache[key]

    def _max_upload_attempts(self) -> int:
        try:
            return max(1, int(self.config.get('max_upload_attempts', DEFAULT_MAX_UPLOAD_ATTEMPTS)))
        except (TypeError, ValueError):
            return DEFAULT_MAX_UPLOAD_ATTEMPTS

    def _should_upload(self, run_id: str, state: str) -> bool:
        """
        Decide whether to upload a run that isn't finished on its destinations, given how many times
        it's been tried since the uploader started.
        """
        attempts = self._upload_attempts.get(run_id, 0)
        if attempts == 0:
            return True
        max_attempts = self._max_upload_attempts()
        if attempts < max_attempts:
            log.info({
                "event_type": "upload_retrying",
                "sequencing_run_id": run_id,
                "previous_state": state,
                "attempt": attempts + 1,
                "max_upload_attempts": max_attempts,
            })
            return True
        if run_id not in self._retries_exhausted_logged:
            self._retries_exhausted_logged.add(run_id)
            log.warning({
                "event_type": "upload_retries_exhausted",
                "sequencing_run_id": run_id,
                "state": state,
                "attempts": attempts,
            })
        return False

    def scan_now(self):
        """
        Start the next scan now, instead of waiting for the scan interval to elapse.
        If a scan is in progress, another one starts as soon as it's finished.
        """
        self._wake_event.set()

    def requeue(self, run_id: str):
        """
        Upload a run again on the next scan (which starts now), even if it's already uploaded or has used up its attempts.
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
        Stop the loop. An upload in progress is stopped (leaving the run PARTIAL, to be resumed later).
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

    def _upload_run(self, run: dict[str, object], destinations: list[dict[str, object]]):
        run_id = run['sequencing_run_id']
        rsync_stderr = tempfile.TemporaryFile(mode='w+') if self.capture_rsync_stderr else None
        self._uploading_run_id = run_id
        self._upload_attempts[run_id] = self._upload_attempts.get(run_id, 0) + 1
        try:
            status = core.upload_run(
                self.config,
                run,
                destinations=destinations,
                on_progress=self.on_progress,
                on_process_started=self._set_rsync_process,
                cancel_event=self._stop_event,
                rsync_stderr=rsync_stderr,
            )
            if status == 'UPLOAD_COMPLETE':
                self._forget_attempts(run_id)
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

    def _forget_attempts(self, run_id: str):
        self._upload_attempts.pop(run_id, None)
        self._retries_exhausted_logged.discard(run_id)

    def _handle_requests(self):
        while True:
            try:
                request, run_id = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                if request == 'requeue':
                    self._forget_attempts(run_id)
                    self._forced_uploads.add(run_id)
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


def server_id(destination_server: dict[str, object]) -> str:
    """
    :return: The name used for a destination server in logs and the TUI.
    """
    return destination_server.get('server_id') or destination_server.get('server_address')
