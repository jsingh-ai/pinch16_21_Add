from __future__ import annotations

import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


# Keep unit tests independent of optional runtime packages and external systems.
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

import collector
import config
import tag_loader


CSV_HEADER = (
    "machine_name,endpoint_url,node_id,opc_path,display_name,"
    "browse_name,data_type,parent_branch\n"
)


def write_csv(directory: Path, filename: str, rows: list[str]) -> Path:
    path = directory / filename
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def tag(node_id: str, display_name: str = "Tag") -> tag_loader.TagDefinition:
    return tag_loader.TagDefinition(node_id, None, display_name, None, None, None)


class ConfigTests(unittest.TestCase):
    def test_default_database_name(self) -> None:
        self.assertEqual(config.DEFAULT_MYSQL_DATABASE, "press_opcua_collector")

    def test_only_press_14_and_15_are_default(self) -> None:
        self.assertEqual(config.DEFAULT_MACHINE_NAMES, ["Press 14", "Press 15"])
        self.assertEqual(set(config.MACHINE_AUTH_CONFIG), {"Press 14", "Press 15"})

    def test_endpoint_environment_override_wins_over_csv(self) -> None:
        machine_config = config.MACHINE_AUTH_CONFIG["Press 14"]
        with patch.dict(machine_config, {"endpoint_url": "opc.tcp://override.invalid:4840"}):
            resolved = config.get_machine_config("Press 14", "opc.tcp://csv.invalid:4840")
        self.assertEqual(resolved["endpoint_url"], "opc.tcp://override.invalid:4840")

    def test_press_specific_runtime_paths(self) -> None:
        self.assertEqual(config.COLLECTOR_LOCK_PATH.name, "press_opcua_collector.lock")
        self.assertEqual(config.COLLECTOR_LOG_PATH.name, "press_opcua_collector.log")

    def test_default_poll_interval_is_60_seconds(self) -> None:
        self.assertEqual(config.DEFAULT_POLL_INTERVAL_SECONDS, 60)


