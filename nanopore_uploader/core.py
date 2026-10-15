import datetime
import glob
import io
import json
import logging
import math
import os
import re
import subprocess
import time
import warnings

from cryptography.utils import CryptographyDeprecationWarning
warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
import paramiko

from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

import nanopore_uploader.db as db

log = logging.getLogger(__name__)

GRIDION_RUN_ID_REGEX = r"\d{8}_\d{4}_X[1-5]_[A-Z0-9]+_[a-f0-9]{8}"
PROMETHION_RUN_ID_REGEX = r"\d{8}_\d{4}_P2S_[0-9\-]+_[A-Z0-9]+_[a-f0-9]{8}"

def find_runs(config: dict[str, object]) -> list[dict[str, str]]:
    """
    Look through sub-directories of each sequencer's run output directory. Find directories that match
    the run ID format, and are complete (as indicated by the presence of a `final_summary` file.
    
    :param config: Application config
    :type config: dict[str, object]
    :return: List of keys: `["sequencing_run_id", "instrument_type", "instrument_id", "run_directory"]`
    :rtype: list[dict[str, str]]
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
            candidate_run_dir = os.path.abspath(PurePosixPath(candidate_run_dir))
            run_id = os.path.basename(candidate_run_dir)
            matches_gridion_run_id_regex = re.match(GRIDION_RUN_ID_REGEX, run_id)
            matches_promethion_run_id_regex = re.match(PROMETHION_RUN_ID_REGEX, run_id)

            final_summary_files = glob.glob(str(PurePosixPath(candidate_run_dir, 'final_summary*.txt')))
            if len(final_summary_files) > 0:
                final_summary_file = final_summary_files[0]
                final_summary = {}
                with open(final_summary_file, 'r') as f:
                    for line in f:
                        k, v = line.strip().split('=', 1)
                        final_summary[k] = v
            else:
                final_summary_file = None

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

            conditions_met = list(conditions_checked.values())
            if all(conditions_met):
                run_id = os.path.basename(candidate_run_dir)
                instrument_type = sequencer.get('instrument_type', None).upper()
                instrument_id = sequencer.get('instrument_id', None)
                protocol_group_id = final_summary.get('protocol_group_id', None)

                run = {
                    "sequencing_run_id": run_id,
                    "protocol_group_id": protocol_group_id,
                    "instrument_type": instrument_type,
                    "instrument_id": instrument_id,
                    "run_directory": candidate_run_dir,
                }

                runs.append(run)

    return runs


def find_remote_runs(config: dict[str, object]) -> list[dict[str, object]]:
    """
    Check the remote destination server to see which runs have already been uploaded.

    :param config: Application config.
    :type config: dict[str, object]
    :return: List of runs on the remote destination servers. Keys: `["sequencing_run_id", "run_directory", "status"]
    :rtype: list[dict[stry, object]]
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy( paramiko.AutoAddPolicy() )
    remote_runs = []
    for sequencer in config['sequencers']:
        for destination_server in sequencer['destination_servers']:
            server_address = destination_server['server_address']
            log.info({"event_type": "connecting_to_remote_server", "server_address": server_address})
            rsa_key = paramiko.RSAKey.from_private_key_file(destination_server['id_file'])
            username = destination_server['login_id']
            destination_dir = destination_server['destination_dir']
            ssh.connect(server_address, username=username, pkey=rsa_key)
            log.info({"event_type": "connected_to_remote_server", "server_address": server_address})
            list_run_dirs_cmd = [
                'ls',
                '-1d',
                str(PurePosixPath(destination_dir) / '*'),
            ]
            stdin, stdout, stderr = ssh.exec_command(' '.join(list_run_dirs_cmd))
            log.debug({"event_type": "executed_remote_command", "command": ' '.join(list_run_dirs_cmd)})
            candidate_remote_run_dirs = [x.strip() for x in stdout.readlines()]
            log.info({
                "event_type": "found_remote_run_directories", 
                "num_candidate_remote_run_dirs": len(candidate_remote_run_dirs)
            })

            for candidate_remote_run_dir in candidate_remote_run_dirs:
                candidate_run_id = os.path.basename(candidate_remote_run_dir)
                if re.match(GRIDION_RUN_ID_REGEX, candidate_run_id) or re.match(PROMETHION_RUN_ID_REGEX, candidate_run_id):
                    list_upload_complete_file_cmd = [
                        'ls',
                        '-1d',
                        str(PurePosixPath(candidate_remote_run_dir) / 'upload_complete.json')
                    ]
                    stdin, stdout, stderr = ssh.exec_command(' '.join(list_upload_complete_file_cmd))
                    log.debug({"event_type": "executed_remote_command", "command": ' '.join(list_upload_complete_file_cmd)})
                    if len(stdout.readlines()) > 0:
                        upload_status = "UPLOAD_COMPLETE"
                    else:
                        upload_status = "UPLOAD_INCOMPLETE"
                    remote_run = {
                        'sequencing_run_id': candidate_run_id,
                        'run_directory': candidate_remote_run_dir,
                        'status' : upload_status
                    }
                    remote_runs.append(remote_run)

            log.info({
                "event_type": "remote_runs_processed",
                "num_remote_runs": len(remote_runs)
            })

    return remote_runs


def scan(config: dict[str, object]) -> Iterator[Optional[dict[str, object]]]:
    """
    Scan for sequencing runs and update their status based on remote runs.

    :param config: Application config.
    :type config: dict[str, object]
    :param remote_runs: List of remote runs with their status.
    :type remote_runs: list[dict[str, object]]
    :return: Iterator of sequencing runs with updated status.
    :rtype: Iterator[Optional[dict[str, object]]]
    """
    for run in find_runs(config):
        if run['sequencing_run_id'] not in config.get('excluded_runs', []):
            yield run


def check_run_status(config: dict[str, object], run: dict[str, object]):
    """
    
    :param config: Application config.
    :type config: dict[str, object]
    :param run: Sequencing run.
    :type run: dict[str, object]
    :return: Run status
    :rtype: str
    """
    existing_run = db.get_run_by_id(config, run['sequencing_run_id'])
    if existing_run is not None:
        run_status = existing_run['status']
    else:
        run_status = 'QUEUED_FOR_UPLOAD'
        run['status'] = run_status
        db.store_run(config, run)

    return run_status



def upload_run(config: dict[str, object], run: dict[str, object]):
    """
    Upload a run to each destination server.

    :param config: Application config.
    :type config: dict[str, object]
    :param run: Sequencing run
    :type run: dict[str, object]
    :return: None
    :rtype: None
    """
    run_id = run['sequencing_run_id']
    db.update_run_status_by_id(config, run_id, 'UPLOAD_STARTED')
    log.info({"event_type": "upload_started", "sequencing_run_id": run_id})
    for sequencer in config['sequencers']:
        if 'excluded_run_contents' in sequencer:
            excluded_run_contents = list(map(lambda x: '--exclude ' + x, sequencer['excluded_run_contents']))
        else:
            excluded_run_contents = []
        for destination_server in sequencer['destination_servers']:
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
            
            upload_percent_complete = 0.0
            timestamp_upload_start = datetime.datetime.now().isoformat()
            try:
                rsync_process = subprocess.Popen(
                    upload_command_list,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    bufsize=1
                )
                next_milestone = 10.0
                while True:
                    line = rsync_process.stdout.readline()
                    if not line and rsync_process.poll() is not None:
                        break
                    if line:
                        split_line = line.strip().split()
                        if len(split_line) > 1:
                            data_transferred = split_line[0]
                            upload_percent_complete = float(split_line[1].removesuffix('%'))
                            upload_speed = split_line[2]
                            upload_duration = split_line[3]
                            if upload_percent_complete >= next_milestone:
                                log.info({
                                    "event_type": "upload_progress_update",
                                    "sequencing_run_id": run_id,
                                    "data_transferred": data_transferred,
                                    "percent_complete": upload_percent_complete,
                                    "upload_speed": upload_speed,
                                    "upload_duration": upload_duration,
                                })
                                next_milestone = (math.floor(upload_percent_complete / 5) + 1) * 5

                rsync_returncode = rsync_process.returncode
                timestamp_upload_complete = datetime.datetime.now().isoformat()
                log.debug({
                    "event_type": "rsync_returncode_captured",
                    "sequencing_run_id": run_id,
                    "rsync_returncode": rsync_returncode
                })

                # Write an 'upload_complete.json' file on the remote system to indicate
                # to downstream automation systems that the upload is complete and the run
                # is ready for further processing
                if rsync_returncode == 0:
                    upload_complete = {
                        "timestamp_upload_start": timestamp_upload_start,
                        "timestamp_upload_complete": timestamp_upload_complete,
                    }
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy( paramiko.AutoAddPolicy() )
                    rsa_key = paramiko.RSAKey.from_private_key_file(destination_server['id_file'])
                    server_address = destination_server['server_address']
                    username = destination_server['login_id']
                    destination_dir = destination_server['destination_dir']
                    ssh.connect(server_address, username=username, pkey=rsa_key)
                    ftp = ssh.open_sftp()
                    upload_complete_destination_path = os.path.join(destination_server['destination_dir'], run_id, 'upload_complete.json')
                    upload_complete_file_contents = json.dumps(upload_complete, indent=2) + '\n'
                    ftp.putfo(io.BytesIO(upload_complete_file_contents.encode()), upload_complete_destination_path)
                    ftp.close()
                    ssh.close()
                    logging.info(json.dumps({
                        "event_type": "upload_complete_file_written",
                        "sequencing_run_id": run_id,
                        "upload_complete_destination_path": upload_complete_destination_path,
                    }))

                    db.update_run_status_by_id(config, run['sequencing_run_id'], 'UPLOAD_COMPLETE')
                    log.info({"event_type": "upload_complete", "sequencing_run_id": run['sequencing_run_id']})
                    

                # Sometimes the last update from rsync is at ~99% complete.
                # If the last update is >95%, bump it up to 100% complete
                # This should only happen when the rsync process completes normally.
                # We shouldn't bump up the percent complete if the upload didn't actually complete.
                if upload_percent_complete > 95.0 and rsync_returncode == 0:
                    db.update_run_upload_percent_complete_by_id(config, run_id, 100.0)
            except KeyboardInterrupt as e:
                db.update_run_status_by_id(config, run_id, 'UPLOAD_INCOMPLETE')
                raise e
            except subprocess.CalledProcessError as e:
                log.error({"event_type": "error_during_upload", "sequencing_run_id": run_id})
                db.update_run_status_by_id(config, run_id, 'UPLOAD_INCOMPLETE_WITH_ERROR')
                # TODO: Further error handling.
                log.info({"event_type": "upload_incomplete", "sequencing_run_id": run_id})

    
