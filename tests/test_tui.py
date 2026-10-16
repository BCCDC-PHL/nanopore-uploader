"""
End-to-end tests of the TUI, using Textual's test pilot (no real terminal needed).
"""
import asyncio
import logging

import pytest

pytest.importorskip("textual")

from nanopore_uploader.tui.app import EXCLUDED, TUILogHandler, UploaderApp

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, make_run, read_db, write_db


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


async def wait_until(pilot, condition, timeout: float = 10.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting for condition")


def test_runs_table_follows_uploads(config, run_output_dir, fake_rsync, fake_ssh, tui_handler):
    make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id="sample-02")
    write_db(config, {GRIDION_RUN_ID_2: 'UPLOAD_COMPLETE'})
    excluded_run_id = "20261001_0000_X1_FAZ00000_00000000"
    with open(config['excluded_runs_list'], 'w') as f:
        f.write(excluded_run_id + "\n")
    config['excluded_runs'] = [excluded_run_id]

    async def scenario():
        app = UploaderApp(config['_path'], config, tui_handler)
        async with app.run_test(size=(150, 40)) as pilot:
            await wait_until(pilot, lambda: app.state == "idle")
            statuses = {run_id: run.status for run_id, run in app.runs.items()}
            table_rows = app.query_one("#runs").row_count
            next_scan_at = app.next_scan_at
            await pilot.press("q")
            await wait_until(pilot, lambda: not app.is_running)
        return statuses, table_rows, next_scan_at

    statuses, table_rows, next_scan_at = asyncio.run(scenario())

    assert statuses == {
        GRIDION_RUN_ID: 'UPLOAD_COMPLETE',
        GRIDION_RUN_ID_2: 'UPLOAD_COMPLETE',
        excluded_run_id: EXCLUDED,
    }
    assert table_rows == 3
    assert next_scan_at is not None
    assert read_db(config) == {GRIDION_RUN_ID_2: 'UPLOAD_COMPLETE', GRIDION_RUN_ID: 'UPLOAD_COMPLETE'}


def test_failed_upload_shows_the_error(config, run_output_dir, fake_rsync, fake_ssh, tui_handler, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
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

    assert status == 'UPLOAD_FAILED'
    assert "rsync exited with code 255 (SSH connection failed)" in last_error
    # rsync's own error message, captured from its stderr.
    assert "rsync: fake failure" in last_error


def test_quitting_during_an_upload_asks_first(config, run_output_dir, fake_rsync, fake_ssh, tui_handler, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
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
    assert read_db(config) == {GRIDION_RUN_ID: 'UPLOAD_INCOMPLETE'}
