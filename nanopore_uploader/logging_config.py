from datetime import datetime
import json
import logging
import logging.handlers
import os
import sys

from typing import Optional

class JSONFormatter(logging.Formatter):
    """
    Custom formatter for logging in structured JSON Lines.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str]=None) -> str:
        """
        Returns the creation time of the LogRecord formatted with milliseconds.
        :param record: The logging record
        :return: The log record creation time, formatted as an ISO timestamp
        """
        # Convert record.created timestamp into a local datetime object
        dt = datetime.fromtimestamp(record.created)
        # Format date and time down to seconds, then slice off microsecond precision to milliseconds
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}"

    def _safe_serialize(self, obj):
        """Recursively forces non-serializable objects into strings."""
        try:
            # default=str automatically converts datetimes, sets, and custom objects to strings
            return json.dumps(obj, default=str)
        except Exception:
            # Absolute worst-case scenario fallback (e.g., recursive loops)
            return json.dumps({
                "timestamp": obj.get("timestamp"),
                "level": obj.get("level"),
                "module": obj.get("module"),
                "function_name": obj.get("function_name"),
                "line_num": obj.get("line_num"),
                "message": {"event": "logging_error",
                            "original_msg_repr": repr(obj.get("message"))}
            })

    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log record as a JSON object, encoded as a string.
        """
        # Build the foundational structured log entry
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "module": record.module,
            "function_name": record.funcName,
            "line_num": record.lineno,
        }
        if isinstance(record.msg, dict):
            log_entry["message"] = record.msg
        else:
            # Fallback for standard string messages
            log_entry["message"] = record.getMessage()

        # Capture tracebacks automatically if an exception occurred
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
            
        # Dynamically inject keys passed via the 'extra' keyword argument
        # while ignoring standard built-in LogRecord attributes
        builtins = {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in builtins:
                log_entry[key] = value

        try:
            # First attempt: Try standard, fast serialization
            return json.dumps(log_entry)
        except TypeError:
            # Second attempt: Graceful recovery if an object (like a set or datetime) fails
            return self._safe_serialize(log_entry)


def make_log_handler(log_file: Optional[os.PathLike]=None, log_retention_days: int=90) -> logging.Handler:
    """
    Make the handler that log records are written to.

    If a log file is given, the log is appended to that file (so restarting doesn't overwrite it), and rotated at midnight.
    Rotated logs are named with the date they cover (eg. 'qc-check.jsonl.2026-10-05').

    :param log_file: Path to the log file. If None, log to stdout.
    :param log_retention_days: Number of rotated log files to keep. Older ones are deleted. 0 keeps all of them.
    :return: The log handler.
    """
    if log_file is None:
        return logging.StreamHandler(sys.stdout)

    return logging.handlers.TimedRotatingFileHandler(log_file, when='midnight', backupCount=log_retention_days, encoding='utf-8')


def configure_logging(log_level: str="info", log_file: Optional[os.PathLike]=None, log_retention_days: int=90):
    """
    Configure logging

    :param log_level: Log level ('debug', 'info', 'warning', 'error') default: 'info'
    :param log_file: Path to the log file. If None, log to stdout.
    :param log_retention_days: Number of rotated log files to keep. 0 keeps all of them.
    """
    handler = make_log_handler(log_file, log_retention_days)
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(
        level=log_level.upper(),
        handlers=[handler]
    )
