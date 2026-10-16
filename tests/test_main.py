"""
Tests of the command line: setting up the upload db, and refusing to start without it.
"""
import json

import pytest

from nanopore_uploader.__main__ import main

from conftest import GRIDION_RUN_ID, read_db, write_db


@pytest.fixture
def config_file(tmp_path) -> str:
    config = {
        "sequencers": [],
        "db": {"file": str(tmp_path / "uploads.csv")},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return str(path)


def test_init_db_creates_the_db_and_exits(config_file, tmp_path, capsys):
    main(["-c", config_file, "--init-db"])

    assert read_db({"db": {"file": str(tmp_path / "uploads.csv")}}) == {}
    assert f"Created upload db file: {tmp_path / 'uploads.csv'}" in capsys.readouterr().out


def test_init_db_wont_replace_an_existing_db(config_file, tmp_path):
    config = {"db": {"file": str(tmp_path / "uploads.csv")}}
    write_db(config, {GRIDION_RUN_ID: "UPLOAD_COMPLETE"})

    with pytest.raises(SystemExit, match="already exists"):
        main(["-c", config_file, "--init-db"])

    assert read_db(config) == {GRIDION_RUN_ID: "UPLOAD_COMPLETE"}


def test_init_db_needs_the_directory_to_exist(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sequencers": [], "db": {"file": str(tmp_path / "missing" / "uploads.csv")}}))

    with pytest.raises(SystemExit, match="directory doesn't exist"):
        main(["-c", str(config_path), "--init-db"])


@pytest.mark.parametrize("mode", [[], ["--tui"]])
def test_wont_start_without_the_db(config_file, mode, monkeypatch):
    # If the check were missing, fail rather than run the uploader (which runs until it's stopped).
    def started(*args, **kwargs):
        raise AssertionError("started without an upload db")

    monkeypatch.setattr("nanopore_uploader.service.Uploader.run", started)
    monkeypatch.setattr("nanopore_uploader.tui.app.UploaderApp.run", started)

    with pytest.raises(SystemExit, match="--init-db"):
        main(["-c", config_file, *mode])
