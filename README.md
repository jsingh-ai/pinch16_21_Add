# Press 14–15 OPC UA Collector

## Overview

The Press 14–15 OPC UA Collector reads enabled OPC UA tags for `Press 14` and `Press 15` once per minute and stores samples in a dedicated MySQL database. It is intentionally independent of the existing Pinch collector: the existing `opcua_collector` database is not used, modified, migrated, or copied.

Database timestamps are stored in UTC. Continuous polling aligns to minute boundaries, runs one cycle at a time, uses batched OPC UA reads where the server supports them, and uses batched MySQL inserts. A Press-specific file lock prevents multiple instances on the same VM from writing duplicate cycles.

## Architecture

The main components are:

- `run_collector.py`: safe command dispatcher, logging, and single-instance lock.
- `collector.py`: minute-aligned scheduling, independent per-press polling, OPC UA reads, and sample writes.
- `tag_loader.py`: complete CSV validation and per-press atomic tag synchronization.
- `db.py`: MySQL schema, UTC conversion, statistics, and retention cleanup.
- `dashboard_app.py`: optional MySQL-backed status dashboard, separate from polling.

Each poll creates a `poll_runs` row and one `machine_poll_runs` row per attempted press. If one press is offline, its error is recorded and the other press is still attempted. A new OPC UA client is created on each later cycle, so a failed session is retried. Individual bad reads produce bad-quality sample rows without preventing good tags from being saved.

## Presses included

The only default machines are the canonical names:

- `Press 14`
- `Press 15`

Authentication and endpoint overrides are configured independently for each press. No endpoint URL, username, password, certificate, security policy, or token policy is supplied by this repository.

## Repository structure

```text
.
├── .env.example
├── Tag_Files/
│   ├── README.md
│   ├── Press_14_opcua_discovered_tags.csv  # administrator supplies this
│   └── Press_15_opcua_discovered_tags.csv  # administrator supplies this
├── collector.py
├── config.py
├── dashboard_app.py
├── dashboard_queries.py
├── db.py
├── requirements.txt
├── run_collector.py
├── tag_loader.py
├── test_mysql_connection.py
└── tests/
```

Runtime files are isolated from the Pinch application:

- lock: `data/press_opcua_collector.lock`
- collector log: `logs/press_opcua_collector.log`
- dashboard log: `logs/press_opcua_collector_dashboard.log`

## Database name

The default MySQL database is `press_opcua_collector`. `MYSQL_DATABASE` can select another database, but production should retain this dedicated name.

The application does not create the `press_opcua_collector` database automatically. The administrator must create it manually before running any application database command. The existing `opcua_collector` database is not used or modified.

## Database schema

The logical schema remains:

- `machines`: one row per press; `machine_name` is unique.
- `tags`: tag definitions and enabled state.
- `tag_samples`: UTC values, quality, status code, and read errors.
- `poll_runs`: overall collection-cycle results.
- `machine_poll_runs`: independent result for each press in each cycle.

The `tags` unique key is `(machine_id, node_id)`. Therefore Press 14 and Press 15 may use the same OPC UA node ID without conflict. CSV synchronization never deletes tag records or historical samples.

## CSV filenames

Add both real discovery exports before synchronization:

```text
Tag_Files/Press_14_opcua_discovered_tags.csv
Tag_Files/Press_15_opcua_discovered_tags.csv
```

Only these canonical filenames are synchronized. Press 14 and Press 15 CSV files must be added before tag synchronization. The repository deliberately does not include populated production CSVs.

## CSV required columns

The existing discovered-tag CSV format remains supported. Preferred fields are:

```text
machine_name,endpoint_url,node_id,opc_path,display_name,browse_name,data_type,parent_branch
```

Every importable row requires nonblank `machine_name`, `endpoint_url`, and `node_id`. `machine_name` must exactly match the file’s canonical press name. A file must use one consistent endpoint URL. Duplicate node IDs in one press CSV are rejected; the same node ID in the other press CSV is valid.

