import datetime
import logging
import os
import queue
import subprocess
import tempfile
import threading

from dataclasses import dataclass, field
from typing import Callable, Optional

import paramiko

import nanopore_uploader.core as core
from nanopore_uploader.config import load_config

DEFAULT_SCAN_INTERVAL_SECONDS = 3600.0
DEFAULT_MAX_UPLOAD_ATTEMPTS = 3

# upload_now() result: uploaded wherever it could be checked, but at least one destination couldn't be.
SOME_DESTINATIONS_UNCHECKED = 'SOME_DESTINATIONS_UNCHECKED'

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


@dataclass
class UploadRequest:
    """
    An upload someone has asked for.
    """
    # Upload again even where the run's already uploaded (rewriting upload_complete.json).
    force: bool = False
    # Destinations (core.destination_key) already uploaded to for this request.
    attempted: set = field(default_factory=set)


class Uploader:
    """
    The scan/upload loop.

    Nothing about a run's state is stored: on each scan, each complete run is checked against its
    sequencer's destination servers (see `core.list_remote_runs`). A run needs uploading to the destinations
    where it's ABSENT or PARTIAL. A destination that can't be checked (UNKNOWN) is left alone.

    In monitor mode (the default), scans only report which runs need uploading: runs are uploaded when
    someone asks (`upload()`, or `upload_now()` from the command line). In auto mode (`auto_upload: true` in
    the config, or `set_auto_upload()`), runs that need uploading are uploaded by themselves. A failed
    automatic upload is tried again on later scans, up to `max_upload_attempts` uploads per run (default 3)
    since the uploader started; after that it's left alone until someone asks for it to be uploaded.

    In headless mode, `run()` is called on the main thread and Ctrl+C (KeyboardInterrupt) stops it.
    In TUI mode, `run()` is called on a worker thread, and the TUI uses `scan_now()`, `upload()`,
    `exclude()`, `set_auto_upload()` and `stop()` to control it. Requests from the TUI are queued and
    carried out on the worker thread between runs.
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
        # Uploads people have asked for: {run_id: UploadRequest}.
        self._upload_requests: dict[str, UploadRequest] = {}
        # Set for this session with set_auto_upload(); otherwise `auto_upload` in the config.
        self._auto_upload_override: Optional[bool] = None

    def run(self):
        """
        Scan for runs and upload them, until `stop()` is called.
        """
        log.info({"event_type": "upload_mode", "auto_upload": self.auto_upload})
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
        # In monitor mode: runs that need uploading, waiting for someone to ask.
        awaiting_upload = []
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
            # The run list was built before the config was reloaded: use the sequencer's current config,
            # so that changes to its destinations (or excluded_run_contents) apply straight away.
            sequencer = current_sequencer(self.config, run)
            if sequencer is None:
                log.warning({
                    "event_type": "run_skipped",
                    "sequencing_run_id": run_id,
                    "reason": "Its sequencer is no longer in the config",
                })
                continue
            run['sequencer'] = sequencer
            try:
                destination_states = self._check_run(run, remote_runs_by_destination)
                state = core.run_state(destination_states)
                if run_id in self._upload_requests:
                    self._handle_upload_request(run, destination_states)
                elif self.auto_upload:
                    to_upload = self._destinations_to_upload(run, destination_states, force=False)
                    if to_upload and self._should_upload(run_id, state):
                        self._start_upload(run, to_upload)
                elif state in (core.ABSENT, core.PARTIAL):
                    awaiting_upload.append(run_id)
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

        if awaiting_upload:
            log.info({
                "event_type": "runs_awaiting_upload",
                "num_runs": len(awaiting_upload),
                "sequencing_run_ids": awaiting_upload,
                "note": "Monitor mode: these runs aren't uploaded until someone asks (or auto_upload is turned on)",
            })

    def _handle_upload_request(self, run: dict[str, object], destination_states: dict[str, str]):
        """
        Carry out a requested upload, as far as the run's destinations can be checked. The request is kept
        until every destination has been dealt with: destinations that can't be checked yet are uploaded to
        on a later scan, once they can be. Each destination is only uploaded to once per request.
        """
        run_id = run['sequencing_run_id']
        request = self._upload_requests[run_id]
        destinations = run['sequencer']['destination_servers']
        to_upload = [
            d for d in self._destinations_to_upload(run, destination_states, request.force)
            if core.destination_key(d) not in request.attempted
        ]
        if to_upload:
            request.attempted.update(core.destination_key(d) for d in to_upload)
            self._start_upload(run, to_upload)
        waiting = [
            label for d, label in zip(destinations, core.destination_labels(destinations))
            if destination_states[label] == core.UNKNOWN and core.destination_key(d) not in request.attempted
        ]
        if waiting:
            log.info({"event_type": "upload_request_waiting", "sequencing_run_id": run_id, "server_ids": waiting})
            return
        del self._upload_requests[run_id]
        if not request.attempted:
            log.info({"event_type": "upload_not_needed", "sequencing_run_id": run_id})

    def _check_run(self, run: dict[str, object], cache: dict[tuple, Optional[dict[str, str]]]) -> dict[str, str]:
        """
        :return: The run's state on each of its sequencer's destinations: {label: core.UPLOADED etc.}, keyed by
                 label (see `core.destination_labels`), which is unique per destination (unlike server_id).
        """
        destinations = run['sequencer']['destination_servers']
        destination_states = {}
        for destination_server, label in zip(destinations, core.destination_labels(destinations)):
            remote_runs = self._remote_runs(destination_server, cache)
            if remote_runs is None:
                destination_states[label] = core.UNKNOWN
            else:
                destination_states[label] = remote_runs.get(run['sequencing_run_id'], core.ABSENT)
        log.debug({
            "event_type": "run_checked",
            "sequencing_run_id": run['sequencing_run_id'],
            "state": core.run_state(destination_states),
            "destinations": destination_states,
        })
        return destination_states

    @staticmethod
    def _destinations_to_upload(run: dict[str, object], destination_states: dict[str, str], force: bool) -> list[dict[str, object]]:
        """
        :param force: Upload again even where the run's already uploaded.
        :return: The destinations to upload the run to: where it's ABSENT or PARTIAL (or, with force,
                 every destination that can be checked).
        """
        if force:
            wanted = (core.ABSENT, core.PARTIAL, core.UPLOADED)
        else:
            wanted = (core.ABSENT, core.PARTIAL)
        destinations = run['sequencer']['destination_servers']
        return [d for d, label in zip(destinations, core.destination_labels(destinations)) if destination_states[label] in wanted]

    def _start_upload(self, run: dict[str, object], destinations: list[dict[str, object]]) -> str:
        log.info({
            "event_type": "upload_starting",
            "sequencing_run_id": run['sequencing_run_id'],
            "server_ids": [core.destination_label(d, run['sequencer']['destination_servers']) for d in destinations],
        })
        return self._upload_run(run, destinations)

    def upload_now(self, run_id: str, force: bool = False) -> str:
        """
        Upload one run straight away, on this thread (for the command line). Doesn't need `run()`.

        :param run_id: The run to upload.
        :param force: Upload again even where it's already uploaded.
        :return: UPLOAD_COMPLETE, UPLOAD_INCOMPLETE_WITH_ERROR or UPLOAD_INCOMPLETE (see `core.upload_run`),
                 ALREADY_UPLOADED, UNKNOWN if its destinations couldn't be checked, or SOME_DESTINATIONS_UNCHECKED
                 if it was uploaded to every destination that could be checked, but at least one couldn't be.
        :raises LookupError: If there's no complete run with that ID.
        :raises ValueError: If the run is in the excluded runs list.
        """
        run = next((r for r in core.find_runs(self.config) if r['sequencing_run_id'] == run_id), None)
        if run is None:
            raise LookupError(f"No complete run called {run_id} was found in the sequencers' run output directories")
        if run_id in self.config.get('excluded_runs', []):
            raise ValueError(f"{run_id} is in the excluded runs list ({self.config.get('excluded_runs_list')})")
        destination_states = self._check_run(run, {})
        to_upload = self._destinations_to_upload(run, destination_states, force)
        if not to_upload:
            return 'ALREADY_UPLOADED' if core.run_state(destination_states) == core.UPLOADED else core.UNKNOWN
        result = self._start_upload(run, to_upload)
        if result == 'UPLOAD_COMPLETE' and core.UNKNOWN in destination_states.values():
            return SOME_DESTINATIONS_UNCHECKED
        return result

    def _remote_runs(self, destination_server: dict[str, object], cache: dict[tuple, Optional[dict[str, str]]]) -> Optional[dict[str, str]]:
        """
        :return: The runs on a destination (see `core.list_remote_runs`), or None if it couldn't be checked.
        """
        key = core.destination_key(destination_server)
        if key not in cache:
            try:
                cache[key] = core.list_remote_runs(destination_server)
            except (core.RemoteListingError, OSError, paramiko.SSHException) as e:
                log.error({
                    "event_type": "remote_listing_failed",
                    "server_id": server_id(destination_server),
                    "destination_dir": destination_server.get('destination_dir'),
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

    def upload(self, run_id: str, force: bool = False):
        """
        Upload a run on the next scan (which starts now, or as soon as the current one finishes), in either mode.
        This also gives it a fresh set of attempts, if it's used them up in auto mode.

        :param force: Upload again even where it's already uploaded (rewriting upload_complete.json).
        """
        self._requests.put(('reupload' if force else 'upload', run_id))
        self._wake_event.set()

    @property
    def auto_upload(self) -> bool:
        """
        :return: Whether runs are uploaded automatically (auto mode), rather than only when someone asks (monitor mode).
        """
        if self._auto_upload_override is not None:
            return self._auto_upload_override
        value = self.config.get('auto_upload', False)
        if isinstance(value, str):
            return value.strip().lower() in ('true', 'yes', '1')
        return bool(value)

    def set_auto_upload(self, enabled: bool):
        """
        Switch between auto and monitor mode, for this session (the config sets the mode at startup).
        Switching to auto mode starts a scan.
        """
        self._auto_upload_override = enabled
        log.info({"event_type": "upload_mode", "auto_upload": enabled})
        if enabled:
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

    def _upload_run(self, run: dict[str, object], destinations: list[dict[str, object]]) -> str:
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
            return status
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
                if request in ('upload', 'reupload'):
                    self._forget_attempts(run_id)
                    force = request == 'reupload'
                    # A new request starts afresh, but doesn't undo an earlier forced one.
                    earlier = self._upload_requests.get(run_id)
                    self._upload_requests[run_id] = UploadRequest(force=force or (earlier is not None and earlier.force))
                    log.info({"event_type": "upload_requested", "sequencing_run_id": run_id, "force": force})
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


def current_sequencer(config: dict[str, object], run: dict[str, object]) -> Optional[dict[str, object]]:
    """
    :return: The config for the sequencer a run was found under (matched by its run output directory),
             from the given config, or None if it's no longer there.
    """
    run_output_dir = os.path.abspath(run['sequencer']['run_output_dir'])
    for sequencer in config.get('sequencers', []):
        if os.path.abspath(sequencer.get('run_output_dir', '')) == run_output_dir:
            return sequencer
    return None
