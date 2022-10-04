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
