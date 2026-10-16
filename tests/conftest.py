"""
Shared fixtures.

Uploads run against a fake `rsync` (put first on PATH) and a fake paramiko SSH client, so the tests
need neither a network nor an SSH server. The fake rsync's behaviour is controlled with environment
variables (see FAKE_RSYNC below), which the real `subprocess.Popen` call passes on to it.
"""
import json
import os
import subprocess
import sys
import textwrap

from pathlib import Path

import pytest

import nanopore_uploader.core as core

GRIDION_RUN_ID = "20261014_0930_X2_FAZ22222_5e6f7a8b"
GRIDION_RUN_ID_2 = "20261015_1100_X3_FAZ33333_9c0d1e2f"
PROMETHION_RUN_ID = "20261012_1400_P2S_01234-1_PBA55555_0f1e2d3c"

FAKE_RSYNC = textwrap.dedent('''\
    """
    Stand-in for rsync. Environment variables:
      FAKE_RSYNC_LOG        Append each invocation's arguments here (one JSON list per line).
      FAKE_RSYNC_FAIL       Exit with FAKE_RSYNC_EXIT_CODE if the destination argument contains this.
      FAKE_RSYNC_EXIT_CODE  Exit code for failures (default 23).
      FAKE_RSYNC_GARBAGE    If set, also print lines that aren't progress updates.
      FAKE_RSYNC_SLEEP      Seconds to wait before exiting (so the upload can be stopped).

    Like rsync, it creates the run's directory at the destination (a local path, in the tests) before copying
    anything, so an upload that fails or is stopped leaves the run partly uploaded. It doesn't copy any files.
    """
    import json, os, sys, time

    args = sys.argv[1:]
    if os.environ.get("FAKE_RSYNC_LOG"):
        with open(os.environ["FAKE_RSYNC_LOG"], "a") as f:
            f.write(json.dumps(args) + "\\n")

    source, destination = args[-2], args[-1]
    destination_dir = destination.split(":", 1)[1]
    os.makedirs(os.path.join(destination_dir, os.path.basename(source)), exist_ok=True)
    if os.environ.get("FAKE_RSYNC_GARBAGE"):
        print("sending incremental file list", flush=True)
        print("some/file.pod5", flush=True)
        print("12  odd  line", flush=True)
    # Like rsync's --info=progress2: updates separated by carriage returns.
    for percent in (25, 50, 75, 100):
        sys.stdout.write(f"{percent * 4}.00K  {percent}%   10.00MB/s    0:00:0{percent // 25}\\r")
        sys.stdout.flush()
    sys.stdout.write("\\n")
    sys.stdout.flush()

    time.sleep(float(os.environ.get("FAKE_RSYNC_SLEEP", "0")))

    fail = os.environ.get("FAKE_RSYNC_FAIL")
    if fail and fail in destination:
        print(f"rsync: fake failure for {destination}", file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_RSYNC_EXIT_CODE", "23")))
''')


def make_run(run_output_dir: Path, run_id: str, complete: bool = True, protocol_group_id: str = "PG-01",
             sample_id: str = "sample-01", final_summary_text: str = None) -> Path:
    """
    Make a fake sequencing run directory, in MinKNOW's <protocol_group_id>/<sample_id>/<run_id> layout.

    :param complete: Write a final_summary file (which is what marks a run as complete).
    :param final_summary_text: Contents for the final_summary file (default: a few valid key=value lines).
    :return: The run directory.
    """
    run_dir = run_output_dir / protocol_group_id / sample_id / run_id
    (run_dir / "pod5").mkdir(parents=True)
    (run_dir / "pod5" / "reads_0.pod5").write_bytes(b"\0" * 1024)
    if complete:
        if final_summary_text is None:
            final_summary_text = f"instrument=GXB03493\nprotocol_group_id={protocol_group_id}\nsample_id={sample_id}\n"
        (run_dir / f"final_summary_{run_id.split('_')[-2]}_{run_id.split('_')[-1]}.txt").write_text(final_summary_text)
    return run_dir


def make_destination(tmp_path: Path, server_id: str) -> dict:
    destination_dir = tmp_path / "destinations" / server_id
    destination_dir.mkdir(parents=True)
    return {
        "server_id": server_id,
        "server_address": f"{server_id}.example.org",
        "login_id": "uploader",
        "id_file": str(tmp_path / "id_rsa"),
        "permissions": {"Du": "rwx", "Dg": "rx", "Fu": "rw", "Fg": "r", "o": ""},
        "destination_dir": str(destination_dir),
    }


@pytest.fixture
def config(tmp_path: Path) -> dict:
    """
    A config with one GridION sequencer and one destination server (the usual deployment), an excluded
    runs list and an upload history file. It's also written to `config['_path']`, for the Uploader to reload.
    """
    run_output_dir = tmp_path / "runs" / "gridion"
    run_output_dir.mkdir(parents=True)
    excluded_runs_list = tmp_path / "excluded_runs.txt"
    excluded_runs_list.write_text("")
    config = {
        "sequencers": [
            {
                "instrument_id": "GXB03493",
                "instrument_type": "gridion",
                "run_output_dir": str(run_output_dir),
                "destination_servers": [make_destination(tmp_path, "archive")],
            }
        ],
        "scan_interval_seconds": 3600,
        "excluded_runs_list": str(excluded_runs_list),
        "excluded_runs": [],
        "upload_history_file": str(tmp_path / "upload_history.csv"),
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({k: v for k, v in config.items() if k != 'excluded_runs'}, indent=2))
    config['_path'] = str(config_path)
    return config


