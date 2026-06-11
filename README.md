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

## Run One Poll Cycle

```bash
python run_collector.py --once
```

## Run Continuously

```bash
python run_collector.py
```

The collector connects to each enabled machine once per poll cycle, reads that machine's enabled tags, inserts results into SQLite, disconnects cleanly, then sleeps until the next interval boundary. Stop it with `Ctrl+C`.

## CSV Location

Place the OPC UA discovery CSV files in `Tag_Files/` with names matching `*_opcua_discovered_tags.csv`.

## Machine Auth Configuration

Per-machine auth and endpoint overrides live in `config.py` under `MACHINE_AUTH_CONFIG`.

- `Pinch 20` is configured for the patched blank-password username token flow.
- `Pinch 16`, `Pinch 17`, `Pinch 18`, `Pinch 19`, and `Pinch 21` default to anonymous auth with `SecurityPolicy None`.
- If `endpoint_url` is left as `None`, the importer uses the endpoint from the CSV.

## Database Notes

SQLite is configured with:

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA busy_timeout=5000`

The schema includes `machines`, `tags`, `tag_samples`, and `poll_runs`, plus indexes for common time-series queries.

## Collector Dashboard

Install dependencies, initialize the database if needed, run the collector, then start the dashboard:

```bash
pip install -r requirements.txt
python run_collector.py --init-only
python run_collector.py
python dashboard_app.py --host 0.0.0.0 --port 5050
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

## Future Migration

To migrate later to PostgreSQL or MySQL, keep the same logical schema and replace the small SQLite access layer in `db.py` plus the SQL connection setup. The importer and collector logic are already separated from transport details, so the main change would be swapping the DB driver and adapting SQL parameter style or upsert syntax.
# pinch16_21_Add
