import csv
import datetime
import json
import logging
import os

from nanopore_uploader.model import *

log = logging.getLogger(__name__)

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
    db_file = config.get('db', {}).get('file')

    with open(db_file, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row['sequencing_run_id'] == run_id:
                existing_run = row
                break
        else:
            existing_run = None

    return existing_run


def update_run_status_by_id(config: dict[str, object], run_id: str, status: str):
    """
    Updates the status of a sequencing run in the database by its ID.

    Args:
        config (dict[str, object]): The configuration dictionary containing database file information.
        run_id (str): The ID of the sequencing run to update.
        status (str): The new status to set for the sequencing run.
    """
    db_file = config.get('db', {}).get('file')

    runs_by_id = {}
    with open(db_file, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row['sequencing_run_id'] == run_id:
                row['status'] = status.upper()
            runs_by_id[row['sequencing_run_id']] = row

    with open(db_file, 'w', newline='') as csvfile:
        fieldnames = ['sequencing_run_id', 'status']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(runs_by_id.values())


def add_run_to_db(config: dict[str, object], run: dict[str, object]):
    """
    Adds a new run to the database CSV file if it does not already exist.

    Args:
        config (dict[str, object]): The configuration dictionary containing database file information.
        run (dict[str, object]): The sequencing run data to add.
    """
    db_file = config.get('db', {}).get('file')

    rows = []
    with open(db_file, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            rows.append(row)

    rows.append({'sequencing_run_id': run['sequencing_run_id'], 'status': run['status']})

    with open(db_file, 'w', newline='') as csvfile:
        fieldnames = ['sequencing_run_id', 'status']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