class CsvValidationTests(unittest.TestCase):
    def test_press_14_csv_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_file = write_csv(
                Path(temp_dir),
                "Press_14_opcua_discovered_tags.csv",
                ["Press 14,opc.tcp://host.invalid:4840,ns=2;s=Shared,,,,,"],
            )
            parsed = tag_loader.validate_machine_csv(csv_file, "Press 14")
        self.assertEqual(parsed.machine_name, "Press 14")
        self.assertEqual([item.node_id for item in parsed.tags], ["ns=2;s=Shared"])

    def test_press_15_csv_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_file = write_csv(
                Path(temp_dir),
                "Press_15_opcua_discovered_tags.csv",
                ["Press 15,opc.tcp://host.invalid:4840,ns=2;s=Shared,,,,,"],
            )
            parsed = tag_loader.validate_machine_csv(csv_file, "Press 15")
        self.assertEqual(parsed.machine_name, "Press 15")
        self.assertEqual(len(parsed.tags), 1)

    def test_duplicate_node_id_is_rejected_within_one_press(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_file = write_csv(
                Path(temp_dir),
                "Press_14_opcua_discovered_tags.csv",
                [
                    "Press 14,opc.tcp://host.invalid:4840,ns=2;s=Duplicate,,,,,",
                    "Press 14,opc.tcp://host.invalid:4840,ns=2;s=Duplicate,,,,,",
                ],
            )
            with self.assertRaises(tag_loader.CsvValidationError) as raised:
                tag_loader.validate_machine_csv(csv_file, "Press 14")
        issue = raised.exception.issues[0]
        self.assertEqual(issue.filename, "Press_14_opcua_discovered_tags.csv")
        self.assertEqual(issue.row_number, 3)
        self.assertEqual(issue.field, "node_id")
        self.assertIn("duplicates row 2", issue.reason)

    def test_same_node_id_is_allowed_on_different_presses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            press14 = write_csv(
                directory,
                "Press_14_opcua_discovered_tags.csv",
                ["Press 14,opc.tcp://host14.invalid:4840,ns=2;s=Shared,,,,,"],
            )
            press15 = write_csv(
                directory,
                "Press_15_opcua_discovered_tags.csv",
                ["Press 15,opc.tcp://host15.invalid:4840,ns=2;s=Shared,,,,,"],
            )
            parsed14 = tag_loader.validate_machine_csv(press14, "Press 14")
            parsed15 = tag_loader.validate_machine_csv(press15, "Press 15")
        self.assertEqual(parsed14.tags[0].node_id, parsed15.tags[0].node_id)

    def test_required_field_error_has_file_row_field_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_file = write_csv(
                Path(temp_dir),
                "Press_15_opcua_discovered_tags.csv",
                ["Press 15,opc.tcp://host.invalid:4840,,,,,,"],
            )
            with self.assertRaises(tag_loader.CsvValidationError) as raised:
                tag_loader.validate_machine_csv(csv_file, "Press 15")
        issue = raised.exception.issues[0]
        self.assertEqual(issue.filename, csv_file.name)
        self.assertEqual(issue.row_number, 2)
        self.assertEqual(issue.field, "node_id")
        self.assertEqual(issue.reason, "required value is blank")

    def test_invalid_press_does_not_prevent_other_press_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            write_csv(
                directory,
                "Press_14_opcua_discovered_tags.csv",
                ["Wrong Name,opc.tcp://host.invalid:4840,ns=2;s=A,,,,,"],
            )
            write_csv(
                directory,
                "Press_15_opcua_discovered_tags.csv",
                ["Press 15,opc.tcp://host.invalid:4840,ns=2;s=B,,,,,"],
            )
            synced: list[str] = []

            def fake_sync(conn, validated):
                synced.append(validated.machine_name)
                return tag_loader.MachineImportSummary(validated.machine_name, validated.filename, inserted=1)

            with (
                patch.object(tag_loader, "sync_validated_machine", side_effect=fake_sync),
                patch.object(tag_loader.LOGGER, "error"),
            ):
                summaries = tag_loader.load_tag_files(object(), directory)
        self.assertEqual(synced, ["Press 15"])
        self.assertEqual(summaries[0].rejected, 1)
        self.assertEqual(summaries[1].inserted, 1)


class SynchronizationPlanTests(unittest.TestCase):
    def test_removed_tag_becomes_disabled(self) -> None:
        existing = {
            "kept": {"id": 1, "enabled": 1, "opc_path": None, "display_name": "Tag", "browse_name": None, "data_type": None, "parent_branch": None},
            "removed": {"id": 2, "enabled": 1, "opc_path": None, "display_name": "Old", "browse_name": None, "data_type": None, "parent_branch": None},
        }
        plan = tag_loader.plan_tag_sync(existing, (tag("kept"),))
        self.assertEqual(plan.disabled_ids, (2,))

    def test_disabled_tag_can_be_re_enabled(self) -> None:
        existing = {
            "returning": {"id": 4, "enabled": 0, "opc_path": None, "display_name": "Tag", "browse_name": None, "data_type": None, "parent_branch": None}
        }
        plan = tag_loader.plan_tag_sync(existing, (tag("returning"),))
        self.assertEqual([item.node_id for item in plan.re_enabled], ["returning"])
        self.assertEqual(plan.disabled_ids, ())


class PollIsolationTests(unittest.TestCase):
    def test_supported_opcua_batch_read_is_used(self) -> None:
        tags = [
            collector.TagRecord(1, 14, "node-a", None, "A"),
            collector.TagRecord(2, 14, "node-b", None, "B"),
        ]
        calls: list[tuple[list[str], int]] = []

        def get_attributes(node_ids, attribute_id):
            calls.append((node_ids, attribute_id))
            return [
                types.SimpleNamespace(
                    Value=types.SimpleNamespace(Value=value),
                    StatusCode="Good",
                    SourceTimestamp=None,
                    ServerTimestamp=None,
                )
                for value in (10, 20)
            ]

        fake_client = types.SimpleNamespace(
            get_node=lambda node_id: types.SimpleNamespace(nodeid=node_id),
            uaclient=types.SimpleNamespace(get_attributes=get_attributes),
        )
        results = collector.read_tag_batch(fake_client, tags, batch_size=100)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["node-a", "node-b"])
        self.assertEqual([result[1] for result in results], [10, 20])

    def test_one_press_failure_does_not_prevent_the_other(self) -> None:
        machines = [
            collector.MachineRecord(14, "Press 14", "opc.tcp://host14.invalid:4840", "anonymous"),
            collector.MachineRecord(15, "Press 15", "opc.tcp://host15.invalid:4840", "anonymous"),
        ]
        successful_result = collector.MachinePollResult(
            15, "Press 15", machines[1].endpoint_url,
            datetime.now(timezone.utc), datetime.now(timezone.utc), 0.1,
            1, 1, 0, True, None, [],
        )

        def fake_poll(conn, machine, sampled_at):
            if machine.machine_name == "Press 14":
                raise ConnectionError("offline")
            return successful_result

        with (
            patch.object(collector, "load_enabled_machines", return_value=machines),
            patch.object(collector, "load_enabled_tags_for_machine", return_value=[tag("n")]),
            patch.object(collector, "poll_machine", side_effect=fake_poll) as poll,
            patch.object(collector, "persist_poll_run"),
            patch.object(collector.LOGGER, "exception"),
        ):
            stats = collector.run_poll_cycle(object())
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(stats.machines_failed, 1)
        self.assertEqual(stats.machines_ok, 1)

    def test_one_failed_tag_does_not_prevent_other_tag_from_being_saved(self) -> None:
        machine = collector.MachineRecord(14, "Press 14", "opc.tcp://host.invalid:4840", "anonymous")
        tags = [
            collector.TagRecord(1, 14, "good", None, "Good tag"),
            collector.TagRecord(2, 14, "bad", None, "Bad tag"),
        ]
        fake_client = types.SimpleNamespace(connect=lambda: None, disconnect=lambda: None)
        captured_rows: list[tuple[object, ...]] = []

        class FakeConnection:
            def commit(self):
                pass

            def rollback(self):
                pass

        read_results = [
            (tags[0], 12.5, "Good", None, None, None),
            (tags[1], None, None, None, None, RuntimeError("read failed")),
        ]

        def capture_many(conn, sql, rows):
            captured_rows.extend(rows)

        with (
            patch.object(collector, "load_enabled_tags_for_machine", return_value=tags),
            patch.object(collector, "create_client", return_value=fake_client),
            patch.object(collector, "read_tag_batch", return_value=read_results),
            patch.object(collector, "executemany", side_effect=capture_many),
            patch.object(collector.LOGGER, "warning"),
        ):
            result = collector.poll_machine(FakeConnection(), machine, datetime.now(timezone.utc))
        self.assertEqual(result.tags_ok, 1)
        self.assertEqual(result.tags_failed, 1)
        self.assertEqual(len(captured_rows), 2)
        self.assertEqual([row[7] for row in captured_rows], ["good", "bad"])
        self.assertEqual(captured_rows[1][8], "ReadError")
        self.assertEqual(captured_rows[1][9], "read failed")


if __name__ == "__main__":
    unittest.main()
