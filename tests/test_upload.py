import csv
import json
import threading

import pytest

import nanopore_uploader.core as core

from conftest import GRIDION_RUN_ID, make_destination, make_run


@pytest.fixture
def run(config, run_output_dir) -> dict:
    make_run(run_output_dir, GRIDION_RUN_ID)
    [run] = core.find_runs(config)
    return run


def history(config) -> list[dict]:
    with open(config["upload_history_file"], newline='') as f:
        return list(csv.DictReader(f))


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


def test_successful_upload(config, run, destination_dir, fake_rsync, fake_ssh):
    progress = []

    status = core.upload_run(config, run, on_progress=progress.append)

    assert status == "UPLOAD_COMPLETE"
    # upload_complete.json is written in the run's directory on the destination.
    marker = destination_dir / GRIDION_RUN_ID / "upload_complete.json"
    assert fake_ssh.written == [str(marker)]
    assert set(json.load(open(marker))) == {"timestamp_upload_start", "timestamp_upload_complete"}
    # Every progress update is passed on, not just the logged milestones.
    assert [p["percent_complete"] for p in progress] == [25.0, 50.0, 75.0, 100.0]
    assert progress[-1]["server_id"] == "archive"
    assert progress[-1]["upload_speed"] == "10.00MB/s"


def test_rsync_command(config, run, destination_dir, fake_rsync, fake_ssh):
    config["sequencers"][0]["excluded_run_contents"] = ["*.fast5", "other_reports"]
    [run] = core.find_runs(config)

    core.upload_run(config, run)

    [args] = fake_rsync.calls()
    assert args[-2:] == [run["run_directory"], f"uploader@archive.example.org:{destination_dir}"]
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


