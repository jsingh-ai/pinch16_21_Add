# Press 14–15 OPC UA Collector

This is a small command-line collector. It reads the two Press CSV files, connects to each OPC UA server with SecurityPolicy `None` and no username/password, and writes tag values to MySQL every configured number of minutes.

There is no dashboard, web server, cleanup process, statistics command, migration command, or separate CSV-loader command.

## Files

```text
run_collector.py       collector, CSV reader, and command line
clear_collector_data.py explicit tag/sample reset command
config.py              .env settings
db.py                  MySQL connection and table definitions
.env.example           safe configuration template
Tag_Files/              real CSV files go here
```

The required CSV filenames are:

```text
Tag_Files/Press_14_opcua_discovered_tags.csv
Tag_Files/Press_15_opcua_discovered_tags.csv
```

## CSV format

The preferred header is:

```csv
machine_name,endpoint_url,node_id,opc_path,display_name,browse_name,data_type,parent_branch
```

Every row requires `machine_name`, `endpoint_url`, and `node_id`. Machine names must be exactly `Press 14` or `Press 15`, matching the filename. Duplicate node IDs inside one press CSV are rejected. The same node ID may appear in both press files.

Both complete CSV files are validated before the database is changed. The normal collector command reads and synchronizes the CSV definitions once at startup. Tags removed from a CSV are disabled; historical samples are not deleted.

## Install

```bash
git clone <repository-url>
cd <repository-folder>

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

For an existing checkout:

```bash
cd <repository-folder>
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
nano .env
```

Configuration:

```text
MYSQL_HOST=                  MySQL host
MYSQL_PORT=3306              MySQL port
MYSQL_USER=                  MySQL user
MYSQL_PASSWORD=              MySQL password
MYSQL_DATABASE=press_opcua_collector
MYSQL_SSL_CA=                optional MySQL CA file
POLL_INTERVAL_MINUTES=1      time between poll starts
OPCUA_READ_BATCH_SIZE=100    tags per OPC UA batch read
MYSQL_INSERT_BATCH_SIZE=2000 maximum rows per MySQL batch insert
PRESS_14_ENDPOINT_URL=       optional override of the Press 14 CSV endpoint
PRESS_15_ENDPOINT_URL=       optional override of the Press 15 CSV endpoint
```

There are no OPC UA authentication variables. The script does not set a security string, username, or password. It uses the OPC UA client default SecurityPolicy `None` connection.

Environment endpoint values override the endpoint in the corresponding CSV. Leave them empty to use the CSV endpoint.

## Add the real CSV files

```bash
cp <real-press-14-csv> Tag_Files/Press_14_opcua_discovered_tags.csv
cp <real-press-15-csv> Tag_Files/Press_15_opcua_discovered_tags.csv
```

The repository does not contain fabricated production tag data.

## Create the database

The script does not create the MySQL database. Create it once as a MySQL administrator:

```sql
CREATE DATABASE press_opcua_collector
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
```

Grant the configured MySQL user access only to this database. The existing `opcua_collector` database is not used or modified.

## Create the tables

Run this once after creating the database and configuring `.env`:

```bash
python run_collector.py --init-db
```

It creates:

- `machines`: Press name and endpoint.
- `tags`: CSV tag definitions, unique by `(machine_id, node_id)`.
- `tag_samples`: timestamp, value, quality, status, and error history.

`--init-db` creates tables only. It does not poll OPC UA or read/import the CSV files.

## Test one poll

Poll both presses immediately and exit:

```bash
python run_collector.py --once
```

Poll only one press:

```bash
python run_collector.py --once --machine "Press 14"
python run_collector.py --once --machine "Press 15"
```

The script reads and synchronizes both CSVs at startup, then polls. If one press is offline, it records failed sample rows for that press and continues to the other press. One failed tag does not prevent other tag values from being saved.

## Run continuously

```bash
python run_collector.py
```

The first poll runs immediately. Later poll starts use `POLL_INTERVAL_MINUTES`. For example:

```text
POLL_INTERVAL_MINUTES=1      every minute
POLL_INTERVAL_MINUTES=5      every five minutes
POLL_INTERVAL_MINUTES=0.5    every 30 seconds
```

You can temporarily override the configured value:

```bash
python run_collector.py --interval-minutes 5
```

Polling cycles do not overlap. A Press-specific lock prevents two collector processes from running simultaneously. Logs are written to `logs/press_opcua_collector.log`. Database timestamps are stored in UTC.

Continuous mode writes one summary line after each cycle with the elapsed time and
good/bad counts for each press. It does not print every non-good tag. Use `--once`
when you want the verbose individual tag list for troubleshooting.

The collector uses batched OPC UA reads and batched MySQL inserts. Insert batches are
bounded by `MYSQL_INSERT_BATCH_SIZE`, and the MySQL connection is checked/reconnected
before each cycle. Per-cycle value/result lists are released after the cycle; they do
not accumulate in Python memory. Rotating logs prevent unlimited log-file growth.

At 2,024 tags per minute, the database receives about 2.9 million sample rows per day.
Database storage growth—not collector process memory—is the main long-term capacity
concern. Use `clear_collector_data.py` while testing. Before production, choose an
explicit retention/archive policy rather than silently deleting historical data.

## Updating CSV files

Stop the collector, replace either CSV, and restart:

```bash
sudo systemctl stop press-opcua-collector
cp <updated-press-14-csv> Tag_Files/Press_14_opcua_discovered_tags.csv
cp <updated-press-15-csv> Tag_Files/Press_15_opcua_discovered_tags.csv
sudo systemctl start press-opcua-collector
```

The next start validates and synchronizes the CSVs before polling.

## Clear test data before final use

To permanently delete all collected samples and tag definitions while retaining the
`machines` rows and table structure, stop the collector and run:

```bash
python clear_collector_data.py --yes
```

The `--yes` flag is required. The script deletes `tag_samples` first and then `tags`
to preserve foreign-key integrity. The next collector start recreates tag definitions
from both CSV files before polling. This command cannot be undone.

## Minimal systemd service

Example `/etc/systemd/system/press-opcua-collector.service`:

```ini
[Unit]
Description=Press 14-15 OPC UA Collector
After=network-online.target
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

[Install]
WantedBy=multi-user.target
```

Use a non-root service user, replace the path/user placeholders, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable press-opcua-collector
sudo systemctl start press-opcua-collector
sudo systemctl status press-opcua-collector
sudo journalctl -u press-opcua-collector -f
```
