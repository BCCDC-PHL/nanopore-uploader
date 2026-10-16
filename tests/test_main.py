"""
Tests of the command line.
"""
import json

import pytest

from nanopore_uploader.__main__ import main


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
