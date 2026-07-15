from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


if "pymysql" not in sys.modules:
    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **kwargs: None
    connections = types.ModuleType("pymysql.connections")
    connections.Connection = object
    cursors = types.ModuleType("pymysql.cursors")
    cursors.DictCursor = object
    sys.modules.update(
        {
            "pymysql": pymysql,
            "pymysql.connections": connections,
            "pymysql.cursors": cursors,
        }
    )

if "opcua" not in sys.modules:
    opcua = types.ModuleType("opcua")
    opcua.Client = object
    opcua.ua = types.SimpleNamespace(AttributeIds=types.SimpleNamespace(Value=13))
    sys.modules["opcua"] = opcua

if "filelock" not in sys.modules:
    filelock = types.ModuleType("filelock")
    filelock.FileLock = object
    filelock.Timeout = TimeoutError
    sys.modules["filelock"] = filelock

import config
import db
import run_collector


HEADER = (
    "machine_name,endpoint_url,node_id,opc_path,display_name,"
    "browse_name,data_type,parent_branch\n"
)


def write_csv(path: Path, machine: str, endpoint: str, node_ids: list[str]) -> None:
    rows = [f"{machine},{endpoint},{node_id},,,,," for node_id in node_ids]
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


class ConfigurationTests(unittest.TestCase):
    def test_simple_defaults(self) -> None:
        self.assertEqual(config.DEFAULT_MYSQL_DATABASE, "press_opcua_collector")
        self.assertEqual(config.DEFAULT_POLL_INTERVAL_MINUTES, 1.0)
        self.assertEqual(config.PRESS_NAMES, ("Press 14", "Press 15"))

    def test_no_opcua_authentication_configuration_exists(self) -> None:
        self.assertFalse(hasattr(config, "AUTH_MODE_ANONYMOUS"))
        self.assertFalse(hasattr(config, "MACHINE_AUTH_CONFIG"))

    def test_schema_is_only_three_required_tables(self) -> None:
        schema = "\n".join(db.SCHEMA_STATEMENTS)
        self.assertIn("CREATE TABLE IF NOT EXISTS machines", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS tags", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS tag_samples", schema)
        self.assertNotIn("poll_runs", schema)


class CsvTests(unittest.TestCase):
    def test_reads_both_press_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            write_csv(
                directory / "Press_14_opcua_discovered_tags.csv",
                "Press 14",
                "opc.tcp://press14.invalid:4840",
                ["ns=2;s=Shared"],
            )
            write_csv(
                directory / "Press_15_opcua_discovered_tags.csv",
                "Press 15",
                "opc.tcp://press15.invalid:4840",
                ["ns=2;s=Shared"],
            )
            machines = run_collector.read_csv_files(directory)
        self.assertEqual([machine.machine_name for machine in machines], ["Press 14", "Press 15"])
        self.assertEqual(machines[0].tags[0].node_id, machines[1].tags[0].node_id)

    def test_duplicate_node_in_one_csv_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Press_14_opcua_discovered_tags.csv"
            write_csv(
                path,
                "Press 14",
                "opc.tcp://press14.invalid:4840",
                ["duplicate", "duplicate"],
            )
            with self.assertRaises(run_collector.CsvValidationError) as raised:
                run_collector.read_machine_csv(path, "Press 14")
        self.assertIn("row 3, field node_id", str(raised.exception))

    def test_required_field_error_is_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Press_15_opcua_discovered_tags.csv"
            write_csv(path, "Press 15", "opc.tcp://press15.invalid:4840", [""])
            with self.assertRaises(run_collector.CsvValidationError) as raised:
                run_collector.read_machine_csv(path, "Press 15")
        message = str(raised.exception)
        self.assertIn(path.name, message)
        self.assertIn("row 2, field node_id", message)
        self.assertIn("required value is blank", message)


class PollingTests(unittest.TestCase):
    def test_no_empty_batch_request_when_all_node_ids_are_invalid(self) -> None:
        tags = (
            run_collector.RuntimeTag(1, 14, "invalid-a", "A"),
            run_collector.RuntimeTag(2, 14, "invalid-b", "B"),
        )
        batch_calls: list[object] = []

        def invalid_node(node_id):
            raise ValueError(f"cannot parse {node_id}")

        client = types.SimpleNamespace(
            get_node=invalid_node,
            uaclient=types.SimpleNamespace(
                get_attributes=lambda *args: batch_calls.append(args)
            ),
        )
        results = run_collector.read_tags(client, tags)
        self.assertEqual(batch_calls, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.status_code == "ReadError" for result in results))

    def test_batch_read_returns_values(self) -> None:
        tags = (
            run_collector.RuntimeTag(1, 14, "a", "A"),
            run_collector.RuntimeTag(2, 14, "b", "B"),
        )

        def get_attributes(node_ids, attribute_id):
            return [
                types.SimpleNamespace(
                    Value=types.SimpleNamespace(Value=value),
                    StatusCode="Good",
                    SourceTimestamp=None,
                    ServerTimestamp=None,
                )
                for value in (10, 20)
            ]

        client = types.SimpleNamespace(
            get_node=lambda node_id: types.SimpleNamespace(nodeid=node_id),
            uaclient=types.SimpleNamespace(get_attributes=get_attributes),
        )
        results = run_collector.read_tags(client, tags)
        self.assertEqual([result.value for result in results], [10, 20])

    def test_poll_client_uses_no_username_password_or_security_configuration(self) -> None:
        calls: list[str] = []

        class AnonymousClient:
            def __init__(self, endpoint, timeout):
                calls.append("created")

            def connect(self):
                calls.append("connected")

            def disconnect(self):
                calls.append("disconnected")

        machine = run_collector.RuntimeMachine(14, "Press 14", "opc.tcp://press14.invalid:4840", ())
        with (
            patch.object(run_collector, "Client", AnonymousClient),
            patch.object(run_collector, "read_tags", return_value=[]),
            patch.object(run_collector, "save_results"),
        ):
            run_collector.poll_machine(object(), machine, datetime.now(timezone.utc))
        self.assertEqual(calls, ["created", "connected", "disconnected"])

    def test_one_press_failure_does_not_stop_the_other(self) -> None:
        machines = (
            run_collector.RuntimeMachine(14, "Press 14", "endpoint-14", ()),
            run_collector.RuntimeMachine(15, "Press 15", "endpoint-15", ()),
        )
        called: list[str] = []

        def poll(connection, machine, sampled_at):
            called.append(machine.machine_name)
            if machine.machine_name == "Press 14":
                raise RuntimeError("offline")

        with (
            patch.object(run_collector, "poll_machine", side_effect=poll),
            patch.object(run_collector.LOGGER, "exception"),
        ):
            run_collector.poll_all(object(), machines)
        self.assertEqual(called, ["Press 14", "Press 15"])


if __name__ == "__main__":
    unittest.main()