The whole machine CSV is validated before that machine is modified. Every rejection identifies the CSV filename, row number, invalid field, and reason.

## CSV example

This is a format example with placeholders, not a production endpoint or node ID:

```csv
machine_name,endpoint_url,node_id,opc_path,display_name,browse_name,data_type,parent_branch
Press 14,opc.tcp://<press-14-host>:<port>,ns=<namespace>;s=<identifier>,<opc-path>,<display-name>,<browse-name>,<data-type>,<parent-branch>
```

Use the real rows produced by discovery. Do not copy the placeholder row into the production CSV.

## Environment variables

Copy `.env.example` to `.env`; `python-dotenv` loads it automatically. OS or systemd environment values take precedence over values in `.env`.

| Variable | Purpose | Default |
|---|---|---|
| `MYSQL_HOST` | MySQL host | `127.0.0.1` |
| `MYSQL_PORT` | MySQL TCP port | `3306` |
| `MYSQL_USER` | Dedicated MySQL user | empty |
| `MYSQL_PASSWORD` | MySQL password | empty |
| `MYSQL_DATABASE` | Independent database | `press_opcua_collector` |
| `MYSQL_SSL_CA` | Optional CA certificate path | empty |
| `POLL_INTERVAL_SECONDS` | Poll interval | `60` |
| `OPCUA_READ_BATCH_SIZE` | Maximum tags per OPC UA read batch | `100` when empty |
| `PRESS_14_ENDPOINT_URL` | Optional Press 14 CSV endpoint override | empty |
| `PRESS_14_AUTH_MODE` | Press 14 authentication mode | `anonymous` when empty |
| `PRESS_14_USERNAME` | Press 14 OPC UA username | empty |
| `PRESS_14_PASSWORD` | Press 14 OPC UA password | empty |
| `PRESS_15_ENDPOINT_URL` | Optional Press 15 CSV endpoint override | empty |
| `PRESS_15_AUTH_MODE` | Press 15 authentication mode | `anonymous` when empty |
| `PRESS_15_USERNAME` | Press 15 OPC UA username | empty |
| `PRESS_15_PASSWORD` | Press 15 OPC UA password | empty |
| `DASHBOARD_HOST` | Optional dashboard bind address | `0.0.0.0` |
| `DASHBOARD_PORT` | Optional Press dashboard port | `5051` |

Environment endpoint values override endpoint values in CSV files. `python run_collector.py --show-config` prints resolved non-secret values and only booleans for secret configuration.

## Authentication configuration

The exact accepted values for each `PRESS_14_AUTH_MODE` and `PRESS_15_AUTH_MODE` setting are:

- `anonymous`: no OPC UA username or password is sent.
- `username_password`: the corresponding press username and password are sent using the OPC UA client’s standard username/password support.

Authentication is independent per press. For example, one may be `anonymous` while the other is `username_password`. Invalid mode text is rejected. This project does not guess security policies, certificate paths, or token-policy values; if a server requires capabilities beyond these supported modes, update and review the client configuration before deployment.

## Linux VM prerequisites

Install Git, Python 3 with `venv`, a compatible MySQL client/server, compiler/runtime packages required by the Python dependencies, and network access from the VM to the configured MySQL and OPC UA endpoints. Use a non-root Linux service account with read access to the application and `.env`, plus write access to `data/` and `logs/`.

