#!/usr/bin/env python3
"""
Generate fake Nanopore sequencing run directories, and a config to upload them locally, for trying out the uploader.

The runs only need to satisfy what `core.find_runs` checks for:
  - they're three levels below a sequencer's `run_output_dir` (MinKNOW's <protocol_group_id>/<sample_id>/<run_id> layout)
  - the directory name matches the GridION or PromethION run ID regex
  - they're complete: there's a `final_summary*.txt` file of `key=value` lines

Everything is written under <output-dir>/demo:

  data/gridion/...        GridION run output dir (complete, in-progress, excluded and badly-named runs)
  data/promethion/...     PromethION run output dir
  destination/...         Where the runs get "uploaded" to
  demo-config.json        Uploader config pointing at the above
  uploads.csv             Upload db, with one run already marked UPLOAD_COMPLETE
  excluded_runs.txt       Excluded runs list
  fake-ssh                An RSYNC_RSH stand-in that runs rsync's remote end locally (no SSH server needed),
                          and throttles it so the progress bar has something to show
  demo_site/sitecustomize.py
                          Swaps paramiko's SSHClient for one that writes upload_complete.json locally
  run-demo.sh             Starts the TUI with all of the above

Usage:
  python scripts/generate_demo_runs.py [--output-dir test_input] [--force]
  test_input/demo/run-demo.sh
"""

import argparse
import csv
import json
import os
import shutil
import stat
import sys

from pathlib import Path

MB = 1024 * 1024

GRIDION_ID = "GXB03493"
PROMETHION_ID = "P2S-01234"

# (sequencer, protocol_group_id, sample_id, run_id, complete, size_mb)
RUNS = [
    ("gridion", "RESP-2026-10-13", "plate-01", "20261013_1015_X1_FAZ11111_1a2b3c4d", True, 16),
    ("gridion", "RESP-2026-10-14", "plate-02", "20261014_0930_X2_FAZ22222_5e6f7a8b", True, 48),
    ("gridion", "RESP-2026-10-14", "plate-03", "20261014_0931_X4_FAZ44444_abcdef12", True, 24),
    ("gridion", "RESP-2026-10-15", "plate-04", "20261015_0800_X3_FAZ33333_9c0d1e2f", False, 8),
    ("gridion", "RESP-2026-10-14", "plate-05", "20261014_bad_run_name", True, 4),
    ("promethion", "WGS-2026-10-12", "batch-17", "20261012_1400_P2S_01234-1_PBA55555_0f1e2d3c", True, 64),
    ("promethion", "WGS-2026-10-15", "batch-18", "20261015_0745_P2S_01234-2_PBA66666_aa11bb22", False, 8),
]

ALREADY_UPLOADED = ["20261013_1015_X1_FAZ11111_1a2b3c4d"]
EXCLUDED = ["20261014_0931_X4_FAZ44444_abcdef12"]

FAKE_SSH = r'''#!/usr/bin/env python3
"""
Stand-in for ssh, for use as RSYNC_RSH in the uploader demo.

rsync runs `$RSYNC_RSH [options] host command...`. This ignores the host and runs the command locally,
copying rsync's data to it at DEMO_UPLOAD_RATE_MB megabytes per second (default 8) so that uploads take
long enough to watch.
"""
import os
import subprocess
import sys
import threading
import time

OPTIONS_WITH_VALUES = set("bcDEeFIiJLlmOopQRSWw")

args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if len(args[i]) == 2 and args[i][1] in OPTIONS_WITH_VALUES else 1
command = " ".join(args[i + 1:])

rate = float(os.environ.get("DEMO_UPLOAD_RATE_MB", "8")) * 1024 * 1024
proc = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE)


def copy_stdin():
    start = time.monotonic()
    sent = 0
    try:
        while True:
            chunk = os.read(0, 64 * 1024)
            if not chunk:
                break
            proc.stdin.write(chunk)
            proc.stdin.flush()
            sent += len(chunk)
            delay = sent / rate - (time.monotonic() - start)
            if delay > 0:
                time.sleep(delay)
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass


# Like ssh, exit when the remote command does. rsync keeps our stdin open until we've exited.
threading.Thread(target=copy_stdin, daemon=True).start()
sys.exit(proc.wait())
'''

