# OPC UA Polling Collector

This project imports discovered OPC UA tags from `Tag_Files/*.csv` and stores time-series samples in local SQLite at `data/opcua_history.sqlite3`.

It is written to be Windows-compatible and does not rely on Linux-only shell features or filesystem paths.

## Files

- `Tag_Files/Pinch_16_opcua_discovered_tags.csv` through `Tag_Files/Pinch_21_opcua_discovered_tags.csv` are the input discovery files.
- `data/opcua_history.sqlite3` is created automatically on initialization.

## Install

```bash
pip install -r requirements.txt
```

On Windows, run the commands from `cmd.exe` or PowerShell with your virtual environment activated.

## Initialize Schema And Import Tags

```bash
python run_collector.py --init-only
```

## Show Config

```bash
python run_collector.py --show-config
```

## Run One Poll Cycle

```bash
python run_collector.py --once
```

## Run Continuously

```bash
python run_collector.py
```

The collector connects to each enabled machine once per poll cycle, reads that machine's enabled tags, inserts results into SQLite, disconnects cleanly, then sleeps until the next interval boundary. Stop it with `Ctrl+C`.

By default only one collector instance is allowed at a time to prevent duplicate inserts. Use `--allow-multiple` only if duplicate polling is intentional.

## CSV Location

Place the OPC UA discovery CSV files in `Tag_Files/` with names matching `*_opcua_discovered_tags.csv`.

## Machine Auth Configuration

Per-machine auth and endpoint overrides live in `config.py` under `MACHINE_AUTH_CONFIG`.

- `Pinch 16`, `Pinch 17`, `Pinch 18`, `Pinch 19`, and `Pinch 21` use anonymous auth.
- `Pinch 20` uses the patched blank-password username token flow with:
  - endpoint `opc.tcp://192.168.11.26:4840`
  - channel `SecurityPolicy None`
  - blank `security_string`
  - username `OpcUaViewer`
  - blank password `''`
  - user token `PolicyId='3'`
  - user token policy URI `http://opcfoundation.org/UA/SecurityPolicy#Basic256`
- No client cert/key files are required for `Pinch 20`.
- If `endpoint_url` is left as `None`, the importer uses the endpoint from the CSV.

## Database Notes

SQLite is configured with:

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA busy_timeout=5000`

The schema includes `machines`, `tags`, `tag_samples`, `poll_runs`, and `machine_poll_runs`, plus indexes for common time-series queries.

Keep the SQLite database on local disk. WAL mode should not be used from a network share.

At about `3833` samples per minute, this collector produces roughly `5.52 million` samples per day. SQLite is acceptable for local/test retention windows, but PostgreSQL or MySQL is the better next stage for longer retention or higher sustained history volume.

## Collector Dashboard

Install dependencies, initialize the database if needed, run the collector, then start the dashboard:

```bash
pip install -r requirements.txt
python run_collector.py --init-only
python run_collector.py
python dashboard_app.py --host 0.0.0.0 --port 5050
```

For a production-ish local VM deployment, prefer Waitress:

```bash
python dashboard_app.py --host 0.0.0.0 --port 5050 --waitress
```

Then open:

```text
http://localhost:5050
```

The dashboard reads only from SQLite and does not connect to OPC UA directly. It refreshes every 15 seconds and exposes a JSON status API at `/api/status`.

Timestamps remain stored in UTC in SQLite, but the dashboard displays them in US Central time for operators.

### Dashboard Maintenance Actions

The dashboard includes maintenance buttons for running retention cleanup and clearing monitoring data. Run the dashboard only on a trusted internal network because those controls can trigger cleanup actions.

- `Run cleanup now` calls the same retention cleanup used by the collector.
- `Clear poll runs`, `Clear bad samples`, and `Clear monitoring data` require confirmation in the browser.
- These dashboard maintenance actions do not delete `machines` or `tags`.
- The Flask dev server is fine for local testing; Waitress is preferred if the dashboard stays running on the VM.

## Data Retention And Cleanup

By default:

- good samples are kept for 14 days
- bad samples are kept for 60 days
- `poll_runs` are kept for 14 days
- `machines` and `tags` are never deleted by cleanup

Automatic cleanup runs once at collector startup and then every 60 minutes while the collector is running. Cleanup uses SQL `DELETE` statements with timestamp cutoffs and does not load historical samples into Python memory.

Manual cleanup commands are available, but destructive clear operations require `--yes`.

```bash
python run_collector.py --cleanup-now
python run_collector.py --clear-poll-runs --yes
python run_collector.py --clear-bad-samples --yes
python run_collector.py --clear-monitoring-data --yes
```

## DB Health Tools

```bash
python run_collector.py --db-stats
python run_collector.py --checkpoint-db
python run_collector.py --checkpoint-db --truncate
python smoke_checks.py
```

- `--db-stats` shows current size plus growth/retention math.
- `--checkpoint-db` runs `PRAGMA wal_checkpoint(PASSIVE)`.
- `--checkpoint-db --truncate` runs `PRAGMA wal_checkpoint(TRUNCATE)` and is best used while the collector is stopped.

## Future Migration

To migrate later to PostgreSQL or MySQL, keep the same logical schema and replace the small SQLite access layer in `db.py` plus the SQL connection setup. The importer and collector logic are already separated from transport details, so the main change would be swapping the DB driver and adapting SQL parameter style or upsert syntax.
