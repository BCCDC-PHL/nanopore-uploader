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


UPLOAD_NOW_MESSAGES = {
    'UPLOAD_COMPLETE': "Uploaded {run_id}",
    'ALREADY_UPLOADED': "{run_id} is already uploaded to every destination. Use --force to upload it again (this rewrites upload_complete.json).",
    'UPLOAD_INCOMPLETE_WITH_ERROR': "Uploading {run_id} failed. See the log for details.",
    'UPLOAD_INCOMPLETE': "Uploading {run_id} was stopped. It's partly uploaded, and will be resumed by the next upload.",
    'UNKNOWN': "{run_id} wasn't uploaded, because its destination(s) couldn't be checked. See the log for details.",
}


def upload_now(uploader: Uploader, run_id: str, force: bool):
    """
    Upload one run, then exit: 0 if it's uploaded, 1 otherwise.
    """
    try:
        result = uploader.upload_now(run_id, force=force)
    except (LookupError, ValueError) as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        log.info({"event_type": "quit"})
        sys.exit(f"Stopped. {run_id} is partly uploaded, and will be resumed by the next upload.")
    message = UPLOAD_NOW_MESSAGES.get(result, "{run_id}: " + result).format(run_id=run_id)
    if result in ('UPLOAD_COMPLETE', 'ALREADY_UPLOADED'):
        print(message)
    else:
        sys.exit(f"Error: {message}")


def main(argv: list[str] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', required=True)
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('--log-file', help='Append logs to this file, rotated daily at midnight (default: log to stdout)')
    parser.add_argument('--log-retention-days', type=int, default=90, help='Number of rotated log files to keep. 0 keeps all of them (default: 90)')
    parser.add_argument('--log-format', default='json', choices=['json', 'text'])
    parser.add_argument('--tui', action='store_true', help='Show an interactive terminal dashboard instead of writing logs to stdout')
    parser.add_argument('--upload', metavar='RUN_ID', help="Upload one run to its destinations where it isn't already uploaded, then exit")
    parser.add_argument('--force', action='store_true', help="With --upload: upload again even where it's already uploaded (rewrites upload_complete.json)")
    args = parser.parse_args(argv)
    if args.force and not args.upload:
        parser.error("--force only works with --upload")
    if args.upload and args.tui:
        parser.error("--upload and --tui can't be used together")

    if args.tui:
        main_tui(args)
        return

    configure_logging(args.log_level, args.log_file, args.log_retention_days, args.log_format)
    log.debug({"event_type": "debug_logging_enabled"})

    config = load_config(args.config)
    note_unused_settings(config)
    uploader = Uploader(args.config, config)

    if args.upload:
        upload_now(uploader, args.upload, args.force)
        return

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
