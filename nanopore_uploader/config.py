import csv
import json
import logging
import os

log = logging.getLogger(__name__)

def _load_optional_list_file(path: os.PathLike, setting: str) -> list[str]:
    """
    Load a file with one entry per line. A missing file is treated as an empty list.

    :param path: Path to the file.
    :param setting: Name of the config setting the path came from (for logging).
    :return: Non-blank lines of the file.
    """
    if not os.path.exists(path):
        log.warning({"event_type": "config_file_not_found", "setting": setting, "path": str(path)})
        return []

    with open(path, 'r') as f:
        return [line.strip() for line in f if line.strip()]


def load_config(config_path: str) -> dict[str, object]:
    """
    :param config_path: Path to application config file (json format)
    :type config_path: str
    :return: Application config dictionary.
    :rtype: dict[str, object]
    """
    config = {}
    with open(config_path, 'r') as f:
        config = json.load(f)

    if 'excluded_runs_list' in config and os.path.exists(config['excluded_runs_list']):
        config['excluded_runs'] = _load_optional_list_file(config['excluded_runs_list'], 'excluded_runs_list')
        
    return config

