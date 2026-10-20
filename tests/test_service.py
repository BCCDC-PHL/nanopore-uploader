import copy
import threading
import time

from pathlib import Path

import pytest

import nanopore_uploader.core as core
from nanopore_uploader.service import SOME_DESTINATIONS_UNCHECKED, Uploader

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, make_destination, make_run, mark_partial, mark_uploaded, save_config


def make_uploader(config, auto_upload: bool = True, **kwargs) -> Uploader:
    """
    Most tests here are about what gets uploaded automatically, so auto mode is on unless asked otherwise.
    """
    config['auto_upload'] = auto_upload
    save_config(config)
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


class TestWhatGetsUploaded:
    """
    Whether a run is uploaded depends only on what's at the destination (and the excluded runs list).
    """

    def test_new_run_is_uploaded_once(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config)

        scan_once(uploader)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]
        assert (destination_dir / GRIDION_RUN_ID / 'upload_complete.json').exists()

    def test_run_already_at_the_destination_isnt_uploaded(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, caplog):
        # eg. uploaded by an earlier version, by hand, or before the uploader was restarted.
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        caplog.set_level('DEBUG')

        scan_once(make_uploader(config))

        assert uploads() == []
        [checked] = events(caplog, 'run_checked')
        assert checked['state'] == core.UPLOADED
        assert checked['destinations'] == {'archive': core.UPLOADED}

    def test_partly_uploaded_run_is_resumed(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        # eg. the uploader crashed, lost power or was stopped mid-upload.
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_partial(destination_dir, GRIDION_RUN_ID)

        scan_once(make_uploader(config))

        assert uploads() == [GRIDION_RUN_ID]
        assert (destination_dir / GRIDION_RUN_ID / 'upload_complete.json').exists()

    def test_nothing_is_uploaded_if_the_destination_cant_be_checked(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, caplog):
        make_run(run_output_dir, GRIDION_RUN_ID)
        destination_dir.rename(destination_dir.with_name('away'))  # eg. a share that isn't mounted

        scan_once(make_uploader(config))

        assert uploads() == []
        [failed] = events(caplog, 'remote_listing_failed')
        assert failed['server_id'] == 'archive'

    def test_nothing_is_uploaded_if_the_server_cant_be_reached(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, caplog):
        make_run(run_output_dir, GRIDION_RUN_ID)
        fake_ssh.fail = True

        scan_once(make_uploader(config))

        assert uploads() == []
        assert len(events(caplog, 'remote_listing_failed')) == 1

    def test_nothing_is_uploaded_if_the_listing_times_out(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, caplog):
        make_run(run_output_dir, GRIDION_RUN_ID)
        fake_ssh.hang = True

        scan_once(make_uploader(config))

        assert uploads() == []
        [failed] = events(caplog, 'remote_listing_failed')
        assert failed['error_type'] == 'TimeoutError'

    def test_each_destination_is_listed_once_per_scan(self, config, run_output_dir, fake_rsync, fake_ssh):
        make_run(run_output_dir, GRIDION_RUN_ID)
        make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
        uploader = make_uploader(config)

        scan_once(uploader)
        scan_once(uploader)

        listings = [command for command in fake_ssh.commands if 'find' in command]
        assert len(listings) == 2

    def test_no_runs_means_no_listing(self, config, fake_rsync, fake_ssh):
        scan_once(make_uploader(config))

        assert fake_ssh.commands == []

    def test_excluded_run_isnt_uploaded(self, config, run_output_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        with open(config['excluded_runs_list'], 'w') as f:
            f.write(GRIDION_RUN_ID + '\n')

        scan_once(make_uploader(config))

        assert uploads() == []


class TestConfigChanges:
    def test_config_changes_apply_to_the_rest_of_the_scan(self, config, run_output_dir, tmp_path, fake_rsync, fake_ssh, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
        uploader = make_uploader(config)
        new_destination = make_destination(tmp_path, 'new-archive')
        upload_run = core.upload_run

        def upload_run_then_change_destination(config_, run, **kwargs):
            # Someone edits the config file to change the sequencer's destination while the first run is uploading.
            result = upload_run(config_, run, **kwargs)
            edited = copy.deepcopy(config)
            edited['sequencers'][0]['destination_servers'] = [new_destination]
            save_config(edited)
            return result

        monkeypatch.setattr(core, 'upload_run', upload_run_then_change_destination)

        scan_once(uploader)

        assert [args[-1].split('@')[1].split('.')[0] for args in fake_rsync.calls()] == ['archive', 'new-archive']

    def test_runs_from_a_removed_sequencer_are_skipped(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, caplog):
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config)
        config['sequencers'][0]['run_output_dir'] = str(run_output_dir.parent / 'elsewhere')
        save_config(config)

        # The run list comes from the config the scan started with; the run is found, but its sequencer has gone.
        uploader.config['sequencers'][0]['run_output_dir'] = str(run_output_dir)
        scan_once(uploader)

        assert uploads() == []
        assert events(caplog, 'run_skipped')[0]['sequencing_run_id'] == GRIDION_RUN_ID


class TestMultipleDestinations:
    """
    Deployments normally have one destination server, but more than one is supported.
    """

    def test_only_uploaded_where_needed(self, config, run_output_dir, destination_dir, tmp_path, fake_rsync, fake_ssh):
        second = make_destination(tmp_path, 'second-archive')
        config['sequencers'][0]['destination_servers'].append(second)
        save_config(config)
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        scan_once(make_uploader(config))

        assert [args[-1].split('@')[1].split('.')[0] for args in fake_rsync.calls()] == ['second-archive']

    def test_reachable_destinations_are_uploaded_to_when_another_cant_be_checked(self, config, run_output_dir, tmp_path, fake_rsync, fake_ssh):
        second = make_destination(tmp_path, 'second-archive')
        second['destination_dir'] = str(tmp_path / 'not-mounted')
        config['sequencers'][0]['destination_servers'].append(second)
        save_config(config)
        make_run(run_output_dir, GRIDION_RUN_ID)

        scan_once(make_uploader(config))

        assert [args[-1].split('@')[1].split('.')[0] for args in fake_rsync.calls()] == ['archive']


class TestSharedServerIds:
    """
    Destinations with the same server_id (eg. different directories on one server) are kept apart.
    """

    @pytest.fixture
    def second_destination(self, config, tmp_path):
        second = make_destination(tmp_path, 'archive-2')
        second['server_id'] = 'archive'
        second['server_address'] = 'archive.example.org'
        config['sequencers'][0]['destination_servers'].append(second)
        save_config(config)
        return second

    def test_unknown_destination_isnt_uploaded_to(self, config, run_output_dir, destination_dir, second_destination, fake_rsync, fake_ssh):
        # The first destination can't be checked; the second, with the same server_id, can.
        make_run(run_output_dir, GRIDION_RUN_ID)
        destination_dir.rename(destination_dir.with_name('away'))

        scan_once(make_uploader(config))

        assert [args[-1].split(':', 1)[1] for args in fake_rsync.calls()] == [second_destination['destination_dir']]

    def test_uploaded_destination_isnt_uploaded_to_again(self, config, run_output_dir, second_destination, fake_rsync, fake_ssh, caplog):
        caplog.set_level('DEBUG')
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(Path(second_destination['destination_dir']), GRIDION_RUN_ID)

        scan_once(make_uploader(config))

        assert [args[-1].split(':', 1)[1] for args in fake_rsync.calls()] == [config['sequencers'][0]['destination_servers'][0]['destination_dir']]
        [checked] = events(caplog, 'run_checked')
        assert sorted(checked['destinations'].values()) == [core.ABSENT, core.UPLOADED]


class TestRetries:
    def test_failed_uploads_are_retried_up_to_max_attempts(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch, caplog):
        make_run(run_output_dir, GRIDION_RUN_ID)
        config['max_upload_attempts'] = 2
        save_config(config)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
        caplog.set_level('INFO')
        uploader = make_uploader(config)

        for _ in range(4):
            scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID] * 2
        assert len(events(caplog, 'upload_retrying')) == 1
        # Giving up is only logged once, not on every scan.
        assert len(events(caplog, 'upload_retries_exhausted')) == 1

    def test_attempts_reset_after_a_successful_upload(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        config['max_upload_attempts'] = 2
        save_config(config)
        uploader = make_uploader(config)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
        scan_once(uploader)
        monkeypatch.delenv('FAKE_RSYNC_FAIL')
        scan_once(uploader)
        assert uploader._upload_attempts == {}

    def test_upload_request_resets_attempts(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        config['max_upload_attempts'] = 2
        save_config(config)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
        uploader = make_uploader(config)
        for _ in range(3):
            scan_once(uploader)
        assert uploads() == [GRIDION_RUN_ID] * 2  # attempts used up

        uploader.upload(GRIDION_RUN_ID)
        for _ in range(3):
            scan_once(uploader)

        # The re-queued upload, then one retry: two more attempts, as if it had just been found.
        assert uploads() == [GRIDION_RUN_ID] * 4

    def test_forced_upload_uploads_an_uploaded_run_again_once(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config)

        uploader.upload(GRIDION_RUN_ID, force=True)
        scan_once(uploader)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]

    def test_failed_reupload_is_retried(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
        # A failed re-upload mustn't leave the run looking uploaded (by its old upload_complete.json).
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config)
        uploader.upload(GRIDION_RUN_ID, force=True)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
        scan_once(uploader)

        monkeypatch.delenv('FAKE_RSYNC_FAIL')
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID] * 2
        assert (destination_dir / GRIDION_RUN_ID / 'upload_complete.json').exists()

    def test_forced_upload_waits_until_the_destination_can_be_checked(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config)
        uploader.upload(GRIDION_RUN_ID, force=True)
        fake_ssh.fail = True
        scan_once(uploader)
        assert uploads() == []

        fake_ssh.fail = False
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]


