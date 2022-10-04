import csv
import json
import logging

def _parse_list(list_path: str) -> list[str]:
    """
    Parse a 'list' file. List files are simple single-column text files, with one entry per line.

    :param list_path: Path to the list file.
    :type list_path: str
    :return: Contents of list file.
    :rtype: list[str]
    """
    items = []
    with open(list_path, 'r') as f:
        for line in f:
            item = line.strip()
            items.append(item)

    return items


def load_config(config_path: str) -> dict[str, object]:
    """
    :param config_path: Path to application config file (json format)
    :type config_path: str
    :return: Application config dictionary.
    :rtype: dict[str, object]
    """
    config = None
    with open(config_path, 'r') as f:
        config = json.load(f)
        
    return config


def make_config_json_serializable(original_config):
    """
    """
    config = original_config.copy()

    return config
