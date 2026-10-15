"""
Interactive terminal dashboard for the uploader.

The scan/upload loop (`service.Uploader`) runs unchanged on a background thread. The TUI learns what it's
doing from the same structured log events that get written to the log file: `TUILogHandler` forwards
each log record to the app as a message, and the app updates the runs table, status line and log panel
from them. rsync progress comes in through a separate callback, because only every 5% is logged.
"""

import collections
import csv
import datetime
import json
import logging
import os
import re
import threading

from dataclasses import dataclass
from typing import Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, ProgressBar, RichLog, Static

from nanopore_uploader.service import Uploader

log = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 5000

# Statuses that only exist in the TUI (the db doesn't record failures or exclusions).
UPLOAD_FAILED = 'UPLOAD_FAILED'
EXCLUDED = 'EXCLUDED'

STATUS_DISPLAY = {
    'QUEUED_FOR_UPLOAD': ("◷ Queued", "yellow"),
    'UPLOAD_STARTED': ("◐ Started, not finished", "dark_orange"),
    'UPLOAD_COMPLETE': ("✓ Complete", "green"),
    'UPLOAD_INCOMPLETE': ("✗ Incomplete", "red"),
    'UPLOAD_INCOMPLETE_WITH_ERROR': ("✗ Error", "red"),
    UPLOAD_FAILED: ("✗ Failed", "bold red"),
    EXCLUDED: ("– Excluded", "dim"),
}
ACTIVE_UPLOAD_DISPLAY = ("● Uploading", "bold cyan")

INSTRUMENT_TYPE_NAMES = {
    'GRIDION': "GridION",
    'PROMETHION': "PromethION",
}

# Events left out of the 'activity' log view, because they happen for every run on every scan
# or are already shown elsewhere (status line, progress bar, runs table).
QUIET_EVENTS = {
    'config_loaded',
    'candidate_run_dirs_found',
    'found_run',
    'scan_started',
    'upload_started',
    'upload_progress_update',
    'upload_complete_file_written',
}

LEVEL_STYLES = {
    logging.DEBUG: "dim",
    logging.INFO: "green",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}


class LogRecordReceived(Message):
    """A log record, forwarded from `TUILogHandler`."""

    def __init__(self, created: float, levelno: int, levelname: str, payload: dict, exc_text: Optional[str]):
        super().__init__()
        self.created = created
        self.levelno = levelno
        self.levelname = levelname
        self.payload = payload
        self.exc_text = exc_text


class UploadProgress(Message):
    """An rsync progress update, from `core.upload_run`'s `on_progress` callback."""

    def __init__(self, progress: dict):
        super().__init__()
        self.progress = progress


class UploaderStopped(Message):
    """The uploader thread has finished, either because it was stopped or because of an unexpected error."""

    def __init__(self, error: Optional[BaseException]):
        super().__init__()
        self.error = error


