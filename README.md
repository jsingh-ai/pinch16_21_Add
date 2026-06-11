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

## Future Migration

To migrate later to PostgreSQL or MySQL, keep the same logical schema and replace the small SQLite access layer in `db.py` plus the SQL connection setup. The importer and collector logic are already separated from transport details, so the main change would be swapping the DB driver and adapting SQL parameter style or upsert syntax.
# pinch16_21_Add
