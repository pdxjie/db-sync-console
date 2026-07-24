import unittest

from sync_tool.mysql import (
    SQLValidationError,
    build_insert_sql,
    build_upsert_sql,
    normalize_where_clause,
    quote_identifier,
    sync_column_plan,
)


class MySQLHelperTests(unittest.TestCase):
    def test_quote_identifier_rejects_unsafe_names(self):
        with self.assertRaises(SQLValidationError):
            quote_identifier("users; drop table users")

    def test_normalize_where_clause_strips_where_keyword(self):
        self.assertEqual(normalize_where_clause("WHERE id > 10"), "id > 10")

    def test_normalize_where_clause_rejects_stacked_sql(self):
        with self.assertRaises(SQLValidationError):
            normalize_where_clause("id = 1; drop table users")

    def test_build_insert_sql_quotes_columns(self):
        self.assertEqual(
            build_insert_sql("users", ["id", "name"]),
            "INSERT INTO `users` (`id`, `name`) VALUES (%s, %s)",
        )

    def test_build_upsert_sql_uses_non_primary_columns(self):
        sql = build_upsert_sql("users", ["id", "name"], ["id"])
        self.assertEqual(
            sql,
            "INSERT INTO `users` (`id`, `name`) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)",
        )

    def test_sync_column_plan_uses_target_common_columns(self):
        prod_columns = [
            {"name": "id", "column_type": "bigint", "nullable": "NO", "column_default": None, "extra": ""},
            {"name": "name", "column_type": "varchar(64)", "nullable": "YES", "column_default": None, "extra": ""},
            {"name": "prod_only", "column_type": "int", "nullable": "YES", "column_default": None, "extra": ""},
        ]
        test_columns = [
            {"name": "id", "column_type": "bigint", "nullable": "NO", "column_default": None, "extra": ""},
            {"name": "test_only", "column_type": "varchar(16)", "nullable": "YES", "column_default": None, "extra": ""},
            {"name": "name", "column_type": "varchar(128)", "nullable": "YES", "column_default": None, "extra": ""},
        ]

        plan = sync_column_plan(prod_columns, test_columns)

        self.assertEqual(plan["write_columns"], ["id", "name"])
        self.assertEqual(plan["source_only_columns"], ["prod_only"])
        self.assertEqual(plan["target_only_columns"], ["test_only"])
        self.assertEqual(plan["type_mismatches"][0]["name"], "name")

    def test_sync_column_plan_warns_required_target_only_columns(self):
        plan = sync_column_plan(
            [{"name": "id", "column_type": "bigint", "nullable": "NO", "column_default": None, "extra": ""}],
            [
                {"name": "id", "column_type": "bigint", "nullable": "NO", "column_default": None, "extra": ""},
                {
                    "name": "tenant_id",
                    "column_type": "bigint",
                    "nullable": "NO",
                    "column_default": None,
                    "extra": "",
                },
                {
                    "name": "created_at",
                    "column_type": "datetime",
                    "nullable": "NO",
                    "column_default": "CURRENT_TIMESTAMP",
                    "extra": "",
                },
            ],
        )

        self.assertEqual(plan["required_target_only_columns"], ["tenant_id"])


if __name__ == "__main__":
    unittest.main()
