import datetime
import json
import logging
import os

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from nanopore_uploader.model import *

def store_run(config: dict[str, object], run: dict[str, object]):
    """
    """

    connection_uri = str(config['database_connection_uri'])
    engine = create_engine(connection_uri)
    Session = sessionmaker(bind=engine)
    session = Session()

    sequencing_run_id = run['sequencing_run_id']
    existing_run = session.query(SequencingRun).filter(SequencingRun.sequencing_run_id == sequencing_run_id).first()

    if existing_run is None:
        eight_digit_date = sequencing_run_id.split('_')[0]
        year = int(eight_digit_date[0:4])
        month = int(eight_digit_date[4:6])
        day = int(eight_digit_date[6:8])
        run_date = datetime.date(year, month, day)

        instrument_id = run['instrument_id']

        r = SequencingRun(
            sequencing_run_id = sequencing_run_id,
            protocol_group_id = run['protocol_group_id'],
            run_date = run_date,
            instrument_type = run['instrument_type'].upper(),
            instrument_id = instrument_id,
            status = run['status'].upper(),
            run_directory = run['run_directory'],
        )

        session.add(r)
        session.commit()
        logging.debug(json.dumps({"event_type": "run_stored", "run_id": sequencing_run_id}))
    else:
        logging.debug(json.dumps({"event_type": "run_exists", "run_id": sequencing_run_id}))


def get_run_by_id(config: dict[str, object], run_id: str):
    connection_uri = str(config['database_connection_uri'])
    engine = create_engine(connection_uri)
    Session = sessionmaker(bind=engine)
    session = Session()

    existing_run = session.query(SequencingRun).filter(SequencingRun.sequencing_run_id == run_id).first()
    if existing_run is not None:
        run = existing_run.__dict__
        run.pop('_sa_instance_state', None)
    else:
        run = None

    session.close()

    return run


def update_run_status_by_id(config: dict[str, object], run_id: str, status: str):
    """
    """
    connection_uri = str(config['database_connection_uri'])
    engine = create_engine(connection_uri)
    Session = sessionmaker(bind=engine)
    session = Session()

    existing_run = session.query(SequencingRun).filter(SequencingRun.sequencing_run_id == run_id).first()

    if existing_run is not None:
        existing_run.status = status.upper()
        session.add(existing_run)
        session.commit()

    session.close()


def update_run_upload_percent_complete_by_id(config: dict[str, object], run_id: str, upload_percent_complete: float):
    """
    """
    connection_uri = str(config['database_connection_uri'])
    engine = create_engine(connection_uri)
    Session = sessionmaker(bind=engine)
    session = Session()

    existing_run = session.query(SequencingRun).filter(SequencingRun.sequencing_run_id == run_id).first()

    if existing_run is not None:
        existing_run.upload_percent_complete = upload_percent_complete
        session.commit()

    session.close()
