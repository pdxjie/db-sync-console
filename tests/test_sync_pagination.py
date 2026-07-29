import unittest
from pathlib import Path

from sync_tool.config import AppConfig, Config, MySQLConfig, SafetyConfig
from sync_tool.sync import SyncEngine, SyncPlanError


def make_engine(max_rows_without_where=1_000_000):
    config = Config(
        prod=MySQLConfig(host="prod", user="reader", database="prod_db"),
        test=MySQLConfig(host="test", user="writer", database="test_db"),
        app=AppConfig(page_size=5000),
        safety=SafetyConfig(max_rows_without_where=max_rows_without_where),
        path=Path("config.json"),
        exists=False,
    )
    return SyncEngine(config, store=None)


class SyncPaginationStrategyTests(unittest.TestCase):
    def test_auto_uses_primary_key_cursor(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="orders",
            requested_strategy="auto",
            cursor_field="",
            prod_column_names=["id", "name"],
            nullable_source_columns=set(),
            primary_keys=["id"],
            indexes=[{"name": "PRIMARY", "unique": True, "primary": True, "columns": ["id"]}],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "cursor")
        self.assertEqual(plan["cursor_fields"], ["id"])
        self.assertTrue(plan["cursor_unique"])

    def test_auto_uses_non_null_unique_index_without_primary_key(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="events",
            requested_strategy="auto",
            cursor_field="",
            prod_column_names=["updated_at", "event_id", "payload"],
            nullable_source_columns=set(),
            primary_keys=[],
            indexes=[
                {"name": "uniq_event", "unique": True, "primary": False, "columns": ["updated_at", "event_id"]},
            ],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "cursor")
        self.assertEqual(plan["cursor_fields"], ["updated_at", "event_id"])
        self.assertEqual(plan["cursor_index"], "uniq_event")

    def test_manual_non_unique_cursor_appends_primary_key(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="events",
            requested_strategy="auto",
            cursor_field="updated_at",
            prod_column_names=["id", "updated_at", "payload"],
            nullable_source_columns=set(),
            primary_keys=["id"],
            indexes=[
                {"name": "PRIMARY", "unique": True, "primary": True, "columns": ["id"]},
                {"name": "idx_updated_at", "unique": False, "primary": False, "columns": ["updated_at"]},
            ],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "cursor")
        self.assertEqual(plan["cursor_fields"], ["updated_at", "id"])
        self.assertTrue(plan["cursor_unique"])
        self.assertTrue(any("primary key appended" in warning for warning in plan["warnings"]))

    def test_manual_cursor_prefix_expands_to_unique_index(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="events",
            requested_strategy="auto",
            cursor_field="updated_at",
            prod_column_names=["updated_at", "event_id", "payload"],
            nullable_source_columns=set(),
            primary_keys=[],
            indexes=[
                {"name": "uniq_event", "unique": True, "primary": False, "columns": ["updated_at", "event_id"]},
            ],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "cursor")
        self.assertEqual(plan["cursor_fields"], ["updated_at", "event_id"])
        self.assertEqual(plan["cursor_index"], "uniq_event")
        self.assertTrue(plan["cursor_unique"])

    def test_auto_falls_back_to_offset_without_stable_cursor(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="logs",
            requested_strategy="auto",
            cursor_field="",
            prod_column_names=["message"],
            nullable_source_columns=set(),
            primary_keys=[],
            indexes=[],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "offset")
        self.assertFalse(plan["cursor_fields"])
        self.assertTrue(any("falling back to offset" in warning for warning in plan["warnings"]))

    def test_auto_skips_nullable_unique_index(self):
        engine = make_engine()

        plan = engine._choose_pagination(
            table="users",
            requested_strategy="auto",
            cursor_field="",
            prod_column_names=["email", "name"],
            nullable_source_columns={"email"},
            primary_keys=[],
            indexes=[{"name": "uniq_email", "unique": True, "primary": False, "columns": ["email"]}],
            row_count=100,
        )

        self.assertEqual(plan["effective_strategy"], "offset")
        self.assertTrue(any("nullable unique" in warning for warning in plan["warnings"]))

    def test_forced_cursor_requires_stable_cursor(self):
        engine = make_engine()

        with self.assertRaises(SyncPlanError):
            engine._choose_pagination(
                table="logs",
                requested_strategy="cursor",
                cursor_field="",
                prod_column_names=["message"],
                nullable_source_columns=set(),
                primary_keys=[],
                indexes=[],
                row_count=100,
            )


if __name__ == "__main__":
    unittest.main()
