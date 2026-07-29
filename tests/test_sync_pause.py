import tempfile
import unittest
from pathlib import Path

from sync_tool.config import AppConfig, Config, MySQLConfig, SafetyConfig
from sync_tool.store import SyncStore
from sync_tool.sync import SyncEngine, SyncManager, SyncPaused


def make_engine(tmp_path):
    store = SyncStore(Path(tmp_path) / "sync.db")
    store.init()
    config = Config(
        prod=MySQLConfig(host="prod", user="reader", database="prod_db"),
        test=MySQLConfig(host="test", user="writer", database="test_db"),
        app=AppConfig(data_dir=Path(tmp_path) / "data", log_dir=Path(tmp_path) / "logs"),
        safety=SafetyConfig(),
        path=Path(tmp_path) / "config.json",
        exists=False,
    )
    return SyncEngine(config, store), store


def create_run(store, status="queued"):
    return store.create_run(
        {
            "id": "run-1",
            "job_id": None,
            "name": "pause-test",
            "tables": ["orders"],
            "mode": "replace",
            "where_clause": "",
            "batch_size": 5000,
            "create_missing_tables": False,
            "sync_strategy": "auto",
            "status": status,
            "total_rows": 10,
        }
    )


class SyncPauseTests(unittest.TestCase):
    def test_pause_request_marks_table_paused_at_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="pause_requested")
            store.create_run_table(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "total_rows": 10,
                    "processed_rows": 5,
                    "offset_value": 5,
                    "status": "running",
                }
            )

            with self.assertRaises(SyncPaused):
                engine._raise_if_pause_requested("run-1", "orders")

            table = store.get_run_table("run-1", "orders")
            self.assertEqual(table["status"], "paused")
            self.assertEqual(table["offset_value"], 5)

    def test_manager_pauses_queued_run_without_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="queued")
            manager = SyncManager(engine)
            try:
                run = manager.pause("run-1")
            finally:
                manager.shutdown()

            self.assertEqual(run["status"], "paused")

    def test_aggregate_shards_keeps_table_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store = make_engine(tmp)
            create_run(store, status="pause_requested")
            store.create_run_table(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "total_rows": 10,
                    "processed_rows": 0,
                    "cursor_field": "id",
                    "status": "running",
                }
            )
            store.create_run_shard(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "shard_index": 0,
                    "processed_rows": 5,
                    "status": "success",
                }
            )
            store.create_run_shard(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "shard_index": 1,
                    "processed_rows": 2,
                    "status": "paused",
                }
            )

            table = store.aggregate_shards("run-1", "orders")

            self.assertEqual(table["status"], "paused")
            self.assertEqual(table["processed_rows"], 7)


if __name__ == "__main__":
    unittest.main()