class TUILogHandler(logging.Handler):
    """
    Forwards this package's log records to the TUI as `LogRecordReceived` messages.

    Records emitted before the app is ready are kept, and passed on once `attach()` is called.
    Textual's `post_message` is thread-safe, so this can be used from the uploader thread.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self._app: Optional[App] = None
        self._pending: list[LogRecordReceived] = []
        self._lock_attach = threading.Lock()
        self._exc_formatter = logging.Formatter()
        self.addFilter(lambda record: record.name in ('root', '__main__') or record.name.startswith('nanopore_uploader'))

    def attach(self, app: App):
        with self._lock_attach:
            self._app = app
            pending, self._pending = self._pending, []
        for message in pending:
            app.post_message(message)

    def emit(self, record: logging.LogRecord):
        try:
            payload = record.msg if isinstance(record.msg, dict) else _parse_json_message(record.getMessage())
            exc_text = self._exc_formatter.formatException(record.exc_info) if record.exc_info else None
            message = LogRecordReceived(record.created, record.levelno, record.levelname, payload, exc_text)
            with self._lock_attach:
                if self._app is None:
                    self._pending.append(message)
                    return
                app = self._app
            app.post_message(message)
        except Exception:
            self.handleError(record)


def _parse_json_message(message: str) -> dict:
    """
    A few events are logged as JSON-encoded strings rather than dicts; decode them so they're handled the same way.
    """
    if message.startswith('{'):
        try:
            payload = json.loads(message)
            if isinstance(payload, dict):
                return payload
        except ValueError:
            pass
    return {"message": message}


@dataclass
class RunInfo:
    sequencing_run_id: str
    status: Optional[str] = None
    instrument: Optional[str] = None
    protocol_group_id: Optional[str] = None
    run_directory: Optional[str] = None
    last_error: Optional[str] = None
    timestamp_updated: Optional[datetime.datetime] = None


def guess_instrument(run_id: str) -> Optional[str]:
    """
    Guess the instrument type from a run ID, for runs that are in the db but haven't been found by a scan yet.
    """
    if re.search(r"_P2S_", run_id):
        return "PromethION"
    if re.search(r"_X[1-5]_", run_id):
        return "GridION"
    return None


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str, yes_label: str = "Yes", no_label: str = "Cancel", danger: bool = False):
        super().__init__()
        self.question = question
        self.yes_label = yes_label
        self.no_label = no_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.danger else ""):
            yield Label(self.question, id="question")
            with Horizontal(id="buttons"):
                yield Button(f"{self.yes_label} (y)", variant="error" if self.danger else "success", id="yes")
                yield Button(f"{self.no_label} (n)", variant="primary", id="no")

    def on_mount(self):
        self.query_one("#no", Button).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool):
        self.dismiss(answer)


class UploaderApp(App):
    TITLE = "Nanopore Uploader"

    CSS = """
    #status {
        height: 1;
        padding: 0 1;
        background: $boost;
        text-style: bold;
    }
    #runs {
        height: 1fr;
        border: round $primary;
    }
    #details {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #upload {
        height: auto;
        border: round $accent;
        padding: 0 1;
        display: none;
    }
    #upload.active {
        display: block;
    }
    #upload ProgressBar {
        width: 100%;
    }
    #upload Bar {
        width: 1fr;
    }
    #log {
        height: 14;
        border: round $panel-lighten-2;
    }
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 76;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #question {
        width: 100%;
        margin-bottom: 1;
    }
    #buttons {
        width: 100%;
        height: auto;
        align-horizontal: right;
    }
    #dialog.danger {
        border: thick $error;
    }
    #buttons Button {
        width: auto;
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("s", "scan_now", "Scan now"),
        Binding("r", "requeue", "Re-queue run"),
        Binding("x", "exclude", "Exclude run"),
        Binding("l", "toggle_log", "Log: activity/all"),
        Binding("q", "request_quit", "Quit"),
        Binding("ctrl+q", "request_quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, config_path: os.PathLike, config: dict[str, object], log_handler: TUILogHandler, log_level: str = "info"):
        super().__init__()
        self.config_path = config_path
        self.log_handler = log_handler
        self.show_debug = log_level.lower() == "debug"
        self.uploader = Uploader(
            config_path,
            config,
            on_progress=lambda progress: self.post_message(UploadProgress(progress)),
            capture_rsync_stderr=True,
        )
        self.runs: dict[str, RunInfo] = {}
        self.log_entries: collections.deque[LogRecordReceived] = collections.deque(maxlen=MAX_LOG_ENTRIES)
        self.log_mode = "activity"
        self.state = "starting"
        self.active_upload_run_id: Optional[str] = None
        self.next_scan_at: Optional[datetime.datetime] = None
        self.runs_found_this_scan = 0
        self.stopping = False
        self.uploader_error: Optional[BaseException] = None
        self.uploader_thread: Optional[threading.Thread] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Static(id="details")
        with Vertical(id="upload"):
            yield Label(id="upload-title")
            yield ProgressBar(id="upload-progress", total=100, show_eta=False)
            yield Label(id="upload-stats")
        yield RichLog(id="log", wrap=True, max_lines=MAX_LOG_ENTRIES)
        yield Footer()

    def on_mount(self):
        self.sub_title = os.path.abspath(self.config_path)
        table = self.query_one("#runs", DataTable)
        table.border_title = "Sequencing runs"
        # Fixed widths, because auto-sized columns don't grow when a cell is updated with something longer.
        table.add_column("Run ID", key="run_id", width=44)
        table.add_column("Instrument", key="instrument", width=20)
        table.add_column("Protocol group", key="protocol_group_id", width=18)
        table.add_column("Status", key="status", width=23)
        table.add_column("Last update", key="timestamp_updated", width=19)
        self.query_one("#upload", Vertical).border_title = "Current upload"
        self._update_log_title()

        self._load_known_runs()
        self.log_handler.attach(self)
        self.set_interval(1, self._refresh_status)
        self._refresh_status()

        self.uploader_thread = threading.Thread(target=self._run_uploader, name="uploader", daemon=True)
        self.uploader_thread.start()

    def on_unmount(self):
        # Make sure an rsync process isn't left running if the app exits some other way.
        self.uploader.stop()

    def _run_uploader(self):
        error = None
        try:
            self.uploader.run()
        except BaseException as e:
            error = e
            log.error({"event_type": "unexpected_error", "error_type": type(e).__name__, "error": str(e)}, exc_info=True)
        self.post_message(UploaderStopped(error))

    # ---- Runs table ----

    def _load_known_runs(self):
        """
        Fill the runs table from the db and the excluded runs list, so it isn't empty until the first scan finishes.
        """
        config = self.uploader.config
        db_file = config.get('db', {}).get('file')
        if db_file and os.path.exists(db_file):
            try:
                with open(db_file, newline='') as f:
                    for row in csv.DictReader(f):
                        run_id = row.get('sequencing_run_id')
                        if run_id:
                            self._update_run(run_id, status=row.get('status'), instrument=guess_instrument(run_id), touch=False)
            except Exception as e:
                log.warning({"event_type": "db_read_failed", "db_file": db_file, "error": str(e)})
        else:
            log.warning({"event_type": "db_file_not_found", "db_file": db_file})

        for run_id in config.get('excluded_runs', []):
            self._update_run(run_id, status=EXCLUDED, instrument=guess_instrument(run_id), touch=False)

    def _update_run(self, run_id: str, touch: bool = True, **changes):
        run = self.runs.get(run_id)
        is_new = run is None
        if is_new:
            run = RunInfo(run_id)
            self.runs[run_id] = run
        for key, value in changes.items():
            if value is not None:
                setattr(run, key, value)
        if touch:
            run.timestamp_updated = datetime.datetime.now()

        table = self.query_one("#runs", DataTable)
        cells = self._run_cells(run)
        if is_new:
            table.add_row(*cells.values(), key=run_id)
            # Run IDs start with the date and time, so this puts the newest runs first.
            table.sort("run_id", reverse=True)
        else:
            for column, value in cells.items():
                table.update_cell(run_id, column, value)
        self._show_details()

    def _run_cells(self, run: RunInfo) -> dict[str, object]:
        if run.sequencing_run_id == self.active_upload_run_id:
            label, style = ACTIVE_UPLOAD_DISPLAY
        else:
            label, style = STATUS_DISPLAY.get(run.status, (run.status or "?", ""))
        updated = run.timestamp_updated.strftime("%Y-%m-%d %H:%M:%S") if run.timestamp_updated else "—"
        return {
            "run_id": run.sequencing_run_id,
            "instrument": run.instrument or "—",
            "protocol_group_id": run.protocol_group_id or "—",
            "status": Text(label, style=style),
            "timestamp_updated": updated,
        }

    def _selected_run(self) -> Optional[RunInfo]:
        table = self.query_one("#runs", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return self.runs.get(row_key.value)

    @on(DataTable.RowHighlighted)
    def _show_details(self):
        details = self.query_one("#details", Static)
        run = self._selected_run()
        if run is None:
            details.update("No runs yet.")
            return
        text = Text()
        text.append(run.run_directory or "Not found on disk by a scan yet", style="italic")
        if run.last_error:
            text.append("\nLast error: ", style="bold red")
            text.append(run.last_error)
        details.update(text)

    # ---- Messages from the uploader ----

    @on(LogRecordReceived)
    def _log_record_received(self, record: LogRecordReceived):
        self._update_state(record)
        self.log_entries.append(record)
        line = self._format_log_entry(record)
        if line is not None:
            self.query_one("#log", RichLog).write(line)

    def _update_state(self, record: LogRecordReceived):
        """
        Update the runs table and status line from a log event.
        """
        payload = record.payload
        event_type = payload.get('event_type')
        run_id = payload.get('sequencing_run_id')

        if event_type == 'scan_started':
            self.state = "scanning"
            self.runs_found_this_scan = 0
        elif event_type == 'found_run':
            self.runs_found_this_scan += 1
            instrument_type = payload.get('instrument_type') or ''
            instrument = " ".join(filter(None, [
                INSTRUMENT_TYPE_NAMES.get(instrument_type.upper(), instrument_type),
                payload.get('instrument_id'),
            ]))
            self._update_run(
                run_id,
                touch=False,
                instrument=instrument or guess_instrument(run_id),
                protocol_group_id=payload.get('protocol_group_id'),
                run_directory=payload.get('run_directory'),
            )
        elif event_type == 'run_status_checked':
            run = self.runs.get(run_id)
            # The db doesn't know about failures, so don't let a re-check hide one.
            if run is None or run.status != UPLOAD_FAILED or payload.get('status') == 'QUEUED_FOR_UPLOAD':
                self._update_run(run_id, touch=False, status=payload.get('status'))
        elif event_type == 'upload_starting':
            self.state = "uploading"
            self.active_upload_run_id = run_id
            self._update_run(run_id, status='UPLOAD_STARTED', last_error="")
            self._start_upload_panel(run_id)
        elif event_type == 'upload_complete':
            self._update_run(run_id, status='UPLOAD_COMPLETE')
        elif event_type == 'upload_cancelled':
            self._finish_upload(run_id, 'UPLOAD_INCOMPLETE')
        elif event_type == 'upload_failed':
            error = f"{payload.get('error_type')}: {payload.get('error')}"
            run = self.runs.get(run_id)
            if run is not None and run.last_error:
                error = f"{error}\n{run.last_error}"
            self._finish_upload(run_id, UPLOAD_FAILED, error=error)
        elif event_type == 'rsync_returncode_captured' and payload.get('rsync_returncode') not in (0, None) and not self.stopping:
            self._update_run(run_id, touch=False, last_error=f"rsync exited with code {payload.get('rsync_returncode')}")
        elif event_type == 'rsync_stderr' and run_id in self.runs:
            run = self.runs[run_id]
            error = f"rsync: {payload.get('stderr')}"
            if run.last_error:
                error = f"{run.last_error}\n{error}"
            self._update_run(run_id, touch=False, last_error=error)
        elif event_type == 'upload_run_finished':
            # Covers uploads that end without an upload_complete/failed/cancelled event (e.g. rsync exiting with an error).
            if run_id == self.active_upload_run_id:
                self._finish_upload(run_id, None)
                self._update_run(run_id, touch=True)
        elif event_type == 'run_requeued':
            self._update_run(run_id, status='QUEUED_FOR_UPLOAD', last_error="")
        elif event_type == 'run_excluded':
            self._update_run(run_id, status=EXCLUDED)
        elif event_type == 'scan_complete':
            self._finish_upload(None, None)
            self.state = "idle"
            try:
                self.next_scan_at = datetime.datetime.fromisoformat(payload.get('timestamp_next_scan_start'))
            except (TypeError, ValueError):
                self.next_scan_at = None
        self._refresh_status()

    def _finish_upload(self, run_id: Optional[str], status: Optional[str], error: Optional[str] = None):
        if run_id is not None and run_id == self.active_upload_run_id:
            self.active_upload_run_id = None
            self.query_one("#upload", Vertical).remove_class("active")
            if self.state == "uploading":
                self.state = "scanning"
        elif run_id is None:
            self.active_upload_run_id = None
            self.query_one("#upload", Vertical).remove_class("active")
        if run_id is not None and status is not None:
            self._update_run(run_id, status=status, last_error=error)

    @on(UploadProgress)
    def _upload_progress(self, message: UploadProgress):
        progress = message.progress
        if progress.get('sequencing_run_id') != self.active_upload_run_id:
            return
        self.query_one("#upload-title", Label).update(
            f"{progress['sequencing_run_id']}  →  {progress.get('server_id') or 'destination'}"
        )
        self.query_one("#upload-progress", ProgressBar).update(progress=progress['percent_complete'])
        self.query_one("#upload-stats", Label).update(
            f"{progress['data_transferred']} transferred · {progress['upload_speed']} · elapsed {progress['upload_duration']}"
        )

    def _start_upload_panel(self, run_id: str):
        self.query_one("#upload-title", Label).update(f"{run_id}  →  connecting…")
        self.query_one("#upload-progress", ProgressBar).update(progress=0)
        self.query_one("#upload-stats", Label).update("")
        self.query_one("#upload", Vertical).add_class("active")

    @on(UploaderStopped)
    def _uploader_stopped(self, message: UploaderStopped):
        self._finish_upload(None, None)
        if self.stopping:
            self.exit()
            return
        self.uploader_error = message.error
        self.state = "error"
        self._refresh_status()
        self.notify("The uploader stopped because of an unexpected error. See the log for details.", severity="error", timeout=30)

    # ---- Status line ----

    def _refresh_status(self):
        status = self.query_one("#status", Static)
        if self.stopping:
            status.update(Text("■ Stopping…", style="yellow"))
        elif self.state == "starting":
            status.update(Text("Starting…"))
        elif self.state == "scanning":
            status.update(Text("⟳ Scanning for completed runs…", style="cyan"))
        elif self.state == "uploading":
            status.update(Text(f"● Uploading {self.active_upload_run_id}", style="bold cyan"))
        elif self.state == "idle":
            text = Text("✓ Idle", style="green")
            if self.next_scan_at is not None:
                remaining = max(0, int((self.next_scan_at - datetime.datetime.now()).total_seconds()))
                hours, rest = divmod(remaining, 3600)
                minutes, seconds = divmod(rest, 60)
                countdown = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
                text.append(f" · next scan in {countdown} (at {self.next_scan_at:%H:%M:%S})", style="default")
            status.update(text)
        elif self.state == "error":
            status.update(Text("✗ Uploader stopped: unexpected error (see log). Press q to quit.", style="bold red"))

    # ---- Log panel ----

    def _format_log_entry(self, entry: LogRecordReceived) -> Optional[Text]:
        payload = entry.payload
        event_type = payload.get('event_type')
        if self.log_mode == "activity":
            if entry.levelno < logging.INFO or event_type in QUIET_EVENTS:
                return None
        elif entry.levelno < logging.INFO and not self.show_debug:
            return None

        text = Text()
        text.append(datetime.datetime.fromtimestamp(entry.created).strftime("%H:%M:%S "), style="dim")
        if self.log_mode == "all" or entry.levelno >= logging.WARNING:
            text.append(f"{entry.levelname:<7} ", style=LEVEL_STYLES.get(entry.levelno, ""))
        if self.log_mode == "activity":
            text.append(self._describe_event(payload))
        else:
            text.append(str(event_type or ""), style="bold")
            for key, value in payload.items():
                if key != 'event_type':
                    text.append(f"  {key}=", style="dim")
                    text.append(str(value))
            if entry.exc_text:
                text.append("\n" + entry.exc_text, style="dim")
        return text

    def _describe_event(self, payload: dict) -> str:
        """
        A plain-language description of a log event, for the 'activity' view.
        """
        event_type = payload.get('event_type')
        run_id = payload.get('sequencing_run_id')
        if event_type == 'upload_starting':
            return f"Uploading {run_id}"
        if event_type == 'upload_complete':
            return f"Upload complete: {run_id}"
        if event_type == 'upload_cancelled':
            return f"Upload stopped: {run_id} (marked UPLOAD_INCOMPLETE)"
        if event_type == 'upload_failed':
            return f"Upload failed: {run_id}: {payload.get('error_type')}: {payload.get('error')}"
        if event_type == 'run_requeued':
            return f"Re-queued for upload: {run_id}"
        if event_type == 'run_excluded':
            return f"Excluded from upload: {run_id}"
        if event_type == 'rsync_stderr':
            return f"rsync reported a problem with {run_id}: {payload.get('stderr')}"
        if event_type == 'scan_complete':
            description = f"Scan finished, {self.runs_found_this_scan} completed run(s) found"
            try:
                next_scan = datetime.datetime.fromisoformat(payload.get('timestamp_next_scan_start'))
                description += f"; next scan at {next_scan:%H:%M:%S}"
            except (TypeError, ValueError):
                pass
            return description
        if event_type == 'db_file_not_found':
            return f"The upload database file wasn't found: {payload.get('db_file')}"
        if event_type == 'upload_progress_update':
            return f"{run_id}: {payload.get('percent_complete')}%"
        if event_type is None:
            return str(payload.get('message', payload))
        details = ", ".join(f"{k}={v}" for k, v in payload.items() if k != 'event_type')
        return f"{event_type}: {details}" if details else event_type

    def _update_log_title(self):
        label = "activity" if self.log_mode == "activity" else "all events"
        self.query_one("#log", RichLog).border_title = f"Log ({label}) · l to switch"

    def action_toggle_log(self):
        self.log_mode = "all" if self.log_mode == "activity" else "activity"
        self._update_log_title()
        log_widget = self.query_one("#log", RichLog)
        log_widget.clear()
        for entry in self.log_entries:
            line = self._format_log_entry(entry)
            if line is not None:
                log_widget.write(line)

    # ---- Actions ----

    def action_scan_now(self):
        if self.stopping or self.state == "error":
            return
        if self.state in ("scanning", "uploading"):
            self.notify("A scan is already running. Another will start as soon as it finishes.")
        self.uploader.scan_now()

    def action_requeue(self):
        run = self._selected_run()
        if run is None or self.stopping:
            return
        if run.sequencing_run_id == self.active_upload_run_id:
            self.notify("That run is uploading now.", severity="warning")
            return
        if run.status == EXCLUDED:
            self.notify(
                "That run is in the excluded runs list. Remove it from the list first.",
                severity="warning",
            )
            return

        def confirmed(answer: bool):
            if answer:
                self.uploader.requeue(run.sequencing_run_id)
                self.notify(f"Re-queued {run.sequencing_run_id}")

        self.push_screen(
            ConfirmScreen(f"Upload {run.sequencing_run_id} again?\n\nrsync only copies files that are missing or changed on the destination.", yes_label="Re-queue"),
            confirmed,
        )

    def action_exclude(self):
        run = self._selected_run()
        if run is None or self.stopping:
            return
        if run.status == EXCLUDED:
            return
        if not self.uploader.can_exclude():
            self.notify("No 'excluded_runs_list' file is set in the config, so runs can't be excluded from here.", severity="warning")
            return

        def confirmed(answer: bool):
            if answer:
                self.uploader.exclude(run.sequencing_run_id)

        note = "\n\nIt's uploading now; this won't stop the current upload." if run.sequencing_run_id == self.active_upload_run_id else ""
        self.push_screen(
            ConfirmScreen(
                f"Exclude {run.sequencing_run_id} from uploading?\n\n"
                f"It will be added to {self.uploader.config.get('excluded_runs_list')}.{note}",
                yes_label="Exclude",
                danger=True,
            ),
            confirmed,
        )

    def action_request_quit(self):
        if self.stopping:
            # Pressed again while waiting for the uploader to stop: don't wait any longer.
            self.exit()
            return
        if self.state == "error" or self.uploader_thread is None or not self.uploader_thread.is_alive():
            self.exit()
            return

        def confirmed(answer: bool):
            if answer:
                self._stop_and_exit()

        uploading_run_id = self.uploader.uploading_run_id
        if uploading_run_id is not None:
            self.push_screen(
                ConfirmScreen(
                    f"An upload is in progress ({uploading_run_id}).\n\n"
                    "Quitting will stop it, and the run will be marked UPLOAD_INCOMPLETE.",
                    yes_label="Stop upload and quit",
                    danger=True,
                ),
                confirmed,
            )
        else:
            self._stop_and_exit()

    def _stop_and_exit(self):
        self.stopping = True
        self._refresh_status()
        log.info({"event_type": "quit"})
        self.uploader.stop()
