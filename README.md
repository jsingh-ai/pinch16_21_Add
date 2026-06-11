# OPC UA Collector With Azure MySQL

This project polls OPC UA tags from Pinch 16 through Pinch 21 and stores one-minute history in Azure MySQL. The dashboard reads only from MySQL and converts UTC timestamps to US Central time for display.

## Install

```bash
pip install -r requirements.txt
```

Set MySQL connection environment variables before running:

```bash
set MYSQL_HOST=your-azure-mysql-host
set MYSQL_PORT=3306
set MYSQL_USER=your_user
set MYSQL_PASSWORD=your_password
set MYSQL_DATABASE=opcua_collector
```

On PowerShell use `$env:MYSQL_HOST="..."` style assignments instead.

## Machine Auth Source Of Truth

- `Pinch 16`: anonymous
- `Pinch 17`: anonymous
- `Pinch 18`: anonymous
- `Pinch 19`: anonymous
- `Pinch 20`: `username_blank_basic256_token`
- `Pinch 21`: anonymous

Pinch 20 details:

- endpoint `opc.tcp://192.168.11.26:4840`
- channel `SecurityPolicy None`
- blank `security_string`
- username `OpcUaViewer`
- blank password `''`
- user token `PolicyId='3'`
- user token policy URI `http://opcfoundation.org/UA/SecurityPolicy#Basic256`
- no client cert/key files required

## Schema Design

Tables:

- `machines`: one row per physical machine
- `tags`: one row per machine/node definition
- `tag_samples`: append-only time-series rows
- `poll_runs`: one row per collector cycle
- `machine_poll_runs`: one row per machine per collector cycle

Relationships:

- `machines` 1-to-many `tags`
- `machines` 1-to-many `tag_samples`
- `tags` 1-to-many `tag_samples`
- `poll_runs` 1-to-many `machine_poll_runs`

All database timestamps are stored in UTC using `DATETIME(6)`.

## Query Patterns

Useful long-term query patterns this schema supports:

- per-machine recent status:
  use `machine_poll_runs` plus `tag_samples` recent counts
- per-tag time series:
  query `tag_samples` by `tag_id, sampled_at_utc`
- machine-wide feature extraction by time window:
  query `tag_samples` by `machine_id, sampled_at_utc`
- bad sample and error analysis:
  query `tag_samples` by `quality, sampled_at_utc` and `machine_id, quality, sampled_at_utc`

## Initialization

Create tables only:

```bash
python run_collector.py --init-db
```

Create tables and import tag definitions from `Tag_Files`:

```bash
python run_collector.py --init-only
```

`--init-only` is the full tag-definition sync step. On a networked Azure MySQL database it can take noticeably longer than normal polling startup.

Show the resolved per-machine auth config:

```bash
python run_collector.py --show-config
```

## Polling

Run one machine once:

```bash
python run_collector.py --once --machine "Pinch 20" --no-sleep-align
```

Run the collector continuously:

```bash
python run_collector.py
```

After tags already exist in MySQL, a normal collector start skips the heavy CSV tag re-import step and goes straight into polling.

Only one collector instance is allowed by default to avoid duplicate writes. Use `--allow-multiple` only if duplicate polling is intentional.

## Dashboard

Run the dashboard locally:

```bash
python dashboard_app.py --host 0.0.0.0 --port 5050 --waitress
```

Then open:

```text
http://localhost:5050
```

The dashboard:

- reads only from MySQL
- never connects to OPC UA
- keeps DB timestamps in UTC
- displays times in US Central time in the browser
- includes maintenance buttons for cleanup and monitoring-data clears

Expose the dashboard only on a trusted internal network because maintenance buttons are present.

## Retention And Cleanup

Defaults:

- good samples: 14 days
- bad samples: 60 days
- poll runs and machine poll runs: 14 days
- machines and tags are never deleted by cleanup

Run cleanup manually:

```bash
python run_collector.py --cleanup-now
```

Destructive clear commands require `--yes`:

```bash
python run_collector.py --clear-poll-runs --yes
python run_collector.py --clear-bad-samples --yes
python run_collector.py --clear-all-samples --yes
python run_collector.py --clear-monitoring-data --yes
```

## DB Stats And Health

Connection smoke test:

```bash
python test_mysql_connection.py
```

Schema/config smoke test:

```bash
python smoke_checks.py
```

DB stats:

```bash
python run_collector.py --db-stats
```

At about `3833` samples per minute, the collector generates roughly `5.52 million` samples per day. The MySQL schema is designed to support dashboarding, cleanup, and future ML extraction, but long retention still requires capacity planning on the Azure MySQL tier.

## ML Readiness Notes

Each sample keeps:

- `sampled_at_utc`
- `source_timestamp_utc`
- `server_timestamp_utc`
- `value_numeric`
- `value_text`
- `quality`
- `status_code`
- `error_text`

This keeps the schema simple while preserving enough metadata for later feature extraction and data-quality filtering.
