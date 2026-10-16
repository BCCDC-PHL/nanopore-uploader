import json
import threading

import pytest

import nanopore_uploader.core as core

from conftest import GRIDION_RUN_ID, make_destination, make_run, read_db, write_db


@pytest.fixture
def run(config, run_output_dir) -> dict:
    run_dir = make_run(run_output_dir, GRIDION_RUN_ID)
    run = {
        "sequencing_run_id": GRIDION_RUN_ID,
        "protocol_group_id": "PG-01",
        "instrument_type": "GRIDION",
        "instrument_id": "GXB03493",
        "run_directory": str(run_dir),
        "status": "QUEUED_FOR_UPLOAD",
    }
    write_db(config, {GRIDION_RUN_ID: "QUEUED_FOR_UPLOAD"})
    return run


def add_second_destination(config, tmp_path) -> dict:
    destination = make_destination(tmp_path, "second-archive")
    config["sequencers"][0]["destination_servers"].append(destination)
    return destination


@pytest.mark.parametrize("returncode, expected", [
    (23, "rsync exited with code 23 (partial transfer due to error)"),
    (255, "rsync exited with code 255 (SSH connection failed)"),
    (99, "rsync exited with code 99"),
    (-15, "rsync was killed by signal 15"),
])
def test_describe_rsync_returncode(returncode, expected):
    assert core.describe_rsync_returncode(returncode) == expected


def test_successful_upload(config, run, fake_rsync, fake_ssh):
    progress = []

    status = core.upload_run(config, run, on_progress=progress.append)

    assert status == "UPLOAD_COMPLETE"
    assert read_db(config) == {GRIDION_RUN_ID: "UPLOAD_COMPLETE"}
    # upload_complete.json is written in the run's directory on the destination.
    destination_dir = config["sequencers"][0]["destination_servers"][0]["destination_dir"]
    marker = f"{destination_dir}/{GRIDION_RUN_ID}/upload_complete.json"
    assert fake_ssh.written == [marker]
    assert set(json.load(open(marker))) == {"timestamp_upload_start", "timestamp_upload_complete"}
    # Every progress update is passed on, not just the logged milestones.
    assert [p["percent_complete"] for p in progress] == [25.0, 50.0, 75.0, 100.0]
    assert progress[-1]["server_id"] == "archive"
    assert progress[-1]["upload_speed"] == "10.00MB/s"


def test_rsync_command(config, run, fake_rsync, fake_ssh):
    config["sequencers"][0]["excluded_run_contents"] = ["*.fast5", "other_reports"]

    core.upload_run(config, run)

    [args] = fake_rsync.calls()
    destination = config["sequencers"][0]["destination_servers"][0]
    assert args[-2:] == [run["run_directory"], f"uploader@archive.example.org:{destination['destination_dir']}"]
    assert "--chmod=Du=rwx,Dg=rx,Fu=rw,Fg=r,o=" in args
    assert args[args.index("--exclude") + 1] == "*.fast5"
    assert args.count("--exclude") == 2


def test_upload_logs_complete_once(config, run, fake_rsync, fake_ssh, caplog):
    caplog.set_level("INFO")

    core.upload_run(config, run)

    events = [r.msg["event_type"] for r in caplog.records if isinstance(r.msg, dict)]
    assert events.count("destination_upload_complete") == 1
    assert events.count("upload_complete") == 1
    assert "upload_failed" not in events


def test_rsync_failure_marks_run_incomplete_with_error(config, run, fake_rsync, fake_ssh, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")
    monkeypatch.setenv("FAKE_RSYNC_EXIT_CODE", "255")

    status = core.upload_run(config, run)

    assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
    assert read_db(config) == {GRIDION_RUN_ID: "UPLOAD_INCOMPLETE_WITH_ERROR"}
    assert fake_ssh.written == []
    [failed] = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event_type") == "upload_failed"]
    assert failed["error"] == "archive: rsync exited with code 255 (SSH connection failed)"


def test_upload_complete_file_failure_marks_run_incomplete_with_error(config, run, fake_rsync, fake_ssh):
    fake_ssh.fail = True

    status = core.upload_run(config, run)

    assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
    assert read_db(config) == {GRIDION_RUN_ID: "UPLOAD_INCOMPLETE_WITH_ERROR"}


def test_unexpected_rsync_output_is_ignored(config, run, fake_rsync, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_GARBAGE", "1")
    progress = []

    status = core.upload_run(config, run, on_progress=progress.append)

    assert status == "UPLOAD_COMPLETE"
    assert [p["percent_complete"] for p in progress] == [25.0, 50.0, 75.0, 100.0]


def test_rsync_is_stopped_if_reading_its_output_fails(config, run, fake_rsync, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_SLEEP", "30")
    processes = []

    def broken_progress_callback(progress):
        raise RuntimeError("bug in progress handling")

    status = core.upload_run(config, run, on_progress=broken_progress_callback, on_process_started=processes.append)

    assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
    [process] = processes
    assert process.poll() is not None


def test_cancelled_upload_marks_run_incomplete(config, run, fake_rsync, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_SLEEP", "30")
    cancel_event = threading.Event()

    def cancel(process):
        cancel_event.set()
        process.terminate()

    status = core.upload_run(config, run, on_process_started=cancel, cancel_event=cancel_event)

    assert status == "UPLOAD_INCOMPLETE"
    assert read_db(config) == {GRIDION_RUN_ID: "UPLOAD_INCOMPLETE"}
    assert fake_ssh.written == []


class TestMultipleDestinations:
    """
    Deployments normally have one destination server, but more than one is supported.
    """

    def test_uploaded_to_every_destination(self, config, run, fake_rsync, fake_ssh, tmp_path, caplog):
        add_second_destination(config, tmp_path)
        caplog.set_level("INFO")

        status = core.upload_run(config, run)

        assert status == "UPLOAD_COMPLETE"
        assert len(fake_rsync.calls()) == 2
        assert len(fake_ssh.written) == 2
        events = [r.msg["event_type"] for r in caplog.records if isinstance(r.msg, dict)]
        assert events.count("destination_upload_complete") == 2
        assert events.count("upload_complete") == 1

    def test_one_failure_doesnt_stop_the_others(self, config, run, fake_rsync, fake_ssh, tmp_path, monkeypatch):
        add_second_destination(config, tmp_path)
        # Fail the first destination ("archive.example.org"), but not "second-archive.example.org".
        monkeypatch.setenv("FAKE_RSYNC_FAIL", "@archive.example.org")

        status = core.upload_run(config, run)

        assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
        assert len(fake_rsync.calls()) == 2
        assert [path.split("/destinations/")[1] for path in fake_ssh.written] == [f"second-archive/{GRIDION_RUN_ID}/upload_complete.json"]
