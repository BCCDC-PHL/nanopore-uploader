import threading
import time

import pytest

import nanopore_uploader.core as core
from nanopore_uploader.service import Uploader

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, make_run, read_db, save_config, write_db


def make_uploader(config, **kwargs) -> Uploader:
    return Uploader(config['_path'], config, **kwargs)


def scan_once(uploader: Uploader):
    """
    One pass of the loop in Uploader.run(), without waiting for the next scan.
    """
    uploader._handle_requests()
    uploader._scan_and_upload()


def events(caplog, event_type: str) -> list[dict]:
    return [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get('event_type') == event_type]


def wait_for(condition, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(0.02)


@pytest.fixture
def uploads(fake_rsync):
    """
    The IDs of the runs rsync was asked to upload, in order.
    """
    return lambda: [args[-2].rsplit('/', 1)[-1] for args in fake_rsync.calls()]


def test_new_run_is_uploaded_once(config, run_output_dir, fake_rsync, fake_ssh, uploads):
    make_run(run_output_dir, GRIDION_RUN_ID)
    uploader = make_uploader(config)

    scan_once(uploader)
    scan_once(uploader)

    assert uploads() == [GRIDION_RUN_ID]
    assert read_db(config) == {GRIDION_RUN_ID: 'UPLOAD_COMPLETE'}


@pytest.mark.parametrize("previous_status", ['UPLOAD_STARTED', 'UPLOAD_INCOMPLETE', 'UPLOAD_INCOMPLETE_WITH_ERROR'])
def test_unfinished_uploads_are_resumed(config, run_output_dir, fake_rsync, fake_ssh, uploads, previous_status, caplog):
    # eg. UPLOAD_STARTED is what's left if the uploader crashed or lost power mid-upload.
    make_run(run_output_dir, GRIDION_RUN_ID)
    write_db(config, {GRIDION_RUN_ID: previous_status})
    caplog.set_level('INFO')

    scan_once(make_uploader(config))

    assert uploads() == [GRIDION_RUN_ID]
    assert read_db(config) == {GRIDION_RUN_ID: 'UPLOAD_COMPLETE'}
    [retrying] = events(caplog, 'upload_retrying')
    assert retrying['previous_status'] == previous_status


def test_failed_uploads_are_retried_up_to_max_attempts(config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch, caplog):
    make_run(run_output_dir, GRIDION_RUN_ID)
    config['max_upload_attempts'] = 2
    save_config(config)
    monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
    uploader = make_uploader(config)

    for _ in range(4):
        scan_once(uploader)

    assert uploads() == [GRIDION_RUN_ID] * 2
    assert read_db(config) == {GRIDION_RUN_ID: 'UPLOAD_INCOMPLETE_WITH_ERROR'}
    # Giving up is only logged once, not on every scan.
    assert len(events(caplog, 'upload_retries_exhausted')) == 1


def test_requeue_resets_attempts(config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
    config['max_upload_attempts'] = 2
    save_config(config)
    monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
    uploader = make_uploader(config)
    for _ in range(3):
        scan_once(uploader)
    assert uploads() == [GRIDION_RUN_ID] * 2  # attempts used up

    uploader.requeue(GRIDION_RUN_ID)
    for _ in range(3):
        scan_once(uploader)

    # The re-queued upload, then one retry: two more attempts, as if it had just been found.
    assert uploads() == [GRIDION_RUN_ID] * 4


def test_requeue_uploads_a_complete_run_again(config, run_output_dir, fake_rsync, fake_ssh, uploads):
    make_run(run_output_dir, GRIDION_RUN_ID)
    uploader = make_uploader(config)
    scan_once(uploader)

    uploader.requeue(GRIDION_RUN_ID)
    scan_once(uploader)

    assert uploads() == [GRIDION_RUN_ID] * 2


def test_run_excluded_mid_scan_is_not_uploaded(config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
    make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
    uploader = make_uploader(config)
    upload_run = core.upload_run

    def upload_run_and_exclude_the_other_run(config, run, **kwargs):
        # Someone excludes the other run while this one is uploading.
        other = GRIDION_RUN_ID_2 if run['sequencing_run_id'] == GRIDION_RUN_ID else GRIDION_RUN_ID
        uploader.exclude(other)
        return upload_run(config, run, **kwargs)

    monkeypatch.setattr(core, 'upload_run', upload_run_and_exclude_the_other_run)

    scan_once(uploader)

    assert len(uploads()) == 1
    [excluded] = open(config['excluded_runs_list']).read().split()
    assert excluded != uploads()[0]


def test_exclude_appends_to_excluded_runs_list(config):
    with open(config['excluded_runs_list'], 'w') as f:
        f.write('20261001_0000_X1_FAZ00000_00000000')  # no trailing newline
    uploader = make_uploader(config)

    uploader.exclude(GRIDION_RUN_ID)
    uploader.exclude(GRIDION_RUN_ID)
    uploader._handle_requests()

    assert open(config['excluded_runs_list']).read().splitlines() == ['20261001_0000_X1_FAZ00000_00000000', GRIDION_RUN_ID]
    assert GRIDION_RUN_ID in uploader.config['excluded_runs']


def test_exclude_without_excluded_runs_list_is_a_warning(config, caplog):
    del config['excluded_runs_list']
    uploader = make_uploader(config)

    assert not uploader.can_exclude()
    uploader.exclude(GRIDION_RUN_ID)
    uploader._handle_requests()

    assert len(events(caplog, 'exclude_failed')) == 1


def test_exclude_is_saved_promptly_while_idle(config, caplog):
    # Waiting for the next scan could take an hour.
    caplog.set_level('INFO')
    uploader = make_uploader(config)
    thread = threading.Thread(target=uploader.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: events(caplog, 'scan_complete'))
        uploader.exclude(GRIDION_RUN_ID)
        wait_for(lambda: GRIDION_RUN_ID in open(config['excluded_runs_list']).read(), timeout=5)
    finally:
        uploader.stop()
        thread.join(10)


def test_requests_made_just_before_stopping_are_kept(config):
    uploader = make_uploader(config)
    uploader.exclude(GRIDION_RUN_ID)
    uploader.stop()

    uploader.run()

    assert open(config['excluded_runs_list']).read().split() == [GRIDION_RUN_ID]


def test_scan_errors_dont_stop_the_loop(config, monkeypatch, caplog):
    def broken_scan(config):
        raise RuntimeError("scan is broken")
        yield

    monkeypatch.setattr(core, 'scan', broken_scan)
    config['scan_interval_seconds'] = 0.01
    uploader = make_uploader(config)
    thread = threading.Thread(target=uploader.run, daemon=True)
    thread.start()

    try:
        wait_for(lambda: len(events(caplog, 'scan_failed')) >= 3)
    finally:
        uploader.stop()
        thread.join(10)

    assert not thread.is_alive()
    assert events(caplog, 'scan_failed')[0]['error'] == "scan is broken"


def test_stop_ends_an_upload_and_marks_it_incomplete(config, run_output_dir, fake_rsync, fake_ssh, monkeypatch):
    make_run(run_output_dir, GRIDION_RUN_ID)
    monkeypatch.setenv('FAKE_RSYNC_SLEEP', '30')
    uploader = make_uploader(config)
    thread = threading.Thread(target=uploader.run, daemon=True)
    thread.start()
    wait_for(lambda: uploader._rsync_process is not None)

    started = time.monotonic()
    uploader.stop()
    thread.join(10)

    assert not thread.is_alive()
    assert time.monotonic() - started < 10
    assert read_db(config) == {GRIDION_RUN_ID: 'UPLOAD_INCOMPLETE'}
    assert uploader.uploading_run_id is None
