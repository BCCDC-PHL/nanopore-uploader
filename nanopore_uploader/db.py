import csv
import datetime
import json
import logging
import os
import stat
import tempfile

from nanopore_uploader.model import *

log = logging.getLogger(__name__)

FIELDNAMES = ['sequencing_run_id', 'status']


def get_db_file(config: dict[str, object]) -> str:
    """
    :param config: Application config.
    :return: Path to the upload db file (`db.file` in the config).
    :raises ValueError: If the config doesn't set `db.file`.
    """
    db_file = config.get('db', {}).get('file')
    if not db_file:
        raise ValueError("No upload db file is set in the config ('db': {'file': ...})")
    return db_file


def check_db_file(config: dict[str, object]):
    """
    Check that the upload db file exists.

    It isn't created automatically: without it every completed run looks new, so a mistyped path or an
    unmounted network share would mean uploading every run on the sequencer again.

    :param config: Application config.
    :raises ValueError: If the config doesn't set `db.file`.
    :raises FileNotFoundError: If the db file doesn't exist.
    """
    db_file = get_db_file(config)
    if not os.path.isfile(db_file):
        raise FileNotFoundError(
            f"Upload db file not found: {os.path.abspath(db_file)}. "
            "If this is a new installation, create it with: nanopore-uploader -c <config> --init-db"
        )


def init_db_file(config: dict[str, object]) -> str:
    """
    Create an empty upload db file. Its directory must already exist.

    :param config: Application config.
    :return: Path to the new db file.
    :raises FileExistsError: If the db file already exists (it's never overwritten).
    :raises FileNotFoundError: If the db file's directory doesn't exist.
    """
    db_file = get_db_file(config)
    # 'x' mode fails if the file exists, so an existing db can't be overwritten.
    with open(db_file, 'x', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
        writer.writeheader()
    log.info({"event_type": "db_file_created", "db_file": os.path.abspath(db_file)})
    return db_file


def _read_rows(db_file: str) -> list[dict[str, str]]:
    with open(db_file, newline='') as csvfile:
        return list(csv.DictReader(csvfile))


def _write_rows(db_file: str, rows: list[dict[str, str]]):
    """
    Replace the db file's contents atomically: write a temporary file in the same directory, then rename
    it over the db. If anything goes wrong part way through, the db is left as it was, and anyone reading
    it sees either the old contents or the new, never a partly-written file.
    """
    directory = os.path.dirname(os.path.abspath(db_file))
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(db_file)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
            csvfile.flush()
            os.fsync(csvfile.fileno())
        # mkstemp creates the file readable only by its owner; keep the db's permissions.
        try:
            os.chmod(temp_path, stat.S_IMODE(os.stat(db_file).st_mode))
        except FileNotFoundError:
            pass
        os.replace(temp_path, db_file)
    except BaseException:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def store_run(config: dict[str, object], run: dict[str, object]):
    """
    Stores a sequencing run in the database if it does not already exist.

    Args:
        config (dict[str, object]): The configuration dictionary containing database file information.
        run (dict[str, object]): The sequencing run data to store.
    """

    sequencing_run_id = run['sequencing_run_id']
    existing_run = get_run_by_id(config, sequencing_run_id)

    if existing_run is None:
        add_run_to_db(config, run)
        log.debug({"event_type": "run_stored", "run_id": sequencing_run_id})
    else:
        log.debug({"event_type": "run_exists", "run_id": sequencing_run_id})


def get_run_by_id(config: dict[str, object], run_id: str):
    for row in _read_rows(get_db_file(config)):
        if row['sequencing_run_id'] == run_id:
            return row
    return None


def update_run_status_by_id(config: dict[str, object], run_id: str, status: str):
    """
    Updates the status of a sequencing run in the database by its ID.

    Args:
        config (dict[str, object]): The configuration dictionary containing database file information.
        run_id (str): The ID of the sequencing run to update.
        status (str): The new status to set for the sequencing run.
    """
    db_file = get_db_file(config)

    runs_by_id = {}
    for row in _read_rows(db_file):
        if row['sequencing_run_id'] == run_id:
            row['status'] = status.upper()
        runs_by_id[row['sequencing_run_id']] = row

    _write_rows(db_file, list(runs_by_id.values()))


def add_run_to_db(config: dict[str, object], run: dict[str, object]):
    """
    Adds a new run to the database CSV file if it does not already exist.

    Args:
        config (dict[str, object]): The configuration dictionary containing database file information.
        run (dict[str, object]): The sequencing run data to add.
    """
    db_file = get_db_file(config)

    rows = _read_rows(db_file)
    rows.append({'sequencing_run_id': run['sequencing_run_id'], 'status': run['status']})

    _write_rows(db_file, rows)
