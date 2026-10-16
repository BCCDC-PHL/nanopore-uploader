import datetime
import glob
import io
import json
import logging
import math
import os
import re
import shlex
import subprocess
import threading
import time
import warnings

from cryptography.utils import CryptographyDeprecationWarning
warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
import paramiko

from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional

import nanopore_uploader.history as history

log = logging.getLogger(__name__)

GRIDION_RUN_ID_REGEX = r"\d{8}_\d{4}_X[1-5]_[A-Z0-9]+_[a-f0-9]{8}"
PROMETHION_RUN_ID_REGEX = r"\d{8}_\d{4}_P2S_[0-9\-]+_[A-Z0-9]+_[a-f0-9]{8}"

UPLOAD_COMPLETE_FILENAME = 'upload_complete.json'

# Where a run is at on a destination server. Worked out on every scan from the destination itself:
# `upload_complete.json` there is the record of a finished upload (it's what downstream automation looks for).
UPLOADED = 'UPLOADED'   # <destination_dir>/<run_id>/upload_complete.json exists
PARTIAL = 'PARTIAL'     # <destination_dir>/<run_id>/ exists, but not upload_complete.json (an interrupted or failed upload)
ABSENT = 'ABSENT'       # <destination_dir>/<run_id>/ doesn't exist
UNKNOWN = 'UNKNOWN'     # The destination couldn't be checked (eg. unreachable). Nothing is uploaded to it.

SSH_CONNECT_TIMEOUT_SECONDS = 30

def parse_final_summary(final_summary_file: os.PathLike) -> dict[str, str]:
    """
    Parse a MinKNOW `final_summary` file of `key=value` lines. Lines without an `=` (eg. blank lines) are skipped.

    :param final_summary_file: Path to the final summary file.
    :return: The summary's keys and values.
    """
    final_summary = {}
    with open(final_summary_file, 'r') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                final_summary[k] = v
    return final_summary


def check_candidate_run_dir(sequencer: dict[str, object], candidate_run_dir: str) -> Optional[dict[str, str]]:
    """
    Check whether a directory is a complete sequencing run.

    :param sequencer: Config for the sequencer whose output directory the candidate was found in.
    :param candidate_run_dir: Directory to check.
    :return: The run (keys: `["sequencing_run_id", "protocol_group_id", "instrument_type", "instrument_id", "run_directory", "sequencer"]`),
             or None if it isn't a complete run. `sequencer` is the sequencer's config, which says where its runs are uploaded to.
    """
    candidate_run_dir = os.path.abspath(PurePosixPath(candidate_run_dir))
    run_id = os.path.basename(candidate_run_dir)
    matches_gridion_run_id_regex = re.match(GRIDION_RUN_ID_REGEX, run_id)
    matches_promethion_run_id_regex = re.match(PROMETHION_RUN_ID_REGEX, run_id)

    final_summary_files = glob.glob(str(PurePosixPath(candidate_run_dir, 'final_summary*.txt')))
    if len(final_summary_files) > 0:
        final_summary_file = final_summary_files[0]
        final_summary = parse_final_summary(final_summary_file)
    else:
        final_summary_file = None
        final_summary = {}

    conditions_checked = {
        "is_directory": os.path.isdir(candidate_run_dir),
        "matches_run_id_regex": matches_gridion_run_id_regex or matches_promethion_run_id_regex,
        "run_complete": final_summary_file is not None and os.path.exists(final_summary_file),
    }
    log.debug({
        "event_type": "conditions_checked",
        "sequencing_run_id": run_id,
        "conditions_checked": conditions_checked
    })

    if not all(conditions_checked.values()):
        return None

    return {
        "sequencing_run_id": run_id,
        "protocol_group_id": final_summary.get('protocol_group_id', None),
        "instrument_type": sequencer.get('instrument_type', None).upper(),
        "instrument_id": sequencer.get('instrument_id', None),
        "run_directory": candidate_run_dir,
        "sequencer": sequencer,
    }