def save_config(config: dict):
    """
    Write changes to the config fixture back to its file, so that they survive the Uploader reloading it.
    """
    with open(config['_path'], 'w') as f:
        json.dump({k: v for k, v in config.items() if k not in ('_path', 'excluded_runs')}, f, indent=2)


@pytest.fixture
def run_output_dir(config) -> Path:
    return Path(config['sequencers'][0]['run_output_dir'])


@pytest.fixture
def destination_dir(config) -> Path:
    """
    The (local) destination directory of the config's one destination server.
    """
    return Path(config['sequencers'][0]['destination_servers'][0]['destination_dir'])


def mark_uploaded(destination_dir: Path, run_id: str):
    """
    Make a run look finished on a destination, as if an earlier upload completed.
    """
    (destination_dir / run_id).mkdir(parents=True, exist_ok=True)
    (destination_dir / run_id / core.UPLOAD_COMPLETE_FILENAME).write_text("{}\n")


def mark_partial(destination_dir: Path, run_id: str):
    """
    Make a run look partly uploaded on a destination, as if an earlier upload was interrupted.
    """
    (destination_dir / run_id / "pod5").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def fake_rsync(tmp_path: Path, monkeypatch):
    """
    Put the fake rsync first on PATH.

    :return: An object with `.log` (path of the invocation log) and `.calls()` (the arguments of each call so far).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text(f"#!{sys.executable}\n" + FAKE_RSYNC)
    rsync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "rsync_calls.jsonl"
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(log))
    for var in ("FAKE_RSYNC_FAIL", "FAKE_RSYNC_EXIT_CODE", "FAKE_RSYNC_GARBAGE", "FAKE_RSYNC_SLEEP"):
        monkeypatch.delenv(var, raising=False)

    class FakeRsync:
        def __init__(self):
            self.log = log

        def calls(self) -> list[list[str]]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

    return FakeRsync()


@pytest.fixture
def fake_ssh(monkeypatch):
    """
    Replace paramiko's SSH client with one that works on the local filesystem: commands (the remote runs
    listing) run locally, and `upload_complete.json` is written to the local destination directory.

    Set `fake_ssh.fail = True` to make connecting fail, `fake_ssh.fail_sftp = True` to make writing
    `upload_complete.json` fail part way through (leaving a partly-written temporary file), `fake_ssh.fail_rename = True`
    to make putting it in place fail, or `fake_ssh.hang = True` to make commands time out.
    `fake_ssh.commands` has the commands run so far (with their timeouts in `fake_ssh.timeouts`), `fake_ssh.written`
    the `upload_complete.json` files written, and `fake_ssh.removed` the files removed.
    """
    class FakeSSH:
        def __init__(self):
            self.fail = False
            self.fail_sftp = False
            self.fail_rename = False
            self.hang = False
            self.written: list[str] = []
            self.removed: list[str] = []
            self.commands: list[str] = []
            self.timeouts: list = []

    state = FakeSSH()

    class ChannelFile:
        def __init__(self, data: bytes, exit_status: int):
            self.data = data
            self.channel = self
            self.exit_status = exit_status

        def read(self) -> bytes:
            if state.hang:
                raise TimeoutError("timed out (fake)")
            return self.data

        def recv_exit_status(self) -> int:
            return self.exit_status

    class LocalSFTP:
        def putfo(self, fl, remotepath):
            os.makedirs(os.path.dirname(remotepath), exist_ok=True)
            with open(remotepath, "wb") as f:
                data = fl.read()
                if state.fail_sftp:
                    f.write(data[:5])
                    raise OSError("Connection lost (fake)")
                f.write(data)

        def posix_rename(self, oldpath, newpath):
            if state.fail_rename:
                raise OSError("Failure (fake)")
            os.replace(oldpath, newpath)
            state.written.append(newpath)

        def remove(self, path):
            os.remove(path)  # raises FileNotFoundError, as paramiko does
            state.removed.append(path)

        def close(self):
            pass

    class LocalSSHClient:
        closed = False

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, *args, **kwargs):
            if state.fail:
                raise core.paramiko.SSHException("Unable to connect (fake)")

        def open_sftp(self):
            return LocalSFTP()

        def exec_command(self, command, timeout=None):
            state.commands.append(command)
            state.timeouts.append(timeout)
            result = subprocess.run(command, shell=True, capture_output=True)
            return None, ChannelFile(result.stdout, result.returncode), ChannelFile(result.stderr, result.returncode)

        def close(self):
            self.closed = True

    monkeypatch.setattr(core.paramiko, "SSHClient", LocalSSHClient)
    monkeypatch.setattr(core.paramiko.RSAKey, "from_private_key_file", classmethod(lambda cls, *args, **kwargs: None))
    return state
