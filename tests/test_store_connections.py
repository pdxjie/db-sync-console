import tempfile
import unittest
from pathlib import Path

from sync_tool.store import SyncStore


class StoreConnectionTests(unittest.TestCase):
    def test_save_connection_redacts_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.init()
            saved = store.save_connection(
                "prod",
                {
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "reader",
                    "password": "secret",
                    "database": "prod_db",
                    "charset": "utf8mb4",
                },
            )
            self.assertTrue(saved["password_set"])
            self.assertNotIn("password", saved)

    def test_blank_password_keeps_existing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.init()
            store.save_connection(
                "test",
                {
                    "host": "127.0.0.1",
                    "user": "writer",
                    "password": "secret",
                    "database": "test_db",
                },
            )
            store.save_connection(
                "test",
                {
                    "host": "127.0.0.1",
                    "user": "writer2",
                    "password": "",
                    "database": "test_db",
                },
            )
            raw = store.get_connection("test", reveal_password=True)
            self.assertEqual(raw["password"], "secret")
            self.assertEqual(raw["user"], "writer2")

    def test_job_and_run_store_create_missing_tables_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "sync.db")
            store.init()
            job = store.create_job(
                {
                    "name": "stock",
                    "tables": ["mc_stock_batch"],
                    "mode": "replace",
                    "where_clause": "",
                    "batch_size": 1000,
                    "create_missing_tables": True,
                }
            )
            self.assertTrue(job["create_missing_tables"])

            run = store.create_run(
                {
                    "id": "run-1",
                    "job_id": job["id"],
                    "name": "stock",
                    "tables": ["mc_stock_batch"],
                    "mode": "replace",
                    "where_clause": "",
                    "batch_size": 1000,
                    "create_missing_tables": True,
                }
            )
            self.assertTrue(run["create_missing_tables"])


if __name__ == "__main__":
    unittest.main()
