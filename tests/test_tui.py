"""
End-to-end tests of the TUI, using Textual's test pilot (no real terminal needed).
"""
import asyncio
import logging

import pytest

pytest.importorskip("textual")

import nanopore_uploader.core as core
from nanopore_uploader.tui.app import EXCLUDED, UPLOAD_FAILED, TUILogHandler, UploaderApp

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, make_destination, make_run, mark_uploaded, save_config


@pytest.fixture
def tui_handler():
    """
    Pass log records to the TUI, as `--tui` does.
    """
    handler = TUILogHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous_level)


def auto_mode(config):
    config["auto_upload"] = True
    save_config(config)


async def wait_until(pilot, condition, timeout: float = 10.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting for condition")


def test_runs_table_follows_uploads(config, run_output_dir, destination_dir, fake_rsync, fake_ssh, tui_handler):
    make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id="sample-02")
    mark_uploaded(destination_dir, GRIDION_RUN_ID_2)
    auto_mode(config)
    excluded_run_id = "20261001_0000_X1_FAZ00000_00000000"
    with open(config['excluded_runs_list'], 'w') as f:
        f.write(excluded_run_id + "\n")
    config['excluded_runs'] = [excluded_run_id]

    async def scenario():
        app = UploaderApp(config['_path'], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            statuses = {run_id: run.status for run_id, run in app.runs.items()}
            destinations = app.runs[GRIDION_RUN_ID].destinations
            table_rows = app.query_one("#runs").row_count
            next_scan_at = app.next_scan_at
            await pilot.press("q")
            await wait_until(pilot, lambda: not app.is_running)
        return statuses, destinations, table_rows, next_scan_at

    statuses, destinations, table_rows, next_scan_at = asyncio.run(scenario())

    assert statuses == {
        GRIDION_RUN_ID: core.UPLOADED,
        GRIDION_RUN_ID_2: core.UPLOADED,
        excluded_run_id: EXCLUDED,
    }
    assert destinations == {"archive": core.UPLOADED}
    assert table_rows == 3
    assert next_scan_at is not None
    # Only the run that wasn't already at the destination was uploaded.
    assert [args[-2].rsplit("/", 1)[-1] for args in fake_rsync.calls()] == [GRIDION_RUN_ID]


def test_failed_upload_shows_the_error(config, run_output_dir, fake_rsync, fake_ssh, tui_handler, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
    auto_mode(config)
    monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")
    monkeypatch.setenv("FAKE_RSYNC_EXIT_CODE", "255")

    async def scenario():
        app = UploaderApp(config['_path'], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            run = app.runs[GRIDION_RUN_ID]
            result = run.status, run.last_error
            await pilot.press("q")
        return result

    status, last_error = asyncio.run(scenario())

    assert status == UPLOAD_FAILED
    assert "rsync exited with code 255 (SSH connection failed)" in last_error
    # rsync's own error message, captured from its stderr.
    assert "rsync: fake failure" in last_error


def test_quitting_during_an_upload_asks_first(config, run_output_dir, fake_rsync, fake_ssh, tui_handler, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
    auto_mode(config)
    monkeypatch.setenv("FAKE_RSYNC_SLEEP", "30")

    async def scenario():
        app = UploaderApp(config['_path'], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.uploader.uploading_run_id is not None)
            await pilot.press("q")
            await pilot.pause(0.2)
            asked = type(app.screen).__name__
            await pilot.press("y")
            await wait_until(pilot, lambda: not app.uploader_thread.is_alive())
        return asked

    asked = asyncio.run(scenario())

    assert asked == "ConfirmScreen"
    # Left partly uploaded, to be resumed next time.
    assert core.list_remote_runs(config["sequencers"][0]["destination_servers"][0]) == {GRIDION_RUN_ID: core.PARTIAL}


def test_run_isnt_shown_as_uploaded_while_a_destination_cant_be_checked(config, run_output_dir, tmp_path, fake_rsync, fake_ssh, tui_handler):
    make_run(run_output_dir, GRIDION_RUN_ID)
    unreachable = make_destination(tmp_path, "offline-archive")
    unreachable["destination_dir"] = str(tmp_path / "not-mounted")
    config["sequencers"][0]["destination_servers"].append(unreachable)
    auto_mode(config)

    async def scenario():
        app = UploaderApp(config["_path"], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            run = app.runs[GRIDION_RUN_ID]
            result = run.status, dict(run.destinations)
            await pilot.press("q")
        return result

    status, destinations = asyncio.run(scenario())

    # Uploaded to the reachable destination, but not known to be on the other.
    assert destinations == {"archive": core.UPLOADED, "offline-archive": core.UNKNOWN}
    assert status == core.UNKNOWN


def test_monitor_mode_uploads_when_asked(config, run_output_dir, destination_dir, fake_rsync, fake_ssh, tui_handler):
    make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id="sample-02")

    async def scenario():
        app = UploaderApp(config["_path"], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            before = {run_id: run.status for run_id, run in app.runs.items()}
            status_line = str(app.query_one("#status").render())
            table = app.query_one("#runs")
            table.move_cursor(row=table.get_row_index(GRIDION_RUN_ID))
            await pilot.press("u")
            await pilot.pause(0.2)
            asked = type(app.screen).__name__
            await pilot.press("y")
            await wait_until(pilot, lambda: app.runs[GRIDION_RUN_ID].status == core.UPLOADED)
            after = {run_id: run.status for run_id, run in app.runs.items()}
            await pilot.press("q")
        return before, status_line, asked, after

    before, status_line, asked, after = asyncio.run(scenario())

    assert before == {GRIDION_RUN_ID: core.ABSENT, GRIDION_RUN_ID_2: core.ABSENT}
    assert "MONITOR" in status_line
    assert "2 runs ready to upload" in status_line
    assert asked == "ConfirmScreen"
    assert after == {GRIDION_RUN_ID: core.UPLOADED, GRIDION_RUN_ID_2: core.ABSENT}
    assert [args[-2].rsplit("/", 1)[-1] for args in fake_rsync.calls()] == [GRIDION_RUN_ID]


def test_switching_to_auto_mode_asks_first(config, run_output_dir, fake_rsync, fake_ssh, tui_handler):
    make_run(run_output_dir, GRIDION_RUN_ID)

    async def scenario():
        app = UploaderApp(config["_path"], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            await pilot.press("a")
            await pilot.pause(0.2)
            asked = type(app.screen).__name__
            await pilot.press("y")
            await wait_until(pilot, lambda: app.runs[GRIDION_RUN_ID].status == core.UPLOADED)
            status_line = str(app.query_one("#status").render())
            # Switching back to monitor mode doesn't need asking.
            await pilot.press("a")
            await pilot.pause(0.1)
            auto_after = app.uploader.auto_upload
            await pilot.press("q")
        return asked, status_line, auto_after

    asked, status_line, auto_after = asyncio.run(scenario())

    assert asked == "ConfirmScreen"
    assert "AUTO" in status_line
    assert auto_after is False
