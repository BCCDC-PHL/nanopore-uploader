#!/usr/bin/env python

import argparse
import logging
import os
import sys

import nanopore_uploader.db as db
from nanopore_uploader.config import load_config
from nanopore_uploader.logging_config import configure_logging
from nanopore_uploader.service import Uploader

log = logging.getLogger(__name__)


def init_db(config: dict[str, object]):
    """
    Create the upload db file for a new installation. Exits with an error message if it can't be created.
    """
    try:
        db_file = os.path.abspath(db.get_db_file(config))
        db.init_db_file(config)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    except FileExistsError:
        sys.exit(f"Error: the upload db file already exists, so it was left as it is: {db_file}")
    except FileNotFoundError:
        sys.exit(f"Error: can't create the upload db file, because its directory doesn't exist: {os.path.dirname(db_file)}")
    print(f"Created upload db file: {db_file}")


def check_db_or_exit(config: dict[str, object]):
    """
    Exit with an error message if the upload db file is missing, rather than treating every run as new.
    """
    try:
        db.check_db_file(config)
    except (FileNotFoundError, ValueError) as e:
        log.error({"event_type": "db_file_not_found", "error": str(e)})
        sys.exit(f"Error: {e}")


def main(argv: list[str] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', required=True)
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('--log-file', help='Append logs to this file, rotated daily at midnight (default: log to stdout)')
    parser.add_argument('--log-retention-days', type=int, default=90, help='Number of rotated log files to keep. 0 keeps all of them (default: 90)')
    parser.add_argument('--log-format', default='json', choices=['json', 'text'])
    parser.add_argument('--tui', action='store_true', help='Show an interactive terminal dashboard instead of writing logs to stdout')
    parser.add_argument('--init-db', action='store_true', help="Create an empty upload db file (the config's db.file) for a new installation, then exit. Its directory must already exist")
    args = parser.parse_args(argv)

    if args.init_db:
        # Only log to a file here: on stdout it would just repeat the message that's printed.
        if args.log_file is not None:
            configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format)
        init_db(load_config(args.config))
        return

    if args.tui:
        main_tui(args)
        return

    configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format)
    log.debug({"event_type": "debug_logging_enabled"})

    config = load_config(args.config)
    check_db_or_exit(config)
    uploader = Uploader(args.config, config)

    try:
        uploader.run()
    except KeyboardInterrupt:
        log.info({"event_type": "quit"})
    except Exception as e:
        log.error({"event_type": "unexpected_error", "error": str(e)})


def main_tui(args: argparse.Namespace):
    try:
        from nanopore_uploader.tui.app import UploaderApp, TUILogHandler
    except ImportError as e:
        print(f"The TUI needs the 'textual' package ({e}). Install it with: pip install 'nanopore-uploader[tui]'", file=sys.stderr)
        sys.exit(1)

    tui_handler = TUILogHandler()
    configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format, tui_handler=tui_handler)

    config = load_config(args.config)
    check_db_or_exit(config)
    app = UploaderApp(args.config, config, tui_handler, log_level=args.log_level)
    app.run()


if __name__ == '__main__':
    main()