class TestMonitorMode:
    """
    The default: runs are only uploaded when someone asks.
    """

    def test_is_the_default(self, config):
        config.pop('auto_upload', None)
        assert not Uploader(config['_path'], config).auto_upload

    @pytest.mark.parametrize("setting, expected", [(True, True), (False, False), ("true", True), ("false", False), ("no", False)])
    def test_auto_upload_setting(self, config, setting, expected):
        config['auto_upload'] = setting
        assert Uploader(config['_path'], config).auto_upload == expected

    def test_nothing_is_uploaded_without_a_request(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, caplog):
        caplog.set_level('INFO')
        make_run(run_output_dir, GRIDION_RUN_ID)
        make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
        mark_partial(destination_dir, GRIDION_RUN_ID_2)
        uploader = make_uploader(config, auto_upload=False)

        scan_once(uploader)
        scan_once(uploader)

        assert uploads() == []
        awaiting = events(caplog, 'runs_awaiting_upload')
        assert len(awaiting) == 2
        assert sorted(awaiting[-1]['sequencing_run_ids']) == [GRIDION_RUN_ID, GRIDION_RUN_ID_2]

    def test_uploaded_runs_arent_reported_as_awaiting_upload(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, caplog):
        caplog.set_level('INFO')
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        scan_once(make_uploader(config, auto_upload=False))

        assert events(caplog, 'runs_awaiting_upload') == []

    def test_requested_upload(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID)
        scan_once(uploader)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]
        assert (destination_dir / GRIDION_RUN_ID / 'upload_complete.json').exists()

    def test_requested_upload_of_an_uploaded_run_isnt_needed(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads, caplog):
        caplog.set_level('INFO')
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID)
        scan_once(uploader)

        assert uploads() == []
        assert len(events(caplog, 'upload_not_needed')) == 1

    def test_forced_upload_of_an_uploaded_run(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID, force=True)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]

    def test_a_plain_request_doesnt_undo_a_forced_one(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID, force=True)
        uploader.upload(GRIDION_RUN_ID)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]

    def test_requested_upload_waits_until_the_destination_can_be_checked(self, config, run_output_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)
        uploader.upload(GRIDION_RUN_ID)
        fake_ssh.fail = True
        scan_once(uploader)
        assert uploads() == []

        fake_ssh.fail = False
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]

    def test_failed_requested_upload_isnt_retried(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID)
        for _ in range(3):
            scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]

    def test_switching_to_auto_mode(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, caplog):
        caplog.set_level('INFO')
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)
        scan_once(uploader)
        assert uploads() == []

        uploader.set_auto_upload(True)
        scan_once(uploader)

        assert uploads() == [GRIDION_RUN_ID]
        assert uploader._wake_event.is_set()  # switching to auto mode starts a scan
        assert events(caplog, 'upload_mode')[-1]['auto_upload'] is True

    def test_switching_to_monitor_mode_lasts_through_config_reloads(self, config, run_output_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        make_run(run_output_dir, GRIDION_RUN_ID_2, sample_id='sample-02')
        uploader = make_uploader(config, auto_upload=True)

        uploader.set_auto_upload(False)
        scan_once(uploader)

        assert uploads() == []


class TestRequestsWithAnUncheckedDestination:
    """
    A requested upload is kept until every destination has been dealt with, including ones that can't be
    checked at first, without uploading to any destination twice.
    """

    @pytest.fixture
    def offline(self, config, tmp_path):
        offline = make_destination(tmp_path, 'offline-archive')
        offline_dir = Path(offline['destination_dir'])
        offline_dir.rmdir()  # can't be listed until it's back
        config['sequencers'][0]['destination_servers'].append(offline)
        save_config(config)
        return offline_dir

    @staticmethod
    def uploaded_to(fake_rsync):
        return [args[-1].split('@')[1].split('.')[0] for args in fake_rsync.calls()]

    def test_request_waits_for_the_unchecked_destination(self, config, run_output_dir, offline, fake_rsync, fake_ssh, caplog):
        caplog.set_level('INFO')
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID)
        scan_once(uploader)
        assert self.uploaded_to(fake_rsync) == ['archive']
        assert events(caplog, 'upload_request_waiting')[-1]['server_ids'] == ['offline-archive']

        scan_once(uploader)  # still offline
        assert self.uploaded_to(fake_rsync) == ['archive']

        offline.mkdir()
        scan_once(uploader)
        scan_once(uploader)

        assert self.uploaded_to(fake_rsync) == ['archive', 'offline-archive']
        assert GRIDION_RUN_ID not in uploader._upload_requests

    def test_forced_request_doesnt_upload_anywhere_twice(self, config, run_output_dir, destination_dir, offline, fake_rsync, fake_ssh):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)

        uploader.upload(GRIDION_RUN_ID, force=True)
        scan_once(uploader)
        scan_once(uploader)  # still offline: 'archive' isn't rewritten again
        assert self.uploaded_to(fake_rsync) == ['archive']

        offline.mkdir()
        mark_uploaded(offline, GRIDION_RUN_ID)
        scan_once(uploader)
        scan_once(uploader)

        assert self.uploaded_to(fake_rsync) == ['archive', 'offline-archive']

    def test_asking_again_starts_afresh(self, config, run_output_dir, offline, fake_rsync, fake_ssh):
        make_run(run_output_dir, GRIDION_RUN_ID)
        uploader = make_uploader(config, auto_upload=False)
        uploader.upload(GRIDION_RUN_ID, force=True)
        scan_once(uploader)

        uploader.upload(GRIDION_RUN_ID, force=True)
        scan_once(uploader)

        assert self.uploaded_to(fake_rsync) == ['archive', 'archive']

    def test_upload_now_doesnt_report_success(self, config, run_output_dir, offline, fake_rsync, fake_ssh):
        make_run(run_output_dir, GRIDION_RUN_ID)

        result = make_uploader(config).upload_now(GRIDION_RUN_ID)

        assert result == SOME_DESTINATIONS_UNCHECKED
        assert self.uploaded_to(fake_rsync) == ['archive']

    def test_upload_now_still_reports_failures(self, config, run_output_dir, offline, fake_rsync, fake_ssh, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')

        assert make_uploader(config).upload_now(GRIDION_RUN_ID) == 'UPLOAD_INCOMPLETE_WITH_ERROR'


class TestUploadNow:
    """
    One-off uploads from the command line.
    """

    def test_uploads_where_needed(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)

        result = make_uploader(config, auto_upload=False).upload_now(GRIDION_RUN_ID)

        assert result == 'UPLOAD_COMPLETE'
        assert uploads() == [GRIDION_RUN_ID]

    def test_already_uploaded(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        assert make_uploader(config).upload_now(GRIDION_RUN_ID) == 'ALREADY_UPLOADED'
        assert uploads() == []

    def test_force(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        mark_uploaded(destination_dir, GRIDION_RUN_ID)

        assert make_uploader(config).upload_now(GRIDION_RUN_ID, force=True) == 'UPLOAD_COMPLETE'
        assert uploads() == [GRIDION_RUN_ID]

    def test_failure(self, config, run_output_dir, fake_rsync, fake_ssh, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        monkeypatch.setenv('FAKE_RSYNC_FAIL', 'archive.example.org')

        assert make_uploader(config).upload_now(GRIDION_RUN_ID) == 'UPLOAD_INCOMPLETE_WITH_ERROR'

    def test_destination_cant_be_checked(self, config, run_output_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        fake_ssh.fail = True

        assert make_uploader(config).upload_now(GRIDION_RUN_ID) == core.UNKNOWN
        assert uploads() == []

    def test_unknown_run(self, config, fake_rsync, fake_ssh):
        with pytest.raises(LookupError, match=GRIDION_RUN_ID):
            make_uploader(config).upload_now(GRIDION_RUN_ID)

    def test_incomplete_run(self, config, run_output_dir, fake_rsync, fake_ssh):
        make_run(run_output_dir, GRIDION_RUN_ID, complete=False)

        with pytest.raises(LookupError):
            make_uploader(config).upload_now(GRIDION_RUN_ID)

    def test_excluded_run(self, config, run_output_dir, fake_rsync, fake_ssh, uploads):
        make_run(run_output_dir, GRIDION_RUN_ID)
        config['excluded_runs'] = [GRIDION_RUN_ID]
        uploader = Uploader(config['_path'], config)

        with pytest.raises(ValueError, match="excluded"):
            uploader.upload_now(GRIDION_RUN_ID)
        assert uploads() == []


class TestRequests:
    def test_run_excluded_mid_scan_is_not_uploaded(self, config, run_output_dir, fake_rsync, fake_ssh, uploads, monkeypatch):
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

    def test_exclude_appends_to_excluded_runs_list(self, config):
        with open(config['excluded_runs_list'], 'w') as f:
            f.write('20261001_0000_X1_FAZ00000_00000000')  # no trailing newline
        uploader = make_uploader(config)

        uploader.exclude(GRIDION_RUN_ID)
        uploader.exclude(GRIDION_RUN_ID)
        uploader._handle_requests()

        assert open(config['excluded_runs_list']).read().splitlines() == ['20261001_0000_X1_FAZ00000_00000000', GRIDION_RUN_ID]
        assert GRIDION_RUN_ID in uploader.config['excluded_runs']

    def test_exclude_without_excluded_runs_list_is_a_warning(self, config, caplog):
        del config['excluded_runs_list']
        uploader = make_uploader(config)

        assert not uploader.can_exclude()
        uploader.exclude(GRIDION_RUN_ID)
        uploader._handle_requests()

        assert len(events(caplog, 'exclude_failed')) == 1

    def test_exclude_is_saved_promptly_while_idle(self, config, caplog):
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

    def test_requests_made_just_before_stopping_are_kept(self, config):
        uploader = make_uploader(config)
        uploader.exclude(GRIDION_RUN_ID)
        uploader.stop()

        uploader.run()

        assert open(config['excluded_runs_list']).read().split() == [GRIDION_RUN_ID]


class TestLoop:
    def test_scan_errors_dont_stop_the_loop(self, config, monkeypatch, caplog):
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

    def test_stop_ends_an_upload_leaving_it_partial(self, config, run_output_dir, destination_dir, fake_rsync, fake_ssh, monkeypatch):
        make_run(run_output_dir, GRIDION_RUN_ID)
        monkeypatch.setenv('FAKE_RSYNC_SLEEP', '30')
        uploader = make_uploader(config)
        thread = threading.Thread(target=uploader.run, daemon=True)
        thread.start()
        # Wait until rsync has started copying (it creates the run's directory first).
        wait_for(lambda: (destination_dir / GRIDION_RUN_ID).exists())

        started = time.monotonic()
        uploader.stop()
        thread.join(10)

        assert not thread.is_alive()
        assert time.monotonic() - started < 10
        assert uploader.uploading_run_id is None
        # Left partly uploaded, so it's resumed next time.
        assert core.list_remote_runs(config['sequencers'][0]['destination_servers'][0]) == {GRIDION_RUN_ID: core.PARTIAL}
