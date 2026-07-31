import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from sync_tool.config import AppConfig, Config, MySQLConfig, SafetyConfig
from sync_tool.store import SyncStore, utc_now
from sync_tool.sync import DEFAULT_RUN_WORKERS, SyncCancelled, SyncEngine, SyncManager, SyncPaused


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


def create_run(store, status="queued", *, run_id="run-1", tables=None):
    tables = tables or ["orders"]
    return store.create_run(
        {
            "id": run_id,
            "job_id": None,
            "name": "pause-test",
            "tables": tables,
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

    def test_cancel_request_marks_table_canceled_at_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="cancel_requested")
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

            with self.assertRaises(SyncCancelled):
                engine._raise_if_stop_requested("run-1", "orders")

            table = store.get_run_table("run-1", "orders")
            self.assertEqual(table["status"], "canceled")
            self.assertEqual(table["offset_value"], 5)

    def test_manager_cancels_paused_run_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="paused")
            store.create_run_table(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "total_rows": 10,
                    "processed_rows": 5,
                    "offset_value": 5,
                    "status": "paused",
                }
            )
            manager = SyncManager(engine)
            try:
                run = manager.cancel("run-1")
            finally:
                manager.shutdown()

            table = store.get_run_table("run-1", "orders")
            self.assertEqual(run["status"], "canceled")
            self.assertEqual(table["status"], "canceled")

    def test_cancel_request_wins_over_pause_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="cancel_requested")
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

            engine._finalize_paused_run("run-1", "orders")

            run = store.get_run("run-1")
            table = store.get_run_table("run-1", "orders")
            self.assertEqual(run["status"], "canceled")
            self.assertEqual(table["status"], "canceled")

    def test_aggregate_shards_keeps_table_canceled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store = make_engine(tmp)
            create_run(store, status="cancel_requested")
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
                    "status": "canceled",
                }
            )

            table = store.aggregate_shards("run-1", "orders")

            self.assertEqual(table["status"], "canceled")
            self.assertEqual(table["processed_rows"], 7)

    def test_cancel_request_turns_active_database_error_into_canceled_run(self):
        class CancelAfterDatabaseErrorEngine(SyncEngine):
            def _execute_offset_table(self, run, table_state, *, resume=False):
                self.store.update_run(run["id"], fetch=False, status="cancel_requested")
                raise RuntimeError("(2013, 'Lost connection to MySQL server during query (timed out)')")

        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            engine = CancelAfterDatabaseErrorEngine(engine.config, store)
            create_run(store, status="queued")
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

            engine.execute_run("run-1")

            run = store.get_run("run-1")
            table = store.get_run_table("run-1", "orders")
            self.assertEqual(run["status"], "canceled")
            self.assertEqual(table["status"], "canceled")
            self.assertIsNone(run["error"])

    def test_cancel_request_turns_shard_database_error_into_canceled_shard(self):
        class DummyConnection:
            def rollback(self):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="running")
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
            shard = store.create_run_shard(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "shard_index": 0,
                    "processed_rows": 0,
                    "status": "running",
                }
            )
            run = {**store.get_run("run-1"), "where_clause": "", "batch_size": 100, "mode": "replace"}

            def fail_after_cancel(*args, **kwargs):
                store.update_run("run-1", fetch=False, status="cancel_requested")
                raise RuntimeError("(2013, 'Lost connection to MySQL server during query (timed out)')")

            with patch("sync_tool.sync.mysql.connect", return_value=DummyConnection()):
                with patch("sync_tool.sync.mysql.fetch_keyset_batch", side_effect=fail_after_cancel):
                    with self.assertRaises(SyncCancelled):
                        engine._execute_cursor_shard(run, "orders", shard, ["id"], ["id"], ["id"])

            shard = store.get_run_shards("run-1", "orders")[0]
            self.assertEqual(shard["status"], "canceled")
            self.assertIsNone(shard["error"])

    def test_keyset_shard_stops_without_extra_empty_fetch_after_short_batch(self):
        class DummyConnection:
            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="running")
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
            shard = store.create_run_shard(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "shard_index": 0,
                    "processed_rows": 0,
                    "status": "running",
                }
            )
            run = {**store.get_run("run-1"), "where_clause": "", "batch_size": 2, "mode": "replace"}

            with patch("sync_tool.sync.mysql.connect", return_value=DummyConnection()):
                with patch("sync_tool.sync.mysql.fetch_keyset_batch", return_value=[{"id": 10}]) as fetch:
                    with patch("sync_tool.sync.mysql.insert_rows"):
                        engine._execute_cursor_shard(run, "orders", shard, ["id"], ["id"], ["id"])

            shard = store.get_run_shards("run-1", "orders")[0]
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(shard["status"], "success")
            self.assertEqual(shard["processed_rows"], 1)
            self.assertEqual(shard["last_pk"], "10")

    def test_offset_table_stops_without_extra_empty_fetch_after_short_batch(self):
        class DummyConnection:
            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            create_run(store, status="running")
            store.create_run_table(
                {
                    "run_id": "run-1",
                    "table_name": "orders",
                    "total_rows": 10,
                    "processed_rows": 0,
                    "offset_value": 0,
                    "status": "running",
                }
            )
            run = {**store.get_run("run-1"), "where_clause": "", "batch_size": 2, "mode": "replace", "skip_exact_count": True}
            table_state = store.get_run_table("run-1", "orders")
            column_plan = {
                "fetch_columns": ["id"],
                "write_columns": ["id"],
                "source_primary_keys": ["id"],
                "target_primary_keys": ["id"],
                "write_primary_keys": ["id"],
            }

            with patch("sync_tool.sync.mysql.connect", return_value=DummyConnection()):
                with patch.object(engine, "_ensure_destination_table", return_value=True):
                    with patch.object(engine, "_resolve_table_columns", return_value=column_plan):
                        with patch("sync_tool.sync.mysql.fetch_batch", return_value=[{"id": 10}]) as fetch:
                            with patch("sync_tool.sync.mysql.insert_rows"):
                                engine._execute_offset_table(run, table_state)

            table = store.get_run_table("run-1", "orders")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(table["status"], "success")
            self.assertEqual(table["processed_rows"], 1)
            self.assertEqual(table["offset_value"], 1)

    def test_manager_queues_same_table_runs_until_lock_is_released(self):
        class QueueEngine(SyncEngine):
            def __init__(self, config, store):
                super().__init__(config, store)
                self.created = 0
                self.started = []
                self.first_started = Event()
                self.first_release = Event()
                self.second_started = Event()
                self.second_finished = Event()

            def prepare_run(self, payload, *, job_id=None, name=None):
                self.created += 1
                run_id = f"run-{self.created}"
                tables = payload["tables"]
                run = self.store.create_run(
                    {
                        "id": run_id,
                        "job_id": job_id,
                        "name": name or f"run {self.created}",
                        "tables": tables,
                        "mode": payload.get("mode", "replace"),
                        "where_clause": payload.get("where_clause", ""),
                        "batch_size": payload.get("batch_size", 1000),
                        "create_missing_tables": False,
                        "sync_strategy": "offset",
                        "dry_run": False,
                        "status": "queued",
                        "total_rows": 1,
                    }
                )
                for table in tables:
                    self.store.create_run_table(
                        {
                            "run_id": run_id,
                            "table_name": table,
                            "total_rows": 1,
                            "processed_rows": 0,
                            "status": "pending",
                        }
                    )
                self.log(run_id, "info", "Plan ready.")
                return {**run, "tables_state": self.store.get_run_tables(run_id)}

            def execute_run(self, run_id, *, resume=False):
                self.started.append(run_id)
                self.store.update_run(run_id, fetch=False, status="running", started_at=utc_now(), error=None)
                self.log(run_id, "info", "Fake sync started.")
                if run_id == "run-1":
                    self.first_started.set()
                    self.first_release.wait(2)
                if run_id == "run-2":
                    self.second_started.set()
                self.store.update_run(
                    run_id,
                    fetch=False,
                    status="success",
                    processed_rows=1,
                    finished_at=utc_now(),
                    error=None,
                )
                self.log(run_id, "info", "Fake sync completed.")
                if run_id == "run-2":
                    self.second_finished.set()

        with tempfile.TemporaryDirectory() as tmp:
            engine, store = make_engine(tmp)
            engine = QueueEngine(engine.config, store)
            manager = SyncManager(engine, max_workers=2)
            try:
                first = manager.start({"tables": ["orders"], "mode": "replace", "batch_size": 1000}, name="first")
                self.assertIn(first["status"], {"queued", "running"})
                self.assertTrue(engine.first_started.wait(1))

                second = manager.start({"tables": ["orders"], "mode": "replace", "batch_size": 1000}, name="second")
                self.assertEqual(second["status"], "queued")
                self.assertEqual(second["queue_state"], "waiting_table")
                self.assertEqual(engine.started, ["run-1"])

                engine.first_release.set()
                self.assertTrue(engine.second_started.wait(2))
                self.assertTrue(engine.second_finished.wait(2))
                self.assertEqual(engine.started, ["run-1", "run-2"])
                self.assertEqual(manager.get_run("run-2")["status"], "success")
            finally:
                engine.first_release.set()
                manager.shutdown(wait=True)

    def test_manager_default_allows_two_run_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = make_engine(tmp)
            manager = SyncManager(engine)
            try:
                self.assertEqual(manager.executor._max_workers, DEFAULT_RUN_WORKERS)
                self.assertGreaterEqual(DEFAULT_RUN_WORKERS, 2)
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
