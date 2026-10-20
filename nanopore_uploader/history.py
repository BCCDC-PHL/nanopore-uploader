"""
An append-only record of uploads, for lab staff to look through (eg. in a spreadsheet).

It's only a record: nothing reads it to decide what to upload, so editing or deleting it is harmless.
Whether a run has been uploaded comes from the destination (its `upload_complete.json`).
"""
import csv
import logging
import os

log = logging.getLogger(__name__)

FIELDNAMES = [
    'timestamp_start',
    'timestamp_end',
    'sequencing_run_id',
    'instrument_id',
    'server_id',
    'result',
    'detail',
]

COMPLETE = 'complete'
FAILED = 'failed'
CANCELLED = 'cancelled'


def record_upload(config: dict[str, object], row: dict[str, object]):
    """
    Append a row to the upload history file (`upload_history_file` in the config), if one is configured.
    The header is written when the file is new. Problems writing it are logged, but never stop an upload.

    :param config: Application config.
    :param row: Values for `FIELDNAMES`. Missing ones are left blank.
    """
    history_file = config.get('upload_history_file')
    if not history_file:
        return
    try:
        is_new = not os.path.exists(history_file) or os.path.getsize(history_file) == 0
        with open(history_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        log.error({
            "event_type": "upload_history_write_failed",
            "upload_history_file": history_file,
            "sequencing_run_id": row.get('sequencing_run_id'),
            "error_type": type(e).__name__,
            "error": str(e),
        })
