import unittest

from sync_tool.mysql import (
    SQLValidationError,
    build_insert_sql,
    build_upsert_sql,
    normalize_where_clause,
    quote_identifier,
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


if __name__ == "__main__":
    unittest.main()