def test_rsync_failure(config, run, fake_rsync, fake_ssh, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")
    monkeypatch.setenv("FAKE_RSYNC_EXIT_CODE", "255")

    status = core.upload_run(config, run)

    assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
    assert fake_ssh.written == []
    [failed] = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event_type") == "upload_failed"]
    assert failed["error"] == "archive: rsync exited with code 255 (SSH connection failed)"


def test_upload_complete_file_failure(config, run, fake_rsync, fake_ssh):
    fake_ssh.fail_sftp = True

    status = core.upload_run(config, run)

    assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"


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


def test_cancelled_upload(config, run, fake_rsync, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_SLEEP", "30")
    cancel_event = threading.Event()

    def cancel(process):
        cancel_event.set()
        process.terminate()

    status = core.upload_run(config, run, on_process_started=cancel, cancel_event=cancel_event)

    assert status == "UPLOAD_INCOMPLETE"
    assert fake_ssh.written == []


class TestHistory:
    def test_successful_upload_is_recorded(self, config, run, fake_rsync, fake_ssh):
        core.upload_run(config, run)

        [row] = history(config)
        assert row["sequencing_run_id"] == GRIDION_RUN_ID
        assert row["instrument_id"] == "GXB03493"
        assert row["server_id"] == "archive"
        assert row["result"] == "complete"
        assert row["detail"] == ""
        assert row["timestamp_start"] <= row["timestamp_end"]

    def test_failed_upload_is_recorded(self, config, run, fake_rsync, fake_ssh, monkeypatch):
        monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")

        core.upload_run(config, run)

        [row] = history(config)
        assert row["result"] == "failed"
        assert row["detail"] == "rsync exited with code 23 (partial transfer due to error)"

    def test_cancelled_upload_is_recorded(self, config, run, fake_rsync, fake_ssh, monkeypatch):
        monkeypatch.setenv("FAKE_RSYNC_SLEEP", "30")
        cancel_event = threading.Event()

        def cancel(process):
            cancel_event.set()
            process.terminate()

        core.upload_run(config, run, on_process_started=cancel, cancel_event=cancel_event)

        assert [row["result"] for row in history(config)] == ["cancelled"]

    def test_rows_are_appended(self, config, run, fake_rsync, fake_ssh):
        core.upload_run(config, run)
        core.upload_run(config, run)

        assert [row["result"] for row in history(config)] == ["complete", "complete"]

    def test_history_is_optional(self, config, run, fake_rsync, fake_ssh, tmp_path):
        del config["upload_history_file"]

        assert core.upload_run(config, run) == "UPLOAD_COMPLETE"

    def test_history_write_errors_dont_stop_uploads(self, config, run, fake_rsync, fake_ssh, tmp_path, caplog):
        config["upload_history_file"] = str(tmp_path / "missing-dir" / "upload_history.csv")

        status = core.upload_run(config, run)

        assert status == "UPLOAD_COMPLETE"
        failures = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event_type") == "upload_history_write_failed"]
        assert len(failures) == 1


class TestDestinations:
    def test_only_the_runs_own_sequencers_destinations_are_used(self, config, run, fake_rsync, fake_ssh, tmp_path):
        # Issue #4: runs used to be uploaded to every sequencer's destinations.
        other_sequencer_output_dir = tmp_path / "runs" / "promethion"
        other_sequencer_output_dir.mkdir(parents=True)
        config["sequencers"].append({
            "instrument_id": "P2S-01234",
            "instrument_type": "promethion",
            "run_output_dir": str(other_sequencer_output_dir),
            "destination_servers": [make_destination(tmp_path, "promethion-archive")],
        })

        core.upload_run(config, run)

        assert [args[-1].split("@")[1].split(".")[0] for args in fake_rsync.calls()] == ["archive"]

    def test_only_the_given_destinations_are_used(self, config, run, fake_rsync, fake_ssh, tmp_path):
        second = add_second_destination(config, tmp_path)
        [run] = core.find_runs(config)

        core.upload_run(config, run, destinations=[second])

        assert [args[-1].split("@")[1].split(".")[0] for args in fake_rsync.calls()] == ["second-archive"]

    def test_uploaded_to_every_destination(self, config, run, fake_rsync, fake_ssh, tmp_path, caplog):
        # Deployments normally have one destination server, but more than one is supported.
        add_second_destination(config, tmp_path)
        [run] = core.find_runs(config)
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
        [run] = core.find_runs(config)
        # Fail the first destination ("archive.example.org"), but not "second-archive.example.org".
        monkeypatch.setenv("FAKE_RSYNC_FAIL", "@archive.example.org")

        status = core.upload_run(config, run)

        assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
        assert len(fake_rsync.calls()) == 2
        assert [path.split("/destinations/")[1] for path in fake_ssh.written] == [f"second-archive/{GRIDION_RUN_ID}/upload_complete.json"]
        assert [row["result"] for row in history(config)] == ["failed", "complete"]


class TestUploadCompleteFile:
    """
    upload_complete.json is the record of a finished upload, so it must never be left partly written or out of date.
    """

    def test_a_failed_write_leaves_no_marker(self, config, run, destination_dir, fake_rsync, fake_ssh):
        fake_ssh.fail_sftp = True

        status = core.upload_run(config, run)

        assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
        assert sorted(p.name for p in (destination_dir / GRIDION_RUN_ID).iterdir()) == []
        assert core.list_remote_runs(config["sequencers"][0]["destination_servers"][0]) == {GRIDION_RUN_ID: core.PARTIAL}

    def test_a_failed_rename_leaves_no_marker(self, config, run, destination_dir, fake_rsync, fake_ssh):
        fake_ssh.fail_rename = True

        status = core.upload_run(config, run)

        assert status == "UPLOAD_INCOMPLETE_WITH_ERROR"
        assert sorted(p.name for p in (destination_dir / GRIDION_RUN_ID).iterdir()) == []

    def test_uploading_again_replaces_the_marker(self, config, run, destination_dir, fake_rsync, fake_ssh):
        marker = destination_dir / GRIDION_RUN_ID / "upload_complete.json"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"old": true}\n')

        assert core.upload_run(config, run) == "UPLOAD_COMPLETE"

        assert set(json.load(open(marker))) == {"timestamp_upload_start", "timestamp_upload_complete"}

    def test_a_failed_reupload_doesnt_leave_the_old_marker(self, config, run, destination_dir, fake_rsync, fake_ssh, monkeypatch):
        marker = destination_dir / GRIDION_RUN_ID / "upload_complete.json"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"old": true}\n')
        monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")

        assert core.upload_run(config, run) == "UPLOAD_INCOMPLETE_WITH_ERROR"

        # So the run shows as partly uploaded (and is retried), not still uploaded.
        assert not marker.exists()
        assert fake_ssh.removed == [str(marker)]

    def test_rsync_isnt_started_if_the_old_marker_cant_be_removed(self, config, run, fake_rsync, fake_ssh, monkeypatch):
        def cant_remove(destination_server, run_id):
            raise PermissionError("Permission denied (fake)")

        monkeypatch.setattr(core, "remove_upload_complete_file", cant_remove)

        assert core.upload_run(config, run) == "UPLOAD_INCOMPLETE_WITH_ERROR"
        assert fake_rsync.calls() == []


def test_destination_labels_are_unique():
    destinations = [
        {"server_id": "archive", "server_address": "a.example.org", "destination_dir": "/data/gridion"},
        {"server_id": "archive", "server_address": "a.example.org", "destination_dir": "/data/promethion"},
        {"server_address": "b.example.org", "destination_dir": "/data"},
        {"server_address": "b.example.org", "destination_dir": "/data"},  # configured twice
    ]

    assert core.destination_labels(destinations) == [
        "archive:/data/gridion",
        "archive:/data/promethion",
        "b.example.org:/data#3",
        "b.example.org:/data#4",
    ]
    assert core.destination_labels(destinations[:1]) == ["archive"]
