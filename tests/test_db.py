import csv
import os
import stat

import pytest

import nanopore_uploader.db as db

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, read_db, write_db


@pytest.fixture
def new_db_config(tmp_path) -> dict:
    return {"db": {"file": str(tmp_path / "uploads.csv")}}


def test_init_db_file_creates_an_empty_db(new_db_config):
    db.init_db_file(new_db_config)

    assert open(new_db_config["db"]["file"]).read().splitlines() == ["sequencing_run_id,status"]
    assert read_db(new_db_config) == {}


def test_init_db_file_never_overwrites(new_db_config):
    write_db(new_db_config, {GRIDION_RUN_ID: "UPLOAD_COMPLETE"})

    with pytest.raises(FileExistsError):
        db.init_db_file(new_db_config)

    assert read_db(new_db_config) == {GRIDION_RUN_ID: "UPLOAD_COMPLETE"}


def test_init_db_file_doesnt_create_directories(tmp_path):
    # A missing directory more likely means a typo or an unmounted share than a new installation.
    config = {"db": {"file": str(tmp_path / "not-mounted" / "uploads.csv")}}

    with pytest.raises(FileNotFoundError):
        db.init_db_file(config)

    assert not (tmp_path / "not-mounted").exists()


def test_check_db_file(new_db_config):
    with pytest.raises(FileNotFoundError, match="--init-db"):
        db.check_db_file(new_db_config)

    db.init_db_file(new_db_config)
    db.check_db_file(new_db_config)


def test_missing_db_setting_is_a_clear_error():
    with pytest.raises(ValueError, match="No upload db file is set"):
        db.check_db_file({})


def test_add_and_update_runs(new_db_config):
    db.init_db_file(new_db_config)

    db.store_run(new_db_config, {"sequencing_run_id": GRIDION_RUN_ID, "status": "QUEUED_FOR_UPLOAD"})
    db.store_run(new_db_config, {"sequencing_run_id": GRIDION_RUN_ID_2, "status": "QUEUED_FOR_UPLOAD"})
    db.store_run(new_db_config, {"sequencing_run_id": GRIDION_RUN_ID, "status": "IGNORED_BECAUSE_ALREADY_STORED"})
    db.update_run_status_by_id(new_db_config, GRIDION_RUN_ID, "upload_complete")

    assert read_db(new_db_config) == {GRIDION_RUN_ID: "UPLOAD_COMPLETE", GRIDION_RUN_ID_2: "QUEUED_FOR_UPLOAD"}
    assert db.get_run_by_id(new_db_config, GRIDION_RUN_ID_2) == {"sequencing_run_id": GRIDION_RUN_ID_2, "status": "QUEUED_FOR_UPLOAD"}
    assert db.get_run_by_id(new_db_config, "not-a-run") is None


def test_failed_write_leaves_the_db_unchanged(new_db_config, tmp_path, monkeypatch):
    write_db(new_db_config, {GRIDION_RUN_ID: "UPLOAD_COMPLETE", GRIDION_RUN_ID_2: "QUEUED_FOR_UPLOAD"})
    before = open(new_db_config["db"]["file"]).read()

    def fail_part_way(self, rows):
        self.writerow(next(iter(rows)))
        raise OSError("disk full")

    monkeypatch.setattr(csv.DictWriter, "writerows", fail_part_way)

    with pytest.raises(OSError, match="disk full"):
        db.update_run_status_by_id(new_db_config, GRIDION_RUN_ID_2, "UPLOAD_STARTED")

    assert open(new_db_config["db"]["file"]).read() == before
    # The temporary file is cleaned up.
    assert sorted(os.listdir(tmp_path)) == ["uploads.csv"]


def test_writes_keep_the_db_file_permissions(new_db_config):
    db.init_db_file(new_db_config)
    db_file = new_db_config["db"]["file"]
    os.chmod(db_file, 0o664)

    db.store_run(new_db_config, {"sequencing_run_id": GRIDION_RUN_ID, "status": "QUEUED_FOR_UPLOAD"})

    assert stat.S_IMODE(os.stat(db_file).st_mode) == 0o664
