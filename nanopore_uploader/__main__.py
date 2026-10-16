#!/usr/bin/env python

import argparse
import logging
import sys

from nanopore_uploader.config import load_config
from nanopore_uploader.logging_config import configure_logging
from nanopore_uploader.service import Uploader

log = logging.getLogger(__name__)


def note_unused_settings(config: dict[str, object]):
    """
    Log a note about settings from earlier versions that are no longer used.
    """
    if 'db' in config:
        log.info({
            "event_type": "config_setting_unused",
            "setting": "db",
            "note": "Upload state now comes from each destination's upload_complete.json files, so the db file isn't used",
        })


def main(argv: list[str] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', required=True)
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('--log-file', help='Append logs to this file, rotated daily at midnight (default: log to stdout)')
    parser.add_argument('--log-retention-days', type=int, default=90, help='Number of rotated log files to keep. 0 keeps all of them (default: 90)')
    parser.add_argument('--log-format', default='json', choices=['json', 'text'])
    parser.add_argument('--tui', action='store_true', help='Show an interactive terminal dashboard instead of writing logs to stdout')
    args = parser.parse_args(argv)

    if args.tui:
        main_tui(args)
        return

    configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format)
    log.debug({"event_type": "debug_logging_enabled"})

    config = load_config(args.config)
    note_unused_settings(config)
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
    note_unused_settings(config)
    app = UploaderApp(args.config, config, tui_handler, log_level=args.log_level)
    app.run()


if __name__ == '__main__':
    main()
