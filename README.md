# nanopore-uploader

![Tests](https://github.com/BCCDC-PHL/nanopore-uploader/actions/workflows/ci.yml/badge.svg)

Watch for new sequence folders and upload to remote server.

## Configuration
See [config.json](config.json) for format details.

```json
```

## Running

### Typical Run Command
```
nanopore-uploader -c config.json
```

To exit, use "Ctrl + C" keyboard combo. Its safe!

### Monitor and auto modes
By default the uploader runs in **monitor mode**: it scans and reports which runs are ready to upload, but
only uploads a run when someone asks (with `u` in the dashboard, or `--upload` below).

In **auto mode** it uploads runs by itself as they're ready. Turn it on with `"auto_upload": true` in the
config, or for one session with `a` in the dashboard.

To upload one run from the command line, then exit:
```
nanopore-uploader -c config.json --upload 20261014_0930_X2_FAZ22222_5e6f7a8b
```

This uploads the run wherever it isn't already uploaded. If it's already uploaded everywhere, nothing is
copied; add `--force` to upload it again. That rewrites `upload_complete.json`, which may start downstream
processing again.

### How it decides what to upload
Nothing is stored locally about which runs have been uploaded. On each scan, the uploader:

1. Finds complete runs on the sequencer: directories three levels below a sequencer's `run_output_dir`, named like
   a run ID, with a `final_summary*.txt` file.
2. Lists each destination server's `destination_dir` (one SSH command per destination), to see which runs are
   there and which have an `upload_complete.json`.
3. Works out where each run needs uploading, on its own sequencer's destination servers:

| At the destination | Meaning | Needs uploading? |
|---|---|---|
| `<run_id>/upload_complete.json` | Uploaded | No |
| `<run_id>/` but no `upload_complete.json` | Partly uploaded (eg. interrupted) | Yes: uploading resumes it (rsync copies what's missing) |
| No `<run_id>/` | Not uploaded | Yes |
| Destination can't be listed | Unknown | Not until it can be checked |

In auto mode, runs that need uploading are uploaded. In monitor mode, they're reported as ready to upload.

Runs in the `excluded_runs_list` file (one run ID per line) are skipped. In auto mode, a failed upload is
retried on later scans, up to `max_upload_attempts` times (default 3); the count resets when the uploader
restarts, or when someone uploads the run by hand.

If `upload_history_file` is set, each upload attempt is appended to it as a CSV row (start and end times, run,
destination, result). It's only a record for people to read: nothing uses it to decide what to upload, so it's
safe to open, edit or delete.

### Interactive dashboard (experimental)
```
pip install -e '.[tui]'
nanopore-uploader -c config.json --tui --log-file uploader.jsonl
```

Shows the runs table, the current upload's progress, and a readable activity log instead of raw log lines.
Logs only go to `--log-file` in this mode (stdout is used by the dashboard).

| Key | Action |
|-----|--------|
| `s` | Scan now, instead of waiting for the scan interval |
| `u` | Upload the selected run (asks first; if it's already uploaded, asks whether to upload it again) |
| `a` | Switch between monitor and auto mode for this session (asks before switching to auto) |
| `x` | Exclude the selected run (adds it to `excluded_runs_list`) |
| `l` | Switch the log between the activity summary and all events |
| `q` | Quit (asks first if an upload is in progress) |

To try it with fake runs and a local "destination" (no SSH server needed):
```
python scripts/generate_demo_runs.py
test_input/demo/run-demo.sh
```

## Running tests
```
pip install -e '.[test]'
pytest
```

The tests use a fake `rsync` and a fake SSH client, so they don't need network access or an SSH server.
