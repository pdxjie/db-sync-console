from __future__ import annotations

import re
from decimal import Decimal
from numbers import Number
from typing import Any, Iterable

from .config import MySQLConfig


VALID_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")
UNSAFE_WHERE_PATTERNS = (";", "--", "/*", "*/", "\x00")


class SQLValidationError(ValueError):
    """Raised when a table, column, or where clause is unsafe for this tool."""


class TableNotFoundError(SQLValidationError):
    def __init__(self, table: str):
        super().__init__(f"Table does not exist or has no visible columns: {table}")
        self.table = table


def validate_identifier(name: str) -> str:
    if not name or not VALID_IDENTIFIER.match(name):
        raise SQLValidationError(f"Unsafe MySQL identifier: {name!r}")
    return name


def quote_identifier(name: str) -> str:
    validate_identifier(name)
    return f"`{name}`"


def quote_identifiers(names: Iterable[str]) -> str:
    return ", ".join(quote_identifier(name) for name in names)


def normalize_where_clause(where_clause: str | None) -> str:
    clause = (where_clause or "").strip()
    if clause.lower().startswith("where "):
        clause = clause[6:].strip()
    lower = clause.lower()
    if any(pattern in lower for pattern in UNSAFE_WHERE_PATTERNS):
        raise SQLValidationError("WHERE clause contains unsupported SQL control characters or comments.")
    return clause


def build_where(
    where_clause: str | None = None,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    clause = normalize_where_clause(where_clause)
    if clause:
        clauses.append(f"({clause})")
    for condition, condition_params in extra_conditions or []:
        if condition:
            clauses.append(f"({condition})")
            params.extend(condition_params)
    if not clauses:
        return "", tuple(params)
    return " WHERE " + " AND ".join(clauses), tuple(params)


def incremental_condition(column: str, since_value: str | None) -> tuple[str, tuple[Any, ...]] | None:
    if not since_value:
        return None
    return f"{quote_identifier(column)} >= %s", (since_value,)


def is_number(value: Any) -> bool:
    return isinstance(value, (Number, Decimal)) and not isinstance(value, bool)


def connect(config: MySQLConfig):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError("PyMySQL is required. Install dependencies with: pip install -r requirements.txt") from exc

    return pymysql.connect(**config.to_pymysql_args(), cursorclass=DictCursor, autocommit=False)


def test_connection(config: MySQLConfig) -> dict[str, Any]:
    conn = connect(config)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS version")
            row = cursor.fetchone()
        return {
            "database": row["database_name"],
            "version": row["version"],
        }
    finally:
        conn.close()


def ensure_connections_are_distinct(prod: MySQLConfig, test: MySQLConfig) -> None:
    if prod.fingerprint() == test.fingerprint():
        raise SQLValidationError("Product and test database targets resolve to the same host, port, and database.")


def list_tables(conn) -> list[dict[str, Any]]:
    sql = """
        SELECT TABLE_NAME AS name,
               TABLE_ROWS AS estimated_rows,
               DATA_LENGTH AS data_length,
               UPDATE_TIME AS update_time
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def table_stats(conn, table: str) -> dict[str, Any]:
    validate_identifier(table)
    sql = """
        SELECT TABLE_ROWS AS estimated_rows,
               DATA_LENGTH AS data_length,
               INDEX_LENGTH AS index_length,
               AVG_ROW_LENGTH AS avg_row_length
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND TABLE_TYPE = 'BASE TABLE'
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table,))
        row = cursor.fetchone()
    if not row:
        raise TableNotFoundError(table)
    estimated_rows = int(row.get("estimated_rows") or 0)
    data_length = int(row.get("data_length") or 0)
    avg_row_length = int(row.get("avg_row_length") or 0)
    if avg_row_length <= 0 and estimated_rows > 0:
        avg_row_length = max(1, data_length // estimated_rows)
    return {
        "estimated_rows": estimated_rows,
        "data_length": data_length,
        "index_length": int(row.get("index_length") or 0),
        "avg_row_length": avg_row_length,
    }


def describe_columns(conn, table: str) -> list[dict[str, Any]]:
    validate_identifier(table)
    sql = """
        SELECT COLUMN_NAME AS name,
               COLUMN_TYPE AS column_type,
               IS_NULLABLE AS nullable,
               COLUMN_DEFAULT AS column_default,
               COLUMN_KEY AS column_key,
               EXTRA AS extra,
               ORDINAL_POSITION AS ordinal_position
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table,))
        rows = list(cursor.fetchall())
    if not rows:
        raise TableNotFoundError(table)
    return rows


def table_exists(conn, table: str) -> bool:
    validate_identifier(table)
    sql = """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND TABLE_TYPE = 'BASE TABLE'
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table,))
        row = cursor.fetchone()
    return int(row["table_count"]) > 0


