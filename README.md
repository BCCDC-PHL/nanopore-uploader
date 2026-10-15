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

A run is marked `UPLOAD_COMPLETE` once it has been copied to every destination server. If any destination fails,
the run is marked `UPLOAD_INCOMPLETE_WITH_ERROR` and retried on later scans, up to `max_upload_attempts` times
(default 3). Runs left `UPLOAD_STARTED` or `UPLOAD_INCOMPLETE` (eg. by Ctrl+C, or a crash) are resumed the same way.
The attempt count resets when the uploader restarts.

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
| `r` | Re-queue the selected run for upload |
| `x` | Exclude the selected run (adds it to `excluded_runs_list`) |
| `l` | Switch the log between the activity summary and all events |
| `q` | Quit (asks first if an upload is in progress) |

To try it with fake runs and a local "destination" (no SSH server needed):
```
python scripts/generate_demo_runs.py
test_input/demo/run-demo.sh
```

To upload one specific folder (will update status to REUPLOAD in db)
```
nanopore-uploader --upload-single-run 200619_M00325_0209_000000000-J6M35
```

Keep the other nanopore-uploader process running, and the file will be picked up.

## Other Commands

Backup database (recommended once a week)
```
nanopore-uploader --backup-db
```