Example Debian/Ubuntu package preparation:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip default-mysql-client
```

## Clone or pull from GitHub

First installation:

```bash
git clone <repository-url>
cd <repository-folder>
```

Existing checkout:

```bash
cd <repository-folder>
git pull
```

If the VM tracks a specific non-default branch, inspect it rather than guessing:

```bash
git branch --show-current
git pull origin <branch-name>
```

## Create the Python virtual environment

Run once for a new checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Activate the environment again in each new interactive shell:

```bash
source .venv/bin/activate
```

## Install dependencies

The initial `pip install -r requirements.txt` above is required once. Run it again after a Git update changes `requirements.txt`:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Create the MySQL database

Run this once as a MySQL administrator. The application will not do it:

```sql
CREATE DATABASE press_opcua_collector
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
```

Grant a dedicated non-administrator application account only the privileges it needs on `press_opcua_collector.*`. Do not grant access to the existing `opcua_collector` database merely for this application.

## Configure .env

Run once, then edit whenever endpoints or credentials change:

```bash
cp .env.example .env
nano .env
```

At minimum, configure `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, and both authentication modes. Configure each `PRESS_*_ENDPOINT_URL` only when it should override the corresponding CSV endpoint. Keep `MYSQL_DATABASE=press_opcua_collector` and `POLL_INTERVAL_SECONDS=60` for the standard deployment.

Protect the secret file:

```bash
chmod 600 .env
```

Review resolved non-secret settings without connecting to MySQL or OPC UA:

```bash
python run_collector.py --show-config
```

## Test the MySQL connection

Testing command; it connects to MySQL but does not create tables:

```bash
python test_mysql_connection.py
```

Confirm the reported database is `press_opcua_collector` and the session time zone is `+00:00`.

## Initialize database tables

Run once after the administrator has created the empty database:

```bash
python run_collector.py --init-db
```

`--init-db` creates the five required tables inside the selected database; it does not create the database server or database itself. Normal application startup does not automatically initialize or migrate schema.

## Validate and synchronize CSV tag definitions

Add both real Press CSV files, then run this after initial table creation and after every CSV update:

```bash
python run_collector.py --sync-tags
```

`--sync-tags` validates both files, processes each press independently, and prints per-press counts for `inserted`, `updated`, `unchanged`, `re-enabled`, `disabled`, and `rejected`. A validation failure causes no partial tag-definition change for that press. `--sync-tags` does not start continuous collection.

The deprecated `--init-only` flag remains a backward-compatible alias for `--sync-tags`; it no longer initializes tables.

## Test Press 14 once

Testing command; it connects to MySQL and the Press 14 OPC UA endpoint, polls immediately, saves results, and exits:

```bash
python run_collector.py --once --machine "Press 14" --no-sleep-align
```

## Test Press 15 once

Testing command; it connects to MySQL and the Press 15 OPC UA endpoint, polls immediately, saves results, and exits:

```bash
python run_collector.py --once --machine "Press 15" --no-sleep-align
```

## Test both presses once

Testing command; it polls both presses immediately and exits:

```bash
python run_collector.py --once --no-sleep-align
```

`--once` polls and exits. Failure of one press does not prevent the other press from being attempted.

## Start continuous collection

Continuous production command:

```bash
python run_collector.py
```

`python run_collector.py` starts continuous minute-by-minute collection. With the default 60-second interval, cycles align to wall-clock minute boundaries. The single-instance lock prevents a second local collector from overlapping or duplicating writes.

## Verify samples using SQL

Select the dedicated database first:

```sql
USE press_opcua_collector;
```

Verify Press 14 exists:

```sql
SELECT id, machine_name, endpoint_url, auth_mode, enabled
FROM machines
WHERE machine_name = 'Press 14';
```

Verify Press 15 exists:

```sql
SELECT id, machine_name, endpoint_url, auth_mode, enabled
FROM machines
WHERE machine_name = 'Press 15';
```

Tag counts by machine:

```sql
SELECT m.machine_name,
       COUNT(t.id) AS total_tags,
       SUM(CASE WHEN t.enabled = 1 THEN 1 ELSE 0 END) AS enabled_tags
FROM machines m
LEFT JOIN tags t ON t.machine_id = m.id
WHERE m.machine_name IN ('Press 14', 'Press 15')
GROUP BY m.id, m.machine_name
ORDER BY m.machine_name;
```

Latest poll result by machine:

