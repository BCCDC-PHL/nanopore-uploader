"""
Tests of the command line.
"""
import json

import pytest

import nanopore_uploader.core as core
from nanopore_uploader.__main__ import main

from conftest import GRIDION_RUN_ID, make_destination, make_run, mark_uploaded, save_config


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """
    Stub out the uploader loop and the TUI (which run until they're stopped), recording which one started.
    """
    calls = []
    monkeypatch.setattr("nanopore_uploader.service.Uploader.run", lambda self: calls.append("uploader"))
    monkeypatch.setattr("nanopore_uploader.tui.app.UploaderApp.run", lambda self: calls.append("tui"))
    return calls


def write_config(tmp_path, **settings) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sequencers": [], **settings}))
    return str(path)


@pytest.mark.parametrize("mode, expected", [([], "uploader"), (["--tui"], "tui")])
def test_starts_without_a_db_file(tmp_path, started, mode, expected):
    # Upload state comes from the destinations, so there's no db to set up first.
    main(["-c", write_config(tmp_path), *mode])

    assert started == [expected]


def test_old_db_setting_is_noted_but_ignored(tmp_path, started, caplog):
    caplog.set_level("INFO")

    main(["-c", write_config(tmp_path, db={"file": str(tmp_path / "uploads.csv")})])

    assert started == ["uploader"]
    notes = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event_type") == "config_setting_unused"]
    assert [note["setting"] for note in notes] == ["db"]
    assert not (tmp_path / "uploads.csv").exists()


class TestUploadOneRun:
    def test_uploads_and_exits(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, started, capsys):
        make_run(run_output_dir, GRIDION_RUN_ID)

        main(["-c", config["_path"], "--upload", GRIDION_RUN_ID])

        assert f"Uploaded {GRIDION_RUN_ID}" in capsys.readouterr().out
        assert (destination_dir / GRIDION_RUN_ID / core.UPLOAD_COMPLETE_FILENAME).exists()
        assert started == []  # the scan loop isn't started

    def test_already_uploaded(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, started, capsys):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        main(["-c", config["_path"], "--upload", GRIDION_RUN_ID])

        assert "already uploaded" in capsys.readouterr().out
        assert fake_rsync.calls() == []

    def test_force(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, started):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        main(["-c", config["_path"], "--upload", GRIDION_RUN_ID, "--force"])

        assert len(fake_rsync.calls()) == 1

    def test_failure_exits_with_an_error(self, config, run_output_dir, fake_rsync, fake_ssh, started, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        monkeypatch.setenv("FAKE_RSYNC_FAIL", "archive.example.org")

        with pytest.raises(SystemExit, match="failed"):
            main(["-c", config["_path"], "--upload", GRIDION_RUN_ID])

    def test_a_destination_that_cant_be_checked_isnt_success(self, config, run_output_dir, tmp_path, fake_rsync, fake_ssh, started):
        make_run(run_output_dir, GRIDION_RUN_ID)
        offline = make_destination(tmp_path, "offline-archive")
        offline["destination_dir"] = str(tmp_path / "not-mounted")
        config["sequencers"][0]["destination_servers"].append(offline)
        save_config(config)

        with pytest.raises(SystemExit, match="couldn't be"):
            main(["-c", config["_path"], "--upload", GRIDION_RUN_ID])

        assert len(fake_rsync.calls()) == 1

    def test_unknown_run(self, config, fake_rsync, fake_ssh, started):
        with pytest.raises(SystemExit, match="No complete run"):
            main(["-c", config["_path"], "--upload", GRIDION_RUN_ID])

    def test_force_needs_upload(self, config, started):
        with pytest.raises(SystemExit):
            main(["-c", config["_path"], "--force"])
        assert started == []