def find_runs(config: dict[str, object]) -> list[dict[str, str]]:
    """
    Look through sub-directories of each sequencer's run output directory. Find directories that match
    the run ID format, and are complete (as indicated by the presence of a `final_summary` file.

    A directory that can't be checked (eg. an unreadable `final_summary` file) is logged and skipped,
    so that it doesn't stop other runs from being found.

    :param config: Application config
    :type config: dict[str, object]
    :return: List of runs (see `check_candidate_run_dir`).
    :rtype: list[dict[str, object]]
    """
    runs = []
    for sequencer in config['sequencers']:
        candidate_run_dirs = glob.glob(str(PurePosixPath(sequencer['run_output_dir'], '*', '*', '*')))
        log.info({
            "event_type": "candidate_run_dirs_found",
            "sequencer": sequencer['run_output_dir'],
            "num_candidate_run_dirs": len(candidate_run_dirs),
        })
        for candidate_run_dir in candidate_run_dirs:
            try:
                run = check_candidate_run_dir(sequencer, candidate_run_dir)
            except OSError as e:
                # Expected, environmental problems (permissions, network storage), so no traceback.
                log.error({
                    "event_type": "check_run_dir_failed",
                    "run_directory": candidate_run_dir,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                continue
            except Exception as e:
                log.error({
                    "event_type": "check_run_dir_failed",
                    "run_directory": candidate_run_dir,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }, exc_info=True)
                continue
            if run is not None:
                runs.append(run)

    return runs


class RemoteListingError(Exception):
    """
    The runs on a destination server couldn't be listed.
    """


def connect(destination_server: dict[str, object]) -> paramiko.SSHClient:
    """
    Open an SSH connection to a destination server. The caller must close it.

    :param destination_server: Config for the destination server.
    :return: The connected client.
    """
    ssh = paramiko.SSHClient()
    try:
        ssh.set_missing_host_key_policy( paramiko.AutoAddPolicy() )
        rsa_key = paramiko.RSAKey.from_private_key_file(destination_server['id_file'])
        ssh.connect(
            destination_server['server_address'],
            username=destination_server['login_id'],
            pkey=rsa_key,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        )
    except BaseException:
        ssh.close()
        raise
    return ssh


def list_remote_runs_command(destination_dir: str) -> str:
    """
    The shell command that lists a destination's run directories and `upload_complete.json` files.

    It prints one path per line, relative to the destination dir: `./<run_id>` for each directory directly
    inside it, and `./<run_id>/upload_complete.json` for each completed upload. One command (rather than one
    per run) keeps a scan to a single round trip, however many runs the destination has.
    """
    return (
        f"cd {shlex.quote(destination_dir)} && "
        f"find . -mindepth 1 -maxdepth 2 "
        f"\\( -path './*/*' -name {UPLOAD_COMPLETE_FILENAME} -o ! -path './*/*' -type d \\)"
    )


def parse_remote_runs_listing(listing: str) -> dict[str, str]:
    """
    Parse the output of `list_remote_runs_command`.

    :param listing: The command's output.
    :return: {run_id: UPLOADED or PARTIAL} for each directory that looks like a run (others are ignored).
    """
    remote_runs = {}
    for line in listing.splitlines():
        parts = line.strip().removeprefix('./').split('/')
        run_id = parts[0]
        if not (re.fullmatch(GRIDION_RUN_ID_REGEX, run_id) or re.fullmatch(PROMETHION_RUN_ID_REGEX, run_id)):
            continue
        if len(parts) == 1:
            remote_runs.setdefault(run_id, PARTIAL)
        elif parts[1:] == [UPLOAD_COMPLETE_FILENAME]:
            remote_runs[run_id] = UPLOADED
    return remote_runs


def list_remote_runs(destination_server: dict[str, object]) -> dict[str, str]:
    """
    Find which runs are on a destination server, and which of them finished uploading.

    :param destination_server: Config for the destination server.
    :return: {run_id: UPLOADED or PARTIAL}. Runs that aren't there are ABSENT.
    :raises RemoteListingError: If the listing command fails (eg. the destination dir is missing or unreadable).
        Then the destination's state is unknown, and nothing should be uploaded to it.
    :raises OSError, paramiko.SSHException: If the server can't be reached.
    """
    command = list_remote_runs_command(destination_server['destination_dir'])
    ssh = connect(destination_server)
    try:
        stdin, stdout, stderr = ssh.exec_command(command)
        listing = stdout.read().decode(errors='replace')
        errors = stderr.read().decode(errors='replace').strip()
        exit_status = stdout.channel.recv_exit_status()
    finally:
        ssh.close()
    if exit_status != 0:
        raise RemoteListingError(errors or f"listing {destination_server['destination_dir']} failed with exit status {exit_status}")
    remote_runs = parse_remote_runs_listing(listing)
    log.info({
        "event_type": "remote_runs_listed",
        "server_id": destination_server.get('server_id'),
        "num_remote_runs": len(remote_runs),
        "num_uploaded": sum(1 for state in remote_runs.values() if state == UPLOADED),
    })
    return remote_runs


def run_state(destination_states: dict[str, str]) -> str:
    """
    Combine a run's state on each of its destinations into one state for the run.

    :param destination_states: {server_id: UPLOADED, PARTIAL, ABSENT or UNKNOWN}
    :return: PARTIAL or ABSENT if the run still needs uploading somewhere (PARTIAL if any destination has
             part of it), UPLOADED if it's finished on every destination, otherwise UNKNOWN.
    """
    states = set(destination_states.values())
    if PARTIAL in states:
        return PARTIAL
    if ABSENT in states:
        return ABSENT
    if states == {UPLOADED}:
        return UPLOADED
    return UNKNOWN


def scan(config: dict[str, object]) -> Iterator[dict[str, object]]:
    """
    Find complete sequencing runs, leaving out excluded runs.

    :param config: Application config.
    :type config: dict[str, object]
    :return: Iterator of sequencing runs (see `check_candidate_run_dir`).
    :rtype: Iterator[dict[str, object]]
    """
    for run in find_runs(config):
        if run['sequencing_run_id'] not in config.get('excluded_runs', []):
            yield run


# Exit codes from the rsync man page that are most likely to come up, to make errors easier to act on.
RSYNC_EXIT_CODE_DESCRIPTIONS = {
    1: "syntax or usage error",
    2: "protocol incompatibility",
    3: "errors selecting input/output files or directories",
    5: "error starting client-server protocol",
    10: "error in socket I/O",
    11: "error in file I/O",
    12: "error in rsync protocol data stream",
    20: "received SIGUSR1 or SIGINT",
    23: "partial transfer due to error",
    24: "partial transfer due to vanished source files",
    30: "timeout in data send/receive",
    35: "timeout waiting for daemon connection",
    255: "SSH connection failed",
}


class UploadError(Exception):
    """
    Uploading a run to a destination server failed.
    """


def describe_rsync_returncode(returncode: int) -> str:
    """
    :param returncode: rsync's exit code (negative if it was killed by a signal).
    :return: A description of the exit code, eg. 'rsync exited with code 23 (partial transfer due to error)'.
    """
    if returncode < 0:
        return f"rsync was killed by signal {-returncode}"
    description = RSYNC_EXIT_CODE_DESCRIPTIONS.get(returncode)
    if description is None:
        return f"rsync exited with code {returncode}"
    return f"rsync exited with code {returncode} ({description})"


def rsync_to_destination(
        run: dict[str, object],
        sequencer: dict[str, object],
        destination_server: dict[str, object],
        on_progress: Optional[Callable[[dict[str, object]], None]] = None,
        on_process_started: Optional[Callable[[subprocess.Popen], None]] = None,
        rsync_stderr=None,
) -> int:
    """
    Copy a run to one destination server with rsync.

    :param run: Sequencing run.
    :param sequencer: Config for the sequencer (for `excluded_run_contents`).
    :param destination_server: Config for the destination server.
    :param on_progress: See `upload_run`.
    :param on_process_started: See `upload_run`.
    :param rsync_stderr: See `upload_run`.
    :return: rsync's exit code.
    """
    run_id = run['sequencing_run_id']
    permissions_strings = []
    for key, value in destination_server['permissions'].items():
        permissions_strings.append(key + '=' + value)
    permissions_string = '--chmod=' + ','.join(permissions_strings)
    log.debug({"event_type": "permissions_string_generated", "permissions_string": permissions_string})

    upload_command_list = [
        'rsync',
        '-arth',
        '--info=progress2',
        '--no-inc-recursive',
        # '-e', '"ssh -i ' + destination_server['id_file'] + '"',
        *[arg for pattern in sequencer.get('excluded_run_contents', []) for arg in ('--exclude', pattern)],
        permissions_string,
        str(Path(run['run_directory'])),
        ''.join([
            destination_server['login_id'],
            '@',
            destination_server['server_address'],
            ':',
            destination_server['destination_dir'],
        ])
    ]

    upload_command = ' '.join(upload_command_list)
    logging.debug({
        "event_type": "upload_command_generated",
        "sequencing_run_id": run_id,
        "upload_command": upload_command
    })

    rsync_process = subprocess.Popen(
        upload_command_list,
        stdout=subprocess.PIPE,
        stderr=rsync_stderr,
        text=True,
        bufsize=1
    )
    try:
        if on_process_started is not None:
            on_process_started(rsync_process)
        next_milestone = 10.0
        while True:
            line = rsync_process.stdout.readline()
            if not line and rsync_process.poll() is not None:
                break
            # --info=progress2 lines look like: '1.23G  45%  48.20MB/s    0:00:25'
            # Ignore anything else, rather than letting an unexpected line stop the upload.
            split_line = line.strip().split()
            if len(split_line) < 4 or not split_line[1].endswith('%'):
                continue
            try:
                upload_percent_complete = float(split_line[1].removesuffix('%'))
            except ValueError:
                continue
            data_transferred = split_line[0]
            upload_speed = split_line[2]
            upload_duration = split_line[3]
            if on_progress is not None:
                on_progress({
                    "sequencing_run_id": run_id,
                    "server_id": destination_server.get('server_id'),
                    "data_transferred": data_transferred,
                    "percent_complete": upload_percent_complete,
                    "upload_speed": upload_speed,
                    "upload_duration": upload_duration,
                })
            if upload_percent_complete >= next_milestone:
                log.info({
                    "event_type": "upload_progress_update",
                    "sequencing_run_id": run_id,
                    "server_id": destination_server.get('server_id'),
                    "data_transferred": data_transferred,
                    "percent_complete": upload_percent_complete,
                    "upload_speed": upload_speed,
                    "upload_duration": upload_duration,
                })
                next_milestone = (math.floor(upload_percent_complete / 5) + 1) * 5
    finally:
        # If something went wrong while reading rsync's output, don't leave it running with nobody reading it.
        if rsync_process.poll() is None:
            rsync_process.terminate()
            rsync_process.wait()

    log.debug({
        "event_type": "rsync_returncode_captured",
        "sequencing_run_id": run_id,
        "server_id": destination_server.get('server_id'),
        "rsync_returncode": rsync_process.returncode
    })
    return rsync_process.returncode


def write_upload_complete_file(destination_server: dict[str, object], run_id: str, timestamp_upload_start: str, timestamp_upload_complete: str):
    """
    Write an 'upload_complete.json' file on the remote system to indicate to downstream automation systems
    that the upload is complete and the run is ready for further processing.

    :param destination_server: Config for the destination server.
    :param run_id: Sequencing run ID.
    :param timestamp_upload_start: When the upload to this server started (ISO format).
    :param timestamp_upload_complete: When the upload to this server finished (ISO format).
    """
    upload_complete = {
        "timestamp_upload_start": timestamp_upload_start,
        "timestamp_upload_complete": timestamp_upload_complete,
    }
    ssh = connect(destination_server)
    try:
        ftp = ssh.open_sftp()
        try:
            upload_complete_destination_path = os.path.join(destination_server['destination_dir'], run_id, UPLOAD_COMPLETE_FILENAME)
            upload_complete_file_contents = json.dumps(upload_complete, indent=2) + '\n'
            ftp.putfo(io.BytesIO(upload_complete_file_contents.encode()), upload_complete_destination_path)
        finally:
            ftp.close()
    finally:
        ssh.close()
    logging.info(json.dumps({
        "event_type": "upload_complete_file_written",
        "sequencing_run_id": run_id,
        "upload_complete_destination_path": upload_complete_destination_path,
    }))


def upload_run(
        config: dict[str, object],
        run: dict[str, object],
        destinations: Optional[list[dict[str, object]]] = None,
        on_progress: Optional[Callable[[dict[str, object]], None]] = None,
        on_process_started: Optional[Callable[[subprocess.Popen], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        rsync_stderr=None,
) -> str:
    """
    Upload a run to destination servers, writing an `upload_complete.json` file on each once it's copied.

    If one destination fails, the others are still attempted. Each attempt is recorded in the upload history
    file, if one is configured.

    :param config: Application config.
    :type config: dict[str, object]
    :param run: Sequencing run (see `check_candidate_run_dir`).
    :type run: dict[str, object]
    :param destinations: Destination servers to upload to. Default: all of the run's sequencer's destinations.
    :param on_progress: Called with every rsync progress update (not just the logged milestones).
    :param on_process_started: Called with each rsync process when it starts, so that it can be stopped from another thread.
    :param cancel_event: If this is set when rsync exits, the upload was stopped deliberately: stop without trying other destinations.
    :param rsync_stderr: Where rsync's stderr goes (passed to `subprocess.Popen`). Default: inherited from this process.
    :return: UPLOAD_COMPLETE (every destination succeeded), UPLOAD_INCOMPLETE (cancelled)
             or UPLOAD_INCOMPLETE_WITH_ERROR (at least one destination failed).
    :rtype: str
    """
    run_id = run['sequencing_run_id']
    sequencer = run['sequencer']
    if destinations is None:
        destinations = sequencer['destination_servers']
    log.info({"event_type": "upload_started", "sequencing_run_id": run_id})
    destination_errors = []

    def record(server_id: str, timestamp_upload_start: str, result: str, detail: str = ''):
        history.record_upload(config, {
            'timestamp_start': timestamp_upload_start,
            'timestamp_end': datetime.datetime.now().isoformat(),
            'sequencing_run_id': run_id,
            'instrument_id': run.get('instrument_id'),
            'server_id': server_id,
            'result': result,
            'detail': detail,
        })

    for destination_server in destinations:
        server_id = destination_server.get('server_id') or destination_server.get('server_address')
        timestamp_upload_start = datetime.datetime.now().isoformat()
        try:
            rsync_returncode = rsync_to_destination(
                run, sequencer, destination_server,
                on_progress=on_progress,
                on_process_started=on_process_started,
                rsync_stderr=rsync_stderr,
            )
            timestamp_upload_complete = datetime.datetime.now().isoformat()

            if cancel_event is not None and cancel_event.is_set():
                record(server_id, timestamp_upload_start, history.CANCELLED)
                log.info({"event_type": "upload_cancelled", "sequencing_run_id": run_id})
                return 'UPLOAD_INCOMPLETE'

            if rsync_returncode != 0:
                raise UploadError(describe_rsync_returncode(rsync_returncode))

            write_upload_complete_file(destination_server, run_id, timestamp_upload_start, timestamp_upload_complete)
            record(server_id, timestamp_upload_start, history.COMPLETE)
            log.info({"event_type": "destination_upload_complete", "sequencing_run_id": run_id, "server_id": server_id})
        except KeyboardInterrupt:
            record(server_id, timestamp_upload_start, history.CANCELLED)
            raise
        # A problem with one destination shouldn't stop the run being copied to the others.
        except (UploadError, OSError, paramiko.SSHException) as e:
            # Expected problems (rsync errors, network, permissions, SSH), so no traceback.
            destination_errors.append(f"{server_id}: {e}")
            record(server_id, timestamp_upload_start, history.FAILED, str(e))
            log.error({
                "event_type": "destination_upload_failed",
                "sequencing_run_id": run_id,
                "server_id": server_id,
                "error_type": type(e).__name__,
                "error": str(e),
            })
        except Exception as e:
            destination_errors.append(f"{server_id}: {type(e).__name__}: {e}")
            record(server_id, timestamp_upload_start, history.FAILED, f"{type(e).__name__}: {e}")
            log.error({
                "event_type": "destination_upload_failed",
                "sequencing_run_id": run_id,
                "server_id": server_id,
                "error_type": type(e).__name__,
                "error": str(e),
            }, exc_info=True)

    if destination_errors:
        log.error({
            "event_type": "upload_failed",
            "sequencing_run_id": run_id,
            "error_type": "UploadError",
            "error": "; ".join(destination_errors),
        })
        return 'UPLOAD_INCOMPLETE_WITH_ERROR'

    log.info({"event_type": "upload_complete", "sequencing_run_id": run_id})
    return 'UPLOAD_COMPLETE'
