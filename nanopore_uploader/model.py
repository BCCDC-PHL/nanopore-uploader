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

    sequencing_run_id = Column(String, primary_key=True)
    instrument_type = Column(String)
    instrument_id = Column(String)
    run_date = Column(Date)
    run_directory = Column(String)
    status = Column(String)
    timestamp_updated = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