SITECUSTOMIZE = r'''"""
Loaded automatically (via PYTHONPATH) by run-demo.sh, so that the uploader demo works without an SSH server.

The uploader writes `upload_complete.json` to the destination with paramiko over SFTP. This replaces
paramiko's SSHClient and key loading with stand-ins that write the file to the local filesystem.
"""
import os
import warnings

try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
    import paramiko
except ImportError:
    paramiko = None


class _LocalSFTP:
    def putfo(self, fl, remotepath):
        os.makedirs(os.path.dirname(remotepath), exist_ok=True)
        with open(remotepath, "wb") as f:
            f.write(fl.read())

    def close(self):
        pass


class _LocalSSHClient:
    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, *args, **kwargs):
        pass

    def open_sftp(self):
        return _LocalSFTP()

    def close(self):
        pass


if paramiko is not None:
    paramiko.SSHClient = _LocalSSHClient
    paramiko.RSAKey.from_private_key_file = classmethod(lambda cls, *args, **kwargs: None)
'''

RUN_DEMO = '''#!/usr/bin/env bash
# Generated by scripts/generate_demo_runs.py. Starts the uploader TUI against the demo runs.
# Extra arguments are passed on (eg. --log-level debug). Set DEMO_UPLOAD_RATE_MB to change the upload speed.
set -euo pipefail
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RSYNC_RSH="$DEMO_DIR/fake-ssh"
export PYTHONPATH="$DEMO_DIR/demo_site${PYTHONPATH:+:$PYTHONPATH}"
exec nanopore-uploader -c "$DEMO_DIR/demo-config.json" --tui --log-file "$DEMO_DIR/demo.log" "$@"
'''


def write_executable(path: Path, contents: str):
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_sized_file(path: Path, size_bytes: int):
    # Sparse files: quick to create and take no space here (rsync still copies every byte).
    with open(path, "wb") as f:
        f.truncate(size_bytes)


