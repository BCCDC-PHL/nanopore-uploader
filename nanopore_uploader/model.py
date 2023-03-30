import datetime

from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Boolean
from sqlalchemy import Integer
from sqlalchemy import Float
from sqlalchemy import String
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import JSON

from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()

class SequencingRun(Base):
    __tablename__ = 'sequencing_run'

    id = Column(Integer, primary_key=True, autoincrement=True)
    sequencing_run_id = Column(String)
    protocol_group_id = Column(String)
    instrument_type = Column(String)
    instrument_id = Column(String)
    run_date = Column(Date)
    run_directory = Column(String)
    status = Column(String)
    timestamp_upload_start = Column(String)
    timestamp_upload_complete = Column(String)
    upload_percent_complete = Column(Float, default=0.0)
    timestamp_updated = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
