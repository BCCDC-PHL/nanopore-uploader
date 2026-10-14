#!/usr/bin/env python

import argparse
import datetime
import json
import logging
import os
import time

import nanopore_uploader.core as core
from nanopore_uploader.config import load_config
from nanopore_uploader.logging_config import configure_logging

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', required=True)
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('--log-file', help='Append logs to this file, rotated daily at midnight (default: log to stdout)')
    parser.add_argument('--log-retention-days', type=int, default=90, help='Number of rotated log files to keep. 0 keeps all of them (default: 90)')
    parser.add_argument('--log-format', default='json', choices=['json', 'text'])
    args = parser.parse_args()

    configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format)
    log.debug({"event_type": "debug_logging_enabled"})

    config = load_config(args.config)

    try:
        while(True):
            scan_start_timestamp = datetime.datetime.now()
            remote_runs = core.find_remote_runs(config)
            for run in core.scan(config, remote_runs):
                log.info({
                    "event_type": "found_run",
                    "sequencing_run_id": run['sequencing_run_id'],
                })
                config = reload_config(args.config, config)
                try:
                    log.info({
                        "event_type": "upload_starting",
                        "sequencing_run_id": run['sequencing_run_id'],
                    })
                    core.upload_run(config, run)
                    log.info({
                        "event_type": "upload_completed",
                        "sequencing_run_id": run['sequencing_run_id'],
                    })
                # A problem with one run shouldn't stop other runs from being checked.
                # No 'qc_check_complete.json' is written, so the run will be retried on the next scan.
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
            scan_complete_timestamp = datetime.datetime.now()
            scan_duration_delta = scan_complete_timestamp - scan_start_timestamp
            scan_duration_seconds = scan_duration_delta.total_seconds()
            next_scan_timestamp = scan_start_timestamp + datetime.timedelta(seconds=config['scan_interval_seconds'])
            log.info({
                "event_type": "scan_complete", 
                "scan_duration_seconds": scan_duration_seconds, 
                "timestamp_next_scan_start": next_scan_timestamp.isoformat()
            })

            time.sleep(max(0, (next_scan_timestamp - datetime.datetime.now()).total_seconds()))
    except KeyboardInterrupt:
        log.info({"event_type": "quit"})
    except Exception as e:
        log.error({"event_type": "unexpected_error", "error": str(e)})

if __name__ == '__main__':
    main()
