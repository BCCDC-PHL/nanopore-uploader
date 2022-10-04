import logging
import glob
import io
import json
import os
import re

import paramiko

from typing import Iterator, Optional

import nanopore_uploader.db as db


GRIDION_RUN_ID_REGEX = "\d{8}_\d{4}_X[1-5]_[A-Z0-9]+_[a-f0-9]{8}"

def find_runs(config: dict[str, object]) -> list[dict[str, str]]:
    """
    """
    runs = []
    for sequencer in config['sequencers']:
        candidate_run_dirs = glob.glob(os.path.abspath(os.path.join(sequencer['run_output_dir'], '*', '*', '*')))
        for candidate_run_dir in candidate_run_dirs:
            matches_gridion_run_id_regex = re.match(GRIDION_RUN_ID_REGEX, os.path.basename(candidate_run_dir))

            final_summary_files = glob.glob(os.path.abspath(os.path.join(candidate_run_dir, 'final_summary*.txt')))
            if len(final_summary_files) > 0:
                final_summary_file = final_summary_files[0]
            else:
                final_summary_file = None

            conditions_checked = {
                "is_directory": os.path.isdir(candidate_run_dir),
                "matches_gridion_run_id_regex": matches_gridion_run_id_regex is not None,
                "run_complete": final_summary_file is not None and os.path.exists(final_summary_file),
            }

            conditions_met = list(conditions_checked.values())
            if all(conditions_met):
                run_id = os.path.basename(candidate_run_dir)
                instrument_type = sequencer.get('instrument_type', None).upper()
                instrument_id = sequencer.get('instrument_id', None)
                run = {
                    "sequencing_run_id": run_id,
                    "instrument_type": instrument_type,
                    "instrument_id": instrument_id,
                    "run_directory": candidate_run_dir,
                }
                runs.append(run)

    return runs


def find_remote_runs(config: dict[str, object]):
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy( paramiko.AutoAddPolicy() )
    remote_runs = []
    for sequencer in config['sequencers']:
        for destination_server in sequencer['destination_servers']:
            rsa_key = paramiko.RSAKey.from_private_key_file(destination_server['identity_file'])
            server_address = destination_server['server_address']
            username = destination_server['username']
            destination_dir = destination_server['destination_dir']
            ssh.connect(server_address, username=username, pkey=rsa_key)
            list_run_dirs_cmd = [
                'ls',
                '-1d',
                os.path.join(destination_dir, '*')
            ]
            stdin, stdout, stderr = ssh.exec_command(' '.join(list_run_dirs_cmd))
            candidate_remote_run_dirs = [x.strip() for x in stdout.readlines()]
            print(json.dumps(candidate_remote_run_dirs, indent=2))
            for candidate_remote_run_dir in candidate_remote_run_dirs:
                candidate_run_id = os.path.basename(candidate_remote_run_dir)
                if re.match(GRIDION_RUN_ID_REGEX, candidate_run_id):
                    list_upload_complete_file_cmd = [
                        'ls',
                        '-1d',
                        os.path.join(candidate_remote_run_dir, 'upload_complete.json')
                    ]
                    stdin, stdout, stderr = ssh.exec_command(' '.join(list_upload_complete_file_cmd))
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
            print(json.dumps(remote_runs, indent=2))
            exit()
            
                


def scan(config: dict[str, object]) -> Iterator[Optional[dict[str, object]]]:
    """
    """
    logging.info(json.dumps({"event_type": "scan_start"}))
    for run in find_runs(config):
        yield run


def check_run_status(config: dict[str, object], run: dict[str, object]):
    """
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
    db.update_run_status_by_id(config, run['sequencing_run_id'], 'UPLOAD_STARTED')
    
    upload_command = [
        'rsync',
        '-artvh',
        '-p',
        '-e', 'ssh',
    ]

    db.update_run_status_by_id(config, run['sequencing_run_id'], 'UPLOAD_COMPLETE')