def show_create_table(conn, table: str) -> str:
    validate_identifier(table)
    with conn.cursor() as cursor:
        cursor.execute(f"SHOW CREATE TABLE {quote_identifier(table)}")
        row = cursor.fetchone()
    ddl = row.get("Create Table") if isinstance(row, dict) else None
    if not ddl:
        raise TableNotFoundError(table)
    return str(ddl)


def create_table_from_ddl(conn, ddl: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(ddl)


def column_names(columns: list[dict[str, Any]]) -> list[str]:
    return [str(item["name"]) for item in columns]


def primary_key_columns(conn, table: str) -> list[str]:
    validate_identifier(table)
    sql = """
        SELECT COLUMN_NAME AS name
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = 'PRIMARY'
        ORDER BY SEQ_IN_INDEX
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table,))
        return [str(row["name"]) for row in cursor.fetchall()]


def indexed_columns(conn, table: str) -> set[str]:
    validate_identifier(table)
    sql = """
        SELECT DISTINCT COLUMN_NAME AS name
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table,))
        return {str(row["name"]) for row in cursor.fetchall()}


def compare_column_shapes(prod_columns: list[dict[str, Any]], test_columns: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    prod_by_name = {str(item["name"]): item for item in prod_columns}
    test_by_name = {str(item["name"]): item for item in test_columns}
    prod_names = list(prod_by_name)
    test_names = list(test_by_name)

    missing = [name for name in prod_names if name not in test_by_name]
    extra = [name for name in test_names if name not in prod_by_name]
    if missing:
        errors.append(f"test table is missing columns: {', '.join(missing)}")
    if extra:
        errors.append(f"test table has extra columns: {', '.join(extra)}")

    for name in prod_names:
        if name not in test_by_name:
            continue
        prod_type = str(prod_by_name[name]["column_type"]).lower()
        test_type = str(test_by_name[name]["column_type"]).lower()
        if prod_type != test_type:
            errors.append(f"column {name} type mismatch: prod={prod_type}, test={test_type}")
    return errors


def sync_column_plan(prod_columns: list[dict[str, Any]], test_columns: list[dict[str, Any]]) -> dict[str, Any]:
    prod_by_name = {str(item["name"]): item for item in prod_columns}
    test_by_name = {str(item["name"]): item for item in test_columns}
    prod_names = list(prod_by_name)
    test_names = list(test_by_name)
    common_columns = [name for name in test_names if name in prod_by_name]
    source_only_columns = [name for name in prod_names if name not in test_by_name]
    target_only_columns = [name for name in test_names if name not in prod_by_name]
    type_mismatches = []
    for name in common_columns:
        prod_type = str(prod_by_name[name]["column_type"]).lower()
        test_type = str(test_by_name[name]["column_type"]).lower()
        if prod_type != test_type:
            type_mismatches.append({"name": name, "prod_type": prod_type, "test_type": test_type})
    return {
        "write_columns": common_columns,
        "source_only_columns": source_only_columns,
        "target_only_columns": target_only_columns,
        "type_mismatches": type_mismatches,
        "required_target_only_columns": required_columns_without_defaults(
            [test_by_name[name] for name in target_only_columns]
        ),
    }


def required_columns_without_defaults(columns: list[dict[str, Any]]) -> list[str]:
    required = []
    for column in columns:
        nullable = str(column.get("nullable", "")).upper()
        extra = str(column.get("extra", "")).lower()
        if nullable == "NO" and column.get("column_default") is None and "auto_increment" not in extra:
            required.append(str(column["name"]))
    return required


def count_rows(
    conn,
    table: str,
    where_clause: str | None = None,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> int:
    table_sql = quote_identifier(table)
    sql = f"SELECT COUNT(*) AS row_count FROM {table_sql}"
    where_sql, params = build_where(where_clause, extra_conditions)
    sql += where_sql
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return int(row["row_count"])


def min_max_cursor(
    conn,
    table: str,
    cursor_field: str,
    where_clause: str | None = None,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> dict[str, Any]:
    table_sql = quote_identifier(table)
    cursor_sql = quote_identifier(cursor_field)
    where_sql, params = build_where(where_clause, extra_conditions)
    sql = f"SELECT MIN({cursor_sql}) AS min_value, MAX({cursor_sql}) AS max_value FROM {table_sql}{where_sql}"
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return {"min": row["min_value"], "max": row["max_value"]}


def fetch_batch(
    conn,
    table: str,
    columns: list[str],
    where_clause: str | None,
    order_columns: list[str],
    limit: int,
    offset: int,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> list[dict[str, Any]]:
    table_sql = quote_identifier(table)
    column_sql = quote_identifiers(columns)
    sql = f"SELECT {column_sql} FROM {table_sql}"
    where_sql, params = build_where(where_clause, extra_conditions)
    sql += where_sql
    if order_columns:
        sql += " ORDER BY " + quote_identifiers(order_columns)
    sql += " LIMIT %s OFFSET %s"
    with conn.cursor() as cursor:
        cursor.execute(sql, (*params, limit, offset))
        return list(cursor.fetchall())


def fetch_cursor_batch(
    conn,
    table: str,
    columns: list[str],
    where_clause: str | None,
    cursor_field: str,
    limit: int,
    *,
    last_pk: Any = None,
    shard_start: Any = None,
    shard_end: Any = None,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> list[dict[str, Any]]:
    table_sql = quote_identifier(table)
    cursor_sql = quote_identifier(cursor_field)
    column_sql = quote_identifiers(columns)
    conditions = list(extra_conditions or [])
    if last_pk is not None:
        conditions.append((f"{cursor_sql} > %s", (last_pk,)))
    elif shard_start is not None:
        conditions.append((f"{cursor_sql} >= %s", (shard_start,)))
    if shard_end is not None:
        conditions.append((f"{cursor_sql} <= %s", (shard_end,)))
    where_sql, params = build_where(where_clause, conditions)
    sql = f"SELECT {column_sql} FROM {table_sql}{where_sql} ORDER BY {cursor_sql} LIMIT %s"
    with conn.cursor() as cursor:
        cursor.execute(sql, (*params, limit))
        return list(cursor.fetchall())


def truncate_table(conn, table: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(f"TRUNCATE TABLE {quote_identifier(table)}")


def delete_where(
    conn,
    table: str,
    where_clause: str | None,
    extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
) -> int:
    where_sql, params = build_where(where_clause, extra_conditions)
    if not where_sql:
        raise SQLValidationError("Refusing DELETE without WHERE. Use TRUNCATE for full replace.")
    with conn.cursor() as cursor:
        cursor.execute(f"DELETE FROM {quote_identifier(table)}{where_sql}", params)
        return int(cursor.rowcount)


def build_insert_sql(table: str, columns: list[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO {quote_identifier(table)} ({quote_identifiers(columns)}) VALUES ({placeholders})"


def build_upsert_sql(table: str, columns: list[str], primary_keys: list[str]) -> str:
    insert_sql = build_insert_sql(table, columns)
    update_columns = [name for name in columns if name not in set(primary_keys)]
    if not update_columns:
        update_columns = columns[:1]
    assignments = ", ".join(f"{quote_identifier(name)} = VALUES({quote_identifier(name)})" for name in update_columns)
    return f"{insert_sql} ON DUPLICATE KEY UPDATE {assignments}"


def row_values(rows: list[dict[str, Any]], columns: list[str]) -> list[tuple[Any, ...]]:
    return [tuple(row.get(column) for column in columns) for row in rows]


def insert_rows(conn, table: str, columns: list[str], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cursor:
        cursor.executemany(build_insert_sql(table, columns), row_values(rows, columns))
        return int(cursor.rowcount)


def upsert_rows(conn, table: str, columns: list[str], primary_keys: list[str], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    if not primary_keys:
        raise SQLValidationError(f"Table {table} has no primary key; upsert is not available.")
    with conn.cursor() as cursor:
        cursor.executemany(build_upsert_sql(table, columns, primary_keys), row_values(rows, columns))
        return int(cursor.rowcount)