```sql
SELECT m.machine_name, mpr.finished_at_utc, mpr.connection_ok,
       mpr.tags_attempted, mpr.tags_ok, mpr.tags_failed, mpr.error_text
FROM machines m
LEFT JOIN machine_poll_runs mpr
  ON mpr.id = (
    SELECT x.id
    FROM machine_poll_runs x
    WHERE x.machine_id = m.id
    ORDER BY COALESCE(x.finished_at_utc, x.started_at_utc) DESC, x.id DESC
    LIMIT 1
  )
WHERE m.machine_name IN ('Press 14', 'Press 15')
ORDER BY m.machine_name;
```

Latest sample timestamp by machine:

```sql
SELECT m.machine_name, MAX(ts.sampled_at_utc) AS latest_sample_utc
FROM machines m
LEFT JOIN tag_samples ts ON ts.machine_id = m.id
WHERE m.machine_name IN ('Press 14', 'Press 15')
GROUP BY m.id, m.machine_name
ORDER BY m.machine_name;
```

Sample counts from the last 10 minutes:

```sql
SELECT m.machine_name,
       COUNT(*) AS samples,
       SUM(CASE WHEN ts.quality = 'good' THEN 1 ELSE 0 END) AS good_samples,
       SUM(CASE WHEN ts.quality <> 'good' THEN 1 ELSE 0 END) AS failed_samples
FROM tag_samples ts
JOIN machines m ON m.id = ts.machine_id
WHERE ts.sampled_at_utc >= UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE
GROUP BY m.id, m.machine_name
ORDER BY m.machine_name;
```

Failed or bad-quality reads:

```sql
SELECT ts.sampled_at_utc, m.machine_name, t.node_id,
       ts.quality, ts.status_code, ts.error_text
FROM tag_samples ts
JOIN machines m ON m.id = ts.machine_id
JOIN tags t ON t.id = ts.tag_id
WHERE ts.quality <> 'good'
ORDER BY ts.sampled_at_utc DESC, ts.id DESC
LIMIT 100;
```

Most recent values for a selected tag:

```sql
SELECT ts.sampled_at_utc, ts.source_timestamp_utc,
       ts.value_numeric, ts.value_text, ts.quality, ts.status_code, ts.error_text
FROM tag_samples ts
JOIN machines m ON m.id = ts.machine_id
JOIN tags t ON t.id = ts.tag_id
WHERE m.machine_name = 'Press 14'
  AND t.node_id = '<selected-node-id>'
ORDER BY ts.sampled_at_utc DESC, ts.id DESC
LIMIT 20;
```

## Database statistics

Operational command; it connects to MySQL and reads statistics:

```bash
python run_collector.py --db-stats
```

The output includes enabled machine/tag counts, sample quality counts, timestamp range, poll-run counts, estimated retained rows, and database size.

## Updating CSV files later

After replacing either discovery export, keep both canonical files present and run:

```bash
python run_collector.py --sync-tags
```

This command is required after each CSV update. It is not part of normal polling startup and it never starts collection.

## Adding or removing tags safely

Synchronization behavior is intentionally non-destructive:

- new CSV tags are inserted;
- changed metadata is updated;
- previously disabled tags that return are re-enabled;
- unchanged tags remain enabled;
- tags absent from the latest machine CSV are disabled, not deleted;
- historical samples and tag records are not deleted during synchronization.

Because each file is validated before its transaction begins, a rejected machine CSV cannot partially synchronize that machine. The other valid press may still synchronize.

## Cleanup and retention

Defaults are 14 days for good samples, 60 days for bad samples, and 14 days for poll history. Machines and tags are not deleted by retention cleanup. Continuous collection checks cleanup hourly; cleanup is also available as an explicit maintenance command:

```bash
python run_collector.py --cleanup-now
```

`--cleanup-now` deletes data older than configured retention and should be run only with an approved retention policy. It is unrelated to CSV synchronization.

## Logs and troubleshooting

Collector logs are written to `logs/press_opcua_collector.log`; dashboard logs use `logs/press_opcua_collector_dashboard.log`. Both rotate at 10 MiB with five backups. Under systemd, also inspect the journal.

Useful safe diagnostic commands:

