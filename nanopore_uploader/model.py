from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Optional



@dataclass
class Config:
    """
    Main application config.
    """
    scan_interval_seconds: float = 3600
    notification: dict = field(default_factory=dict)
    sequencers: list = field(default_factory=list)
    excluded_runs_list: Optional[Path] = None
    excluded_runs: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.scan_interval_seconds = float(self.scan_interval_seconds)

    @classmethod
    def from_dict(cls, data: dict):
        # Get all valid field names for this dataclass
        valid_fields = {f.name for f in fields(cls)}
        # Filter the input dictionary
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


class InstrumentType(StrEnum):
    """
    Instrument type ('gridion', 'promethion' or 'unknown')
    """
    gridion   = "gridion"
    promethion = "promethion"
    unknown = "unknown"


class UploadStatus(StrEnum):
    QUEUED_FOR_UPLOAD = "QUEUED_FOR_UPLOAD"
    UPLOAD_STARTED = "UPLOAD_STARTED"
    UPLOAD_COMPLETE = "UPLOAD_COMPLETE"
    UPLOAD_INCOMPLETE = "UPLOAD_INCOMPLETE"
    UNKNOWN = "UNKNOWN"


@dataclass
class SequencingRun(Base):
    sequencing_run_id: str
    protocol_group_id: str
    instrument_type: InstrumentType
    instrument_id: str
    run_date: datetime.date
    run_directory = Column(String)
    status: UploadStatus
    timestamp_upload_start = Column(String)
    timestamp_upload_complete = Column(String)
    upload_percent_complete = Column(Float, default=0.0)
    timestamp_updated = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