def make_run(run_output_dir: Path, instrument_id: str, protocol_group_id: str, sample_id: str, run_id: str, complete: bool, size_mb: int):
    run_dir = run_output_dir / protocol_group_id / sample_id / run_id
    run_dir.mkdir(parents=True)
    parts = run_id.split("_")
    flow_cell_id = parts[-2]
    acquisition_id = parts[-1]
    started = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}T{parts[1][:2]}:{parts[1][2:]}:00-07:00" if parts[1].isdigit() else ""

    # pod5 files hold most of the data; a few barcoded fastq files hold the rest.
    num_pod5 = 4
    pod5_dir = run_dir / "pod5"
    pod5_dir.mkdir()
    for n in range(num_pod5):
        write_sized_file(pod5_dir / f"{flow_cell_id}_{acquisition_id}_{n}.pod5", size_mb * MB * 3 // (4 * num_pod5))
    for barcode in ("barcode01", "barcode02", "barcode03", "unclassified"):
        fastq_dir = run_dir / "fastq_pass" / barcode
        fastq_dir.mkdir(parents=True)
        write_sized_file(fastq_dir / f"{flow_cell_id}_pass_{barcode}_{acquisition_id}_0.fastq.gz", size_mb * MB // 16)

    (run_dir / f"sequencing_summary_{flow_cell_id}_{acquisition_id}.txt").write_text("filename\tread_id\trun_id\tchannel\n")
    (run_dir / f"report_{flow_cell_id}_{acquisition_id}.html").write_text("<html><body>Demo run report</body></html>\n")

    if complete:
        final_summary = {
            "instrument": instrument_id,
            "position": parts[2] if parts[2].startswith("X") else f"{parts[3]}",
            "flow_cell_id": flow_cell_id,
            "sample_id": sample_id,
            "protocol_group_id": protocol_group_id,
            "protocol": "sequencing/sequencing_MIN114_DNA_e8_2_400K:FLO-MIN114:SQK-RBK114-24",
            "protocol_run_id": f"{acquisition_id}-demo",
            "acquisition_run_id": f"{acquisition_id}-demo-acquisition",
            "started": started,
            "acquisition_stopped": started,
            "processing_stopped": started,
            "basecalling_enabled": "1",
            "sequencing_summary_file": f"sequencing_summary_{flow_cell_id}_{acquisition_id}.txt",
            "pod5_files_in_final_dest": str(num_pod5),
            "fastq_files_in_final_dest": "4",
        }
        (run_dir / f"final_summary_{flow_cell_id}_{acquisition_id}.txt").write_text(
            "".join(f"{k}={v}\n" for k, v in final_summary.items())
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", default="test_input", help="Directory to create the 'demo' directory in (default: test_input)")
    parser.add_argument("--force", action="store_true", help="Delete and re-create an existing demo directory")
    args = parser.parse_args()

    demo_dir = (Path(args.output_dir) / "demo").resolve()
    if demo_dir.exists():
        if not args.force:
            sys.exit(f"{demo_dir} already exists. Use --force to delete and re-create it.")
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True)

    sequencers = {
        "gridion": {"instrument_id": GRIDION_ID},
        "promethion": {"instrument_id": PROMETHION_ID},
    }
    for sequencer, info in sequencers.items():
        info["run_output_dir"] = demo_dir / "data" / sequencer
        info["destination_dir"] = demo_dir / "destination" / sequencer / info["instrument_id"]
        info["run_output_dir"].mkdir(parents=True)
        info["destination_dir"].mkdir(parents=True)

    for sequencer, protocol_group_id, sample_id, run_id, complete, size_mb in RUNS:
        info = sequencers[sequencer]
        make_run(info["run_output_dir"], info["instrument_id"], protocol_group_id, sample_id, run_id, complete, size_mb)

    db_file = demo_dir / "uploads.csv"
    with open(db_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sequencing_run_id", "status"])
        writer.writeheader()
        for run_id in ALREADY_UPLOADED:
            writer.writerow({"sequencing_run_id": run_id, "status": "UPLOAD_COMPLETE"})

    excluded_runs_list = demo_dir / "excluded_runs.txt"
    excluded_runs_list.write_text("".join(f"{run_id}\n" for run_id in EXCLUDED))

    config = {
        "sequencers": [
            {
                "instrument_id": info["instrument_id"],
                "run_output_dir": str(info["run_output_dir"]),
                "instrument_type": sequencer,
                "destination_servers": [
                    {
                        "server_id": f"demo-{sequencer}-archive",
                        "server_address": "localhost",
                        "login_id": os.environ.get("USER", "demo"),
                        "id_file": str(demo_dir / "unused-demo-key"),
                        "permissions": {"Du": "rwx", "Dg": "rx", "Fu": "rw", "Fg": "r", "o": ""},
                        "destination_dir": str(info["destination_dir"]),
                    }
                ],
            }
            for sequencer, info in sequencers.items()
        ],
        "scan_interval_seconds": 60,
        "excluded_runs_list": str(excluded_runs_list),
        "db": {"file": str(db_file)},
    }
    (demo_dir / "demo-config.json").write_text(json.dumps(config, indent=4) + "\n")

    write_executable(demo_dir / "fake-ssh", FAKE_SSH)
    (demo_dir / "demo_site").mkdir()
    (demo_dir / "demo_site" / "sitecustomize.py").write_text(SITECUSTOMIZE)
    write_executable(demo_dir / "run-demo.sh", RUN_DEMO)

    print(f"Demo runs written to {demo_dir}")
    print(f"Start the TUI with: {demo_dir / 'run-demo.sh'}")


if __name__ == "__main__":
    main()