```bash
python run_collector.py --help
python run_collector.py --show-config
```

If a press fails while the other succeeds, inspect its latest `machine_poll_runs.error_text` and bad samples. Verify endpoint precedence, auth-mode spelling, VM routing/firewall access, server availability, and credentials. A failed OPC UA session is retried on the next cycle. If the collector reports another instance, inspect the active process; do not delete the lock while a collector is running.

The optional dashboard uses environment defaults and does not start automatically:

```bash
python dashboard_app.py --waitress
```

Its default bind is `0.0.0.0:5051`, configurable with `DASHBOARD_HOST` and `DASHBOARD_PORT`. Restrict dashboard network access because it exposes maintenance actions.

## Running with systemd

Create `/etc/systemd/system/press-opcua-collector.service` with a non-root user and a neutral installation path:

```ini
[Unit]
Description=Press 14-15 OPC UA Collector
After=network-online.target mysql.service
Wants=network-online.target

[Service]
Type=simple
User=<linux-user>
Group=<linux-group>
WorkingDirectory=/opt/press-opcua-collector
EnvironmentFile=/opt/press-opcua-collector/.env
ExecStart=/opt/press-opcua-collector/.venv/bin/python /opt/press-opcua-collector/run_collector.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Replace the user, group, and path placeholders. Ensure the service user owns or can write `data/` and `logs/`; do not run as root unless an explicitly reviewed environment requires it.

Install and operate the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable press-opcua-collector
sudo systemctl start press-opcua-collector
sudo systemctl status press-opcua-collector
sudo journalctl -u press-opcua-collector -f
sudo systemctl restart press-opcua-collector
sudo systemctl stop press-opcua-collector
```

## Updating the application from GitHub

Stop the service before changing source or dependencies:

```bash
sudo systemctl stop press-opcua-collector
cd /opt/press-opcua-collector
git branch --show-current
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

If a specific branch is required, use `git pull origin <branch-name>` with the branch reported by `git branch --show-current`. Do not run `--init-db` or `--sync-tags` merely because source was pulled; run them only when the release/schema instructions or CSV changes require them.

## Safe restart procedure

Use this sequence to avoid overlap and unexpected definition changes:

```bash
sudo systemctl stop press-opcua-collector
sudo systemctl status press-opcua-collector
cd /opt/press-opcua-collector
source .venv/bin/activate
python run_collector.py --show-config
sudo systemctl start press-opcua-collector
sudo systemctl status press-opcua-collector
sudo journalctl -u press-opcua-collector -f
```

If CSVs changed, run `python run_collector.py --sync-tags` after stopping the service and before starting it. A normal restart does not import CSVs or alter tag definitions.

## Common commands

```bash
# Safe, no external connection
python run_collector.py --help
python run_collector.py --show-config

# Database preparation/operations
python test_mysql_connection.py
python run_collector.py --init-db
python run_collector.py --sync-tags
python run_collector.py --db-stats
python run_collector.py --cleanup-now

# Testing polls (poll once and exit)
python run_collector.py --once --machine "Press 14" --no-sleep-align
python run_collector.py --once --machine "Press 15" --no-sleep-align
python run_collector.py --once --no-sleep-align

# Continuous production
python run_collector.py
```

## Important safety notes

- The application does not create the `press_opcua_collector` database automatically.
- `--init-db` creates tables inside the selected database; it does not create the database server or database itself.
- `--sync-tags` does not start continuous collection.
- `--once` polls and exits.
- `python run_collector.py` starts continuous minute-by-minute collection.
- Press 14 and Press 15 CSV files must be added before tag synchronization.
- The existing `opcua_collector` database is not used or modified.
- No automatic migration or copying from a Pinch database exists.
- Historical samples are not deleted during CSV synchronization.
- Removed CSV tags are disabled rather than deleted.
- Database timestamps are stored in UTC.
- Never commit `.env`, credentials, production certificates, or real secrets.
- Stop the service before manual polling or tag synchronization so operational commands are deliberate and easy to audit.
