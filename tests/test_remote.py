"""
Tests of how a run's state on its destinations is worked out.
"""
import os
import stat

import pytest

import nanopore_uploader.core as core

from conftest import GRIDION_RUN_ID, GRIDION_RUN_ID_2, PROMETHION_RUN_ID, mark_partial, mark_uploaded


def test_parse_remote_runs_listing():
    listing = "\n".join([
        f"./{GRIDION_RUN_ID}",
        f"./{GRIDION_RUN_ID}/upload_complete.json",
        f"./{GRIDION_RUN_ID_2}",
        f"./{PROMETHION_RUN_ID}/upload_complete.json",  # marker listed before (or without) its directory
        "./not_a_run",
        "./20261014_0930_X2_FAZ22222_5e6f7a8b_old",     # looks like a run ID, but isn't one
        f"./{GRIDION_RUN_ID_2}/other_file.json",
    ])

    assert core.parse_remote_runs_listing(listing) == {
        GRIDION_RUN_ID: core.UPLOADED,
        GRIDION_RUN_ID_2: core.PARTIAL,
        PROMETHION_RUN_ID: core.UPLOADED,
    }


def test_list_remote_runs(config, destination_dir, fake_ssh):
    mark_uploaded(destination_dir, GRIDION_RUN_ID)
    mark_partial(destination_dir, GRIDION_RUN_ID_2)
    (destination_dir / "not_a_run").mkdir()
    (destination_dir / "stray_file.txt").write_text("")
    destination = config["sequencers"][0]["destination_servers"][0]

    remote_runs = core.list_remote_runs(destination)

    assert remote_runs == {GRIDION_RUN_ID: core.UPLOADED, GRIDION_RUN_ID_2: core.PARTIAL}
    # One command, however many runs there are.
    assert len(fake_ssh.commands) == 1


def test_list_remote_runs_handles_awkward_destination_paths(config, tmp_path, fake_ssh):
    destination = dict(config["sequencers"][0]["destination_servers"][0])
    awkward_dir = tmp_path / "it's a dir with spaces"
    awkward_dir.mkdir()
    mark_uploaded(awkward_dir, GRIDION_RUN_ID)
    destination["destination_dir"] = str(awkward_dir)

    assert core.list_remote_runs(destination) == {GRIDION_RUN_ID: core.UPLOADED}


def test_list_remote_runs_fails_if_the_destination_dir_is_missing(config, tmp_path, fake_ssh):
    destination = dict(config["sequencers"][0]["destination_servers"][0])
    destination["destination_dir"] = str(tmp_path / "not-mounted")

    with pytest.raises(core.RemoteListingError, match="not-mounted"):
        core.list_remote_runs(destination)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read directories without read permission")
def test_list_remote_runs_fails_if_a_run_dir_cant_be_read(config, destination_dir, fake_ssh):
    # If a marker can't be checked, the destination's state is unknown, rather than the run looking partial.
    mark_uploaded(destination_dir, GRIDION_RUN_ID)
    (destination_dir / GRIDION_RUN_ID).chmod(0)
    try:
        with pytest.raises(core.RemoteListingError):
            core.list_remote_runs(config["sequencers"][0]["destination_servers"][0])
    finally:
        (destination_dir / GRIDION_RUN_ID).chmod(stat.S_IRWXU)


def test_list_remote_runs_has_a_timeout(config, fake_ssh):
    destination = config["sequencers"][0]["destination_servers"][0]
    core.list_remote_runs(destination)
    assert fake_ssh.timeouts == [core.REMOTE_COMMAND_TIMEOUT_SECONDS]

    # eg. the destination's filesystem has stopped responding.
    fake_ssh.hang = True
    with pytest.raises(OSError):
        core.list_remote_runs(destination)


def test_list_remote_runs_fails_if_the_server_cant_be_reached(config, fake_ssh):
    fake_ssh.fail = True

    with pytest.raises(core.paramiko.SSHException):
        core.list_remote_runs(config["sequencers"][0]["destination_servers"][0])


@pytest.mark.parametrize("destination_states, expected", [
    ({"a": core.UPLOADED}, core.UPLOADED),
    ({"a": core.ABSENT}, core.ABSENT),
    ({"a": core.PARTIAL}, core.PARTIAL),
    ({"a": core.UNKNOWN}, core.UNKNOWN),
    ({}, core.UNKNOWN),
    # With more than one destination: still needs uploading if any destination needs it.
    ({"a": core.UPLOADED, "b": core.ABSENT}, core.ABSENT),
    ({"a": core.ABSENT, "b": core.PARTIAL}, core.PARTIAL),
    ({"a": core.UNKNOWN, "b": core.PARTIAL}, core.PARTIAL),
    ({"a": core.UPLOADED, "b": core.UNKNOWN}, core.UNKNOWN),
])
def test_run_state(destination_states, expected):
    assert core.run_state(destination_states) == expected
