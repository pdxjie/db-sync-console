from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import MySQLConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    tables_json TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    where_clause TEXT NOT NULL DEFAULT '',
                    batch_size INTEGER NOT NULL,
                    create_missing_tables INTEGER NOT NULL DEFAULT 0,
                    sync_strategy TEXT NOT NULL DEFAULT 'auto',
                    cursor_field TEXT NOT NULL DEFAULT '',
                    incremental_field TEXT NOT NULL DEFAULT '',
                    incremental_since TEXT NOT NULL DEFAULT '',
                    skip_exact_count INTEGER NOT NULL DEFAULT 0,
                    shard_count INTEGER NOT NULL DEFAULT 1,
                    worker_count INTEGER NOT NULL DEFAULT 1,
                    schedule_enabled INTEGER NOT NULL DEFAULT 0,
                    cron_expr TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    job_id INTEGER,
                    name TEXT NOT NULL,
                    tables_json TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    where_clause TEXT NOT NULL DEFAULT '',
                    batch_size INTEGER NOT NULL,
                    create_missing_tables INTEGER NOT NULL DEFAULT 0,
                    sync_strategy TEXT NOT NULL DEFAULT 'auto',
                    cursor_field TEXT NOT NULL DEFAULT '',
                    incremental_field TEXT NOT NULL DEFAULT '',
                    incremental_since TEXT NOT NULL DEFAULT '',
                    skip_exact_count INTEGER NOT NULL DEFAULT 0,
                    shard_count INTEGER NOT NULL DEFAULT 1,
                    worker_count INTEGER NOT NULL DEFAULT 1,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    current_table TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS run_tables (
                    run_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    offset_value INTEGER NOT NULL DEFAULT 0,
                    last_pk TEXT,
                    shard_count INTEGER NOT NULL DEFAULT 1,
                    cursor_field TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(run_id, table_name),
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_shards (
                    run_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    shard_index INTEGER NOT NULL,
                    start_pk TEXT,
                    end_pk TEXT,
                    last_pk TEXT,
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(run_id, table_name, shard_index),
                    FOREIGN KEY(run_id, table_name) REFERENCES run_tables(run_id, table_name) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS db_connections (
                    env TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            for table in ("jobs", "runs"):
                self._ensure_column(conn, table, "create_missing_tables", "INTEGER NOT NULL DEFAULT 0")
                self._ensure_column(conn, table, "sync_strategy", "TEXT NOT NULL DEFAULT 'auto'")
                self._ensure_column(conn, table, "cursor_field", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, table, "incremental_field", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, table, "incremental_since", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, table, "skip_exact_count", "INTEGER NOT NULL DEFAULT 0")
                self._ensure_column(conn, table, "shard_count", "INTEGER NOT NULL DEFAULT 1")
                self._ensure_column(conn, table, "worker_count", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "runs", "total_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "processed_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "run_tables", "total_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "run_tables", "processed_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "run_tables", "last_pk", "TEXT")
            self._ensure_column(conn, "run_tables", "shard_count", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "run_tables", "cursor_field", "TEXT NOT NULL DEFAULT ''")

    def save_connection(self, env: str, payload: dict[str, Any]) -> dict[str, Any]:
        env = self._validate_env(env)
        raw = self.build_connection_payload(env, payload)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO db_connections (env, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(env) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (env, json.dumps(raw, ensure_ascii=False), utc_now()),
            )
        return self.get_connection(env)

    def build_connection_payload(self, env: str, payload: dict[str, Any]) -> dict[str, Any]:
        env = self._validate_env(env)
        existing = self.get_connection(env, reveal_password=True)
        return self._connection_payload(payload, existing)

    def get_connection(self, env: str, *, reveal_password: bool = False) -> dict[str, Any] | None:
        env = self._validate_env(env)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM db_connections WHERE env = ?", (env,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        item = self._redact_connection(payload) if not reveal_password else payload
        item["env"] = env
        item["updated_at"] = row["updated_at"]
        return item

    def list_connections(self) -> dict[str, Any]:
        return {
            "prod": self.get_connection("prod"),
            "test": self.get_connection("test"),
        }

    def mysql_config(self, env: str) -> MySQLConfig | None:
        payload = self.get_connection(env, reveal_password=True)
        if payload is None:
            return None
        return MySQLConfig.from_dict(payload)

    def delete_connection(self, env: str) -> None:
        env = self._validate_env(env)
        with self.connect() as conn:
            conn.execute("DELETE FROM db_connections WHERE env = ?", (env,))

    def mark_interrupted_runs(self) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE run_tables
                SET status = 'paused',
                    error = NULL
                WHERE run_id IN (SELECT id FROM runs WHERE status = 'pause_requested')
                  AND status IN ('running', 'pending', 'pause_requested')
                """
            )
            conn.execute(
                """
                UPDATE run_shards
                SET status = 'paused',
                    error = NULL
                WHERE run_id IN (SELECT id FROM runs WHERE status = 'pause_requested')
                  AND status IN ('running', 'pending', 'pause_requested')
                """
            )
            conn.execute(
                """
                UPDATE runs
                SET status = 'paused',
                    error = NULL,
                    finished_at = COALESCE(finished_at, ?)
                WHERE status = 'pause_requested'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    error = 'Process stopped while this run was active.',
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('running', 'queued')
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE run_tables
                SET status = 'failed',
                    error = COALESCE(error, 'Process stopped while this table was active.')
                WHERE run_id IN (SELECT id FROM runs WHERE error = 'Process stopped while this run was active.')
                  AND status IN ('running', 'pending')
                """
            )
            conn.execute(
                """
                UPDATE run_shards
                SET status = 'failed',
                    error = COALESCE(error, 'Process stopped while this shard was active.')
                WHERE run_id IN (SELECT id FROM runs WHERE error = 'Process stopped while this run was active.')
                  AND status IN ('running', 'pending')
                """
            )

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    name, tables_json, mode, where_clause, batch_size,
                    create_missing_tables, sync_strategy, cursor_field, incremental_field,
                    incremental_since, skip_exact_count, shard_count, worker_count,
                    schedule_enabled, cron_expr, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    json.dumps(payload["tables"], ensure_ascii=False),
                    payload["mode"],
                    payload.get("where_clause", ""),
                    int(payload["batch_size"]),
                    1 if payload.get("create_missing_tables") else 0,
                    payload.get("sync_strategy", "auto"),
                    payload.get("cursor_field", ""),
                    payload.get("incremental_field", ""),
                    payload.get("incremental_since", ""),
                    1 if payload.get("skip_exact_count") else 0,
                    int(payload.get("shard_count", 1)),
                    int(payload.get("worker_count", 1)),
                    1 if payload.get("schedule_enabled") else 0,
                    payload.get("cron_expr", ""),
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
        return self.get_job(job_id)

    def update_job(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_job(job_id)
        merged = {**existing, **payload}
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET name = ?,
                    tables_json = ?,
                    mode = ?,
                    where_clause = ?,
                    batch_size = ?,
                    create_missing_tables = ?,
                    sync_strategy = ?,
                    cursor_field = ?,
                    incremental_field = ?,
                    incremental_since = ?,
                    skip_exact_count = ?,
                    shard_count = ?,
                    worker_count = ?,
                    schedule_enabled = ?,
                    cron_expr = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    merged["name"],
                    json.dumps(merged["tables"], ensure_ascii=False),
                    merged["mode"],
                    merged.get("where_clause", ""),
                    int(merged["batch_size"]),
                    1 if merged.get("create_missing_tables") else 0,
                    merged.get("sync_strategy", "auto"),
                    merged.get("cursor_field", ""),
                    merged.get("incremental_field", ""),
                    merged.get("incremental_since", ""),
                    1 if merged.get("skip_exact_count") else 0,
                    int(merged.get("shard_count", 1)),
                    int(merged.get("worker_count", 1)),
                    1 if merged.get("schedule_enabled") else 0,
                    merged.get("cron_expr", ""),
                    now,
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def delete_job(self, job_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return self._decode_job(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC, id DESC").fetchall()
        return [self._decode_job(row) for row in rows]

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, job_id, name, tables_json, mode, where_clause, batch_size,
                    create_missing_tables, sync_strategy, cursor_field, incremental_field,
                    incremental_since, skip_exact_count, shard_count, worker_count,
                    dry_run, status, total_rows, processed_rows, total_bytes, processed_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload.get("job_id"),
                    payload["name"],
                    json.dumps(payload["tables"], ensure_ascii=False),
                    payload["mode"],
                    payload.get("where_clause", ""),
                    int(payload["batch_size"]),
                    1 if payload.get("create_missing_tables") else 0,
                    payload.get("sync_strategy", "auto"),
                    payload.get("cursor_field", ""),
                    payload.get("incremental_field", ""),
                    payload.get("incremental_since", ""),
                    1 if payload.get("skip_exact_count") else 0,
                    int(payload.get("shard_count", 1)),
                    int(payload.get("worker_count", 1)),
                    1 if payload.get("dry_run") else 0,
                    payload.get("status", "queued"),
                    int(payload.get("total_rows", 0)),
                    int(payload.get("processed_rows", 0)),
                    int(payload.get("total_bytes", 0)),
                    int(payload.get("processed_bytes", 0)),
                    now,
                ),
            )
        return self.get_run(payload["id"])

    def update_run(self, run_id: str, *, fetch: bool = True, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_run(run_id) if fetch else {}
        allowed = {
            "status",
            "total_rows",
            "processed_rows",
            "total_bytes",
            "processed_bytes",
            "current_table",
            "error",
            "started_at",
            "finished_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown run fields: {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)
        return self.get_run(run_id) if fetch else {}

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return self._decode_run(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def create_run_table(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_tables (
                    run_id, table_name, total_rows, processed_rows, total_bytes, processed_bytes,
                    offset_value, last_pk, shard_count, cursor_field, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["table_name"],
                    int(payload.get("total_rows", 0)),
                    int(payload.get("processed_rows", 0)),
                    int(payload.get("total_bytes", 0)),
                    int(payload.get("processed_bytes", 0)),
                    int(payload.get("offset_value", 0)),
                    payload.get("last_pk"),
                    int(payload.get("shard_count", 1)),
                    payload.get("cursor_field", ""),
                    payload.get("status", "pending"),
                    payload.get("error"),
                ),
            )
        return self.get_run_table(payload["run_id"], payload["table_name"])

    def update_run_table(self, run_id: str, table_name: str, *, fetch: bool = True, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_run_table(run_id, table_name) if fetch else {}
        allowed = {
            "total_rows",
            "processed_rows",
            "total_bytes",
            "processed_bytes",
            "offset_value",
            "last_pk",
            "shard_count",
            "cursor_field",
            "status",
            "error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown run table fields: {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.extend([run_id, table_name])
        with self.connect() as conn:
            conn.execute(
                f"UPDATE run_tables SET {assignments} WHERE run_id = ? AND table_name = ?",
                values,
            )
        return self.get_run_table(run_id, table_name) if fetch else {}

    def get_run_table(self, run_id: str, table_name: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_tables WHERE run_id = ? AND table_name = ?",
                (run_id, table_name),
            ).fetchone()
        if row is None:
            raise KeyError(f"Run table not found: {run_id}/{table_name}")
        return dict(row)

    def get_run_tables(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_tables WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sum_processed_rows(self, run_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(processed_rows), 0) AS total FROM run_tables WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["total"])

    def sum_processed_bytes(self, run_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(processed_bytes), 0) AS total FROM run_tables WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["total"])

    def create_run_shard(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_shards (
                    run_id, table_name, shard_index, start_pk, end_pk, last_pk,
                    total_rows, processed_rows, total_bytes, processed_bytes, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["table_name"],
                    int(payload["shard_index"]),
                    self._string_or_none(payload.get("start_pk")),
                    self._string_or_none(payload.get("end_pk")),
                    self._string_or_none(payload.get("last_pk")),
                    int(payload.get("total_rows", 0)),
                    int(payload.get("processed_rows", 0)),
                    int(payload.get("total_bytes", 0)),
                    int(payload.get("processed_bytes", 0)),
                    payload.get("status", "pending"),
                    payload.get("error"),
                ),
            )
        return self.get_run_shard(payload["run_id"], payload["table_name"], int(payload["shard_index"]))

    def update_run_shard(
        self,
        run_id: str,
        table_name: str,
        shard_index: int,
        *,
        fetch: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        if not fields:
            return self.get_run_shard(run_id, table_name, shard_index) if fetch else {}
        allowed = {
            "start_pk",
            "end_pk",
            "last_pk",
            "total_rows",
            "processed_rows",
            "total_bytes",
            "processed_bytes",
            "status",
            "error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown run shard fields: {', '.join(sorted(unknown))}")
        normalized = {
            key: self._string_or_none(value) if key in {"start_pk", "end_pk", "last_pk"} else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = list(normalized.values())
        values.extend([run_id, table_name, int(shard_index)])
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE run_shards
                SET {assignments}
                WHERE run_id = ? AND table_name = ? AND shard_index = ?
                """,
                values,
            )
        return self.get_run_shard(run_id, table_name, shard_index) if fetch else {}

    def get_run_shard(self, run_id: str, table_name: str, shard_index: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM run_shards
                WHERE run_id = ? AND table_name = ? AND shard_index = ?
                """,
                (run_id, table_name, int(shard_index)),
            ).fetchone()
        if row is None:
            raise KeyError(f"Run shard not found: {run_id}/{table_name}/{shard_index}")
        return dict(row)

    def get_run_shards(self, run_id: str, table_name: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if table_name:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM run_shards
                    WHERE run_id = ? AND table_name = ?
                    ORDER BY table_name, shard_index
                    """,
                    (run_id, table_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM run_shards
                    WHERE run_id = ?
                    ORDER BY table_name, shard_index
                    """,
                    (run_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def aggregate_shards(self, run_id: str, table_name: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(processed_rows), 0) AS processed_rows,
                    COALESCE(SUM(processed_bytes), 0) AS processed_bytes,
                    MAX(last_pk) AS last_pk,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS paused_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    COUNT(*) AS shard_count
                FROM run_shards
                WHERE run_id = ? AND table_name = ?
                """,
                (run_id, table_name),
            ).fetchone()
        shard_count = int(row["shard_count"] or 0)
        success_count = int(row["success_count"] or 0)
        paused_count = int(row["paused_count"] or 0)
        failed_count = int(row["failed_count"] or 0)
        if shard_count and success_count == shard_count:
            status = "success"
        elif failed_count:
            status = "failed"
        elif paused_count:
            status = "paused"
        else:
            status = "running"
        return self.update_run_table(
            run_id,
            table_name,
            processed_rows=int(row["processed_rows"]),
            processed_bytes=int(row["processed_bytes"]),
            last_pk=row["last_pk"],
            status=status,
        )

    def append_log(self, run_id: str, level: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO logs (run_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (run_id, level, message, utc_now()),
            )

    def get_logs(self, run_id: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, level, message, created_at
                FROM logs
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tables"] = json.loads(item.pop("tables_json"))
        item["create_missing_tables"] = bool(item["create_missing_tables"])
        item["skip_exact_count"] = bool(item["skip_exact_count"])
        item["schedule_enabled"] = bool(item["schedule_enabled"])
        return item

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tables"] = json.loads(item.pop("tables_json"))
        item["create_missing_tables"] = bool(item["create_missing_tables"])
        item["skip_exact_count"] = bool(item["skip_exact_count"])
        item["dry_run"] = bool(item["dry_run"])
        return item

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _validate_env(env: str) -> str:
        if env not in {"prod", "test"}:
            raise ValueError("Connection env must be prod or test.")
        return env

    @staticmethod
    def _connection_payload(payload: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
        existing = existing or {}
        password = payload.get("password")
        if (password is None or password == "") and existing.get("password"):
            password = existing["password"]
        return {
            "host": str(payload.get("host") or existing.get("host") or "").strip(),
            "port": int(payload.get("port") or existing.get("port") or 3306),
            "user": str(payload.get("user") or existing.get("user") or "").strip(),
            "password": str(password or ""),
            "database": str(payload.get("database") or existing.get("database") or "").strip(),
            "charset": str(payload.get("charset") or existing.get("charset") or "utf8mb4").strip(),
            "connect_timeout": int(payload.get("connect_timeout") or existing.get("connect_timeout") or 10),
            "read_timeout": int(payload.get("read_timeout") or existing.get("read_timeout") or 120),
            "write_timeout": int(payload.get("write_timeout") or existing.get("write_timeout") or 120),
        }

    @staticmethod
    def _redact_connection(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": payload.get("host", ""),
            "port": int(payload.get("port") or 3306),
            "user": payload.get("user", ""),
            "database": payload.get("database", ""),
            "charset": payload.get("charset", "utf8mb4"),
            "connect_timeout": int(payload.get("connect_timeout") or 10),
            "read_timeout": int(payload.get("read_timeout") or 120),
            "write_timeout": int(payload.get("write_timeout") or 120),
            "password_set": bool(payload.get("password")),
        }
