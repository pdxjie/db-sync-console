from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from math import ceil
from threading import Lock
from time import monotonic
from typing import Any, Callable

from . import mysql
from .config import Config
from .store import SyncStore, utc_now


VALID_MODES = {"replace", "upsert"}
VALID_STRATEGIES = {"offset", "cursor"}
MAX_WORKERS = 8
MAX_SHARDS = 64
RECOMMENDED_CURSOR_BATCH_SIZE = 5000
PROGRESS_AGGREGATE_INTERVAL_SECONDS = 1.5


class SyncPlanError(RuntimeError):
    pass


class SyncEngine:
    def __init__(self, config: Config | Callable[[], Config], store: SyncStore):
        if callable(config):
            self._config_provider = config
        else:
            self._config_provider = lambda: config
        self.store = store

    @property
    def config(self) -> Config:
        return self._config_provider()

    def list_source_tables(self, query: str = "") -> list[dict[str, Any]]:
        self._ensure_config_ready()
        query = query.strip().lower()
        conn = mysql.connect(self.config.prod)
        try:
            tables = mysql.list_tables(conn)
        finally:
            conn.close()
        if query:
            tables = [item for item in tables if query in str(item["name"]).lower()]
        return tables

    def create_plan(
        self,
        tables: list[str],
        mode: str,
        where_clause: str = "",
        batch_size: int | None = None,
        create_missing_tables: bool = False,
        sync_strategy: str = "offset",
        cursor_field: str = "",
        incremental_field: str = "",
        incremental_since: str = "",
        skip_exact_count: bool = False,
        shard_count: int = 1,
        worker_count: int = 1,
    ) -> dict[str, Any]:
        self._ensure_config_ready()
        mysql.ensure_connections_are_distinct(self.config.prod, self.config.test)
        mode = self._validate_mode(mode)
        sync_strategy = self._validate_strategy(sync_strategy)
        batch_size = self._normalize_batch_size(batch_size)
        shard_count = self._normalize_shard_count(shard_count)
        worker_count = self._normalize_worker_count(worker_count)
        selected_tables = self._normalize_tables(tables)
        where_clause = mysql.normalize_where_clause(where_clause)
        create_missing_tables = bool(create_missing_tables)
        incremental_field = mysql.validate_identifier(incremental_field.strip()) if incremental_field.strip() else ""
        incremental_since = incremental_since.strip()
        skip_exact_count = bool(skip_exact_count)

        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        plan_tables: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_rows = 0
        total_bytes = 0
        try:
            for table in selected_tables:
                if table in self.config.safety.blocked_tables:
                    raise SyncPlanError(f"Table is blocked by safety config: {table}")

                try:
                    prod_columns = mysql.describe_columns(prod_conn, table)
                except mysql.TableNotFoundError as exc:
                    raise SyncPlanError(f"Product database table is missing or not visible: {table}") from exc

                target_missing = False
                table_warnings: list[str] = []
                try:
                    test_columns = mysql.describe_columns(test_conn, table)
                except mysql.TableNotFoundError as exc:
                    if not create_missing_tables:
                        raise SyncPlanError(
                            f"Test database table is missing or not visible: {table}. "
                            "Enable 'create missing test tables' or create the table in test first."
                        ) from exc
                    target_missing = True
                    test_columns = prod_columns
                    table_warnings.append("test table is missing; it will be created from product table structure")

                if not target_missing:
                    schema_errors = mysql.compare_column_shapes(prod_columns, test_columns)
                    if schema_errors and self.config.app.strict_schema:
                        raise SyncPlanError(f"Schema mismatch for {table}: {'; '.join(schema_errors)}")
                    table_warnings.extend(schema_errors)

                primary_keys = mysql.primary_key_columns(prod_conn, table)
                prod_column_names = mysql.column_names(prod_columns)
                if mode == "upsert" and not primary_keys:
                    raise SyncPlanError(f"Table {table} has no primary key; upsert cannot be used.")
                if not primary_keys:
                    table_warnings.append("table has no primary key; pagination order is not stable")

                if incremental_field and incremental_field not in prod_column_names:
                    raise SyncPlanError(f"Incremental field {incremental_field} does not exist in {table}.")
                extra_conditions = self._extra_conditions(incremental_field, incremental_since)
                stats = mysql.table_stats(prod_conn, table)
                precise_count = not skip_exact_count
                if skip_exact_count:
                    row_count = stats["estimated_rows"]
                    table_warnings.append("using estimated row count; progress percentage and ETA are approximate")
                else:
                    row_count = mysql.count_rows(prod_conn, table, where_clause, extra_conditions)
                total_rows += row_count
                avg_row_length = int(stats.get("avg_row_length") or 0)
                row_bytes = row_count * avg_row_length
                total_bytes += row_bytes
                if not where_clause and row_count > self.config.safety.max_rows_without_where:
                    table_warnings.append(
                        f"row count {row_count} exceeds max_rows_without_where; consider adding a WHERE clause"
                    )

                resolved_cursor = ""
                cursor_min = None
                cursor_max = None
                shards = []
                effective_shard_count = 1
                if sync_strategy == "cursor":
                    if batch_size < RECOMMENDED_CURSOR_BATCH_SIZE:
                        table_warnings.append(
                            f"batch size {batch_size} is small for big table mode; consider {RECOMMENDED_CURSOR_BATCH_SIZE}+"
                        )
                    resolved_cursor = cursor_field.strip() or (primary_keys[0] if primary_keys else "")
                    if not resolved_cursor:
                        raise SyncPlanError(f"Table {table} has no primary key; choose a cursor field for big table mode.")
                    resolved_cursor = mysql.validate_identifier(resolved_cursor)
                    if resolved_cursor not in prod_column_names:
                        raise SyncPlanError(f"Cursor field {resolved_cursor} does not exist in {table}.")
                    indexed_columns = mysql.indexed_columns(prod_conn, table)
                    if resolved_cursor not in indexed_columns:
                        table_warnings.append(f"cursor field {resolved_cursor} is not indexed; big table mode may be slow")
                    if resolved_cursor not in primary_keys:
                        table_warnings.append(
                            f"cursor field {resolved_cursor} is not the primary key; make sure values are unique and stable"
                        )
                    cursor_range = mysql.min_max_cursor(prod_conn, table, resolved_cursor, where_clause, extra_conditions)
                    cursor_min = cursor_range["min"]
                    cursor_max = cursor_range["max"]
                    shards = self._build_shards(cursor_min, cursor_max, shard_count)
                    effective_shard_count = len(shards)
                    if shard_count > 1 and effective_shard_count == 1:
                        table_warnings.append("cursor range is not numeric or empty; using single cursor worker")

                action = "truncate and insert" if mode == "replace" and not where_clause else mode
                if mode == "replace" and self._has_filter(where_clause, incremental_field, incremental_since):
                    action = "delete matching rows and insert"
                if target_missing:
                    action = f"create test table, {action}"
                if sync_strategy == "cursor":
                    action = f"cursor {action}"

                plan_tables.append(
                    {
                        "name": table,
                        "mode": mode,
                        "action": action,
                        "row_count": row_count,
                        "estimated": not precise_count,
                        "total_bytes": row_bytes,
                        "avg_row_length": avg_row_length,
                        "columns": prod_column_names,
                        "primary_keys": primary_keys,
                        "create_missing_table": target_missing,
                        "sync_strategy": sync_strategy,
                        "cursor_field": resolved_cursor,
                        "cursor_min": str(cursor_min) if cursor_min is not None else None,
                        "cursor_max": str(cursor_max) if cursor_max is not None else None,
                        "shard_count": effective_shard_count,
                        "shards": shards,
                        "warnings": table_warnings,
                    }
                )
                warnings.extend(f"{table}: {message}" for message in table_warnings)
        finally:
            prod_conn.close()
            test_conn.close()

        return {
            "mode": mode,
            "where_clause": where_clause,
            "batch_size": batch_size,
            "create_missing_tables": create_missing_tables,
            "sync_strategy": sync_strategy,
            "cursor_field": cursor_field.strip(),
            "incremental_field": incremental_field,
            "incremental_since": incremental_since,
            "skip_exact_count": skip_exact_count,
            "shard_count": shard_count,
            "worker_count": worker_count,
            "total_rows": total_rows,
            "total_bytes": total_bytes,
            "table_count": len(plan_tables),
            "tables": plan_tables,
            "warnings": warnings,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def prepare_run(
        self,
        payload: dict[str, Any],
        *,
        job_id: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        plan = self.create_plan(
            tables=payload["tables"],
            mode=payload["mode"],
            where_clause=payload.get("where_clause", ""),
            batch_size=payload.get("batch_size"),
            create_missing_tables=payload.get("create_missing_tables", False),
            sync_strategy=payload.get("sync_strategy", "offset"),
            cursor_field=payload.get("cursor_field", ""),
            incremental_field=payload.get("incremental_field", ""),
            incremental_since=payload.get("incremental_since", ""),
            skip_exact_count=payload.get("skip_exact_count", False),
            shard_count=payload.get("shard_count", 1),
            worker_count=payload.get("worker_count", 1),
        )
        run_id = str(uuid.uuid4())
        run_name = name or payload.get("name") or "Manual sync"
        run = self.store.create_run(
            {
                "id": run_id,
                "job_id": job_id,
                "name": run_name,
                "tables": [item["name"] for item in plan["tables"]],
                "mode": plan["mode"],
                "where_clause": plan["where_clause"],
                "batch_size": plan["batch_size"],
                "create_missing_tables": plan["create_missing_tables"],
                "sync_strategy": plan["sync_strategy"],
                "cursor_field": plan["cursor_field"],
                "incremental_field": plan["incremental_field"],
                "incremental_since": plan["incremental_since"],
                "skip_exact_count": plan["skip_exact_count"],
                "shard_count": plan["shard_count"],
                "worker_count": plan["worker_count"],
                "dry_run": bool(payload.get("dry_run")),
                "status": "queued",
                "total_rows": plan["total_rows"],
                "total_bytes": plan["total_bytes"],
            }
        )
        for table in plan["tables"]:
            self.store.create_run_table(
                {
                    "run_id": run_id,
                    "table_name": table["name"],
                    "total_rows": table["row_count"],
                    "processed_rows": 0,
                    "total_bytes": table["total_bytes"],
                    "processed_bytes": 0,
                    "offset_value": 0,
                    "last_pk": None,
                    "shard_count": table["shard_count"],
                    "cursor_field": table["cursor_field"],
                    "status": "pending",
                }
            )
            for index, shard in enumerate(table.get("shards", [])):
                self.store.create_run_shard(
                    {
                        "run_id": run_id,
                        "table_name": table["name"],
                        "shard_index": index,
                        "start_pk": shard.get("start"),
                        "end_pk": shard.get("end"),
                        "last_pk": None,
                        "total_rows": 0,
                        "processed_rows": 0,
                        "total_bytes": 0,
                        "processed_bytes": 0,
                        "status": "pending",
                    }
                )
        self.log(run_id, "info", f"Plan ready: {plan['table_count']} table(s), {plan['total_rows']} row(s).")
        for warning in plan["warnings"]:
            self.log(run_id, "warning", warning)
        return {**run, "plan": plan, "tables_state": self.store.get_run_tables(run_id)}

    def execute_run(self, run_id: str, *, resume: bool = False) -> None:
        run = self.store.get_run(run_id)
        if run["status"] == "success":
            self.log(run_id, "info", "Run already completed.")
            return
        if run["dry_run"]:
            self._complete_dry_run(run_id)
            return

        self.store.update_run(run_id, fetch=False, status="running", started_at=run["started_at"] or utc_now(), error=None)
        self.log(run_id, "info", "Sync started.")
        if run.get("sync_strategy") == "cursor":
            self._execute_cursor_run(run_id, resume=resume)
            return

        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        current_table = None
        try:
            for table_state in self.store.get_run_tables(run_id):
                table = table_state["table_name"]
                current_table = table
                if resume and table_state["status"] == "success":
                    continue

                self.store.update_run(run_id, fetch=False, current_table=table)
                self.store.update_run_table(run_id, table, fetch=False, status="running", error=None)
                prod_columns = mysql.describe_columns(prod_conn, table)
                columns = mysql.column_names(prod_columns)
                primary_keys = mysql.primary_key_columns(prod_conn, table)
                extra_conditions = self._extra_conditions(run.get("incremental_field", ""), run.get("incremental_since", ""))
                offset = int(table_state["offset_value"]) if resume else 0
                processed = int(table_state["processed_rows"]) if resume else 0
                processed_bytes = int(table_state.get("processed_bytes") or 0) if resume else 0
                total_rows = int(table_state["total_rows"])

                target_created = False
                if not mysql.table_exists(test_conn, table):
                    if not run["create_missing_tables"]:
                        raise SyncPlanError(
                            f"Test database table is missing or not visible: {table}. "
                            "Enable 'create missing test tables' and start a new run."
                        )
                    ddl = mysql.show_create_table(prod_conn, table)
                    mysql.create_table_from_ddl(test_conn, ddl)
                    test_conn.commit()
                    target_created = True
                    self.log(run_id, "info", f"{table}: created test table from product structure.")

                if offset == 0 and not target_created:
                    self._prepare_destination_table(test_conn, run, table, extra_conditions)

                while offset < total_rows:
                    rows = mysql.fetch_batch(
                        prod_conn,
                        table,
                        columns,
                        run["where_clause"],
                        primary_keys,
                        int(run["batch_size"]),
                        offset,
                        extra_conditions,
                    )
                    if not rows:
                        break

                    if run["mode"] == "replace":
                        mysql.insert_rows(test_conn, table, columns, rows)
                    elif run["mode"] == "upsert":
                        mysql.upsert_rows(test_conn, table, columns, primary_keys, rows)
                    else:
                        raise SyncPlanError(f"Unsupported mode: {run['mode']}")

                    test_conn.commit()
                    batch_bytes = self._estimate_rows_bytes(rows, columns)
                    offset += len(rows)
                    processed += len(rows)
                    processed_bytes += batch_bytes
                    self.store.update_run_table(
                        run_id,
                        table,
                        fetch=False,
                        processed_rows=processed,
                        processed_bytes=processed_bytes,
                        offset_value=offset,
                        status="running",
                    )
                    self._refresh_run_progress(run_id)
                    self.log(run_id, "info", f"{table}: {processed}/{total_rows} rows synced.")

                self.store.update_run_table(
                    run_id,
                    table,
                    fetch=False,
                    processed_rows=min(processed, total_rows),
                    processed_bytes=processed_bytes,
                    offset_value=min(offset, total_rows),
                    status="success",
                )
                self._refresh_run_progress(run_id)
                self.log(run_id, "info", f"{table}: completed.")

            self.store.update_run(
                run_id,
                fetch=False,
                status="success",
                current_table=None,
                processed_rows=self.store.sum_processed_rows(run_id),
                processed_bytes=self.store.sum_processed_bytes(run_id),
                finished_at=utc_now(),
                error=None,
            )
            self.log(run_id, "info", "Sync completed successfully.")
        except Exception as exc:
            test_conn.rollback()
            message = str(exc)
            if current_table:
                self.store.update_run_table(run_id, current_table, status="failed", error=message)
            self.store.update_run(run_id, status="failed", error=message, finished_at=utc_now())
            self.log(run_id, "error", message)
            raise
        finally:
            prod_conn.close()
            test_conn.close()

    def _execute_cursor_run(self, run_id: str, *, resume: bool = False) -> None:
        run = self.store.get_run(run_id)
        current_table = None
        try:
            for table_state in self.store.get_run_tables(run_id):
                table = table_state["table_name"]
                current_table = table
                if resume and table_state["status"] == "success":
                    continue
                self.store.update_run(run_id, fetch=False, current_table=table)
                self.store.update_run_table(run_id, table, fetch=False, status="running", error=None)

                prod_conn = mysql.connect(self.config.prod)
                test_conn = mysql.connect(self.config.test)
                try:
                    prod_columns = mysql.describe_columns(prod_conn, table)
                    columns = mysql.column_names(prod_columns)
                    primary_keys = mysql.primary_key_columns(prod_conn, table)
                    extra_conditions = self._extra_conditions(run.get("incremental_field", ""), run.get("incremental_since", ""))
                    should_prepare = not resume or int(table_state.get("processed_rows") or 0) == 0
                    if should_prepare:
                        self._ensure_destination_table(prod_conn, test_conn, run, table)
                        self._prepare_destination_table(test_conn, run, table, extra_conditions)
                finally:
                    prod_conn.close()
                    test_conn.close()

                shards = self.store.get_run_shards(run_id, table)
                if not shards:
                    shards = [
                        self.store.create_run_shard(
                            {
                                "run_id": run_id,
                                "table_name": table,
                                "shard_index": 0,
                                "start_pk": None,
                                "end_pk": None,
                                "last_pk": table_state.get("last_pk"),
                                "status": "pending",
                            }
                        )
                    ]

                worker_count = max(1, min(int(run.get("worker_count") or 1), len(shards), MAX_WORKERS))
                self.log(
                    run_id,
                    "info",
                    f"{table}: cursor sync using {len(shards)} shard(s), {worker_count} worker(s), batch size {run['batch_size']}.",
                )
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = []
                    for shard in shards:
                        if resume and shard["status"] == "success":
                            continue
                        futures.append(
                            executor.submit(
                                self._execute_cursor_shard,
                                run,
                                table,
                                shard,
                                columns,
                                primary_keys,
                            )
                        )
                    for future in as_completed(futures):
                        future.result()

                self.store.aggregate_shards(run_id, table)
                self._refresh_run_progress(run_id)
                final_table = self.store.get_run_table(run_id, table)
                if final_table["status"] != "success":
                    self.store.update_run_table(run_id, table, status="success")
                self.log(run_id, "info", f"{table}: cursor sync completed.")

            self.store.update_run(
                run_id,
                fetch=False,
                status="success",
                current_table=None,
                processed_rows=self.store.sum_processed_rows(run_id),
                processed_bytes=self.store.sum_processed_bytes(run_id),
                finished_at=utc_now(),
                error=None,
            )
            self.log(run_id, "info", "Sync completed successfully.")
        except Exception as exc:
            message = str(exc)
            if current_table:
                self.store.update_run_table(run_id, current_table, fetch=False, status="failed", error=message)
            self.store.update_run(run_id, fetch=False, status="failed", error=message, finished_at=utc_now())
            self.log(run_id, "error", message)
            raise

    def _execute_cursor_shard(
        self,
        run: dict[str, Any],
        table: str,
        shard: dict[str, Any],
        columns: list[str],
        primary_keys: list[str],
    ) -> None:
        run_id = run["id"]
        shard_index = int(shard["shard_index"])
        cursor_field = run.get("cursor_field") or self.store.get_run_table(run_id, table).get("cursor_field")
        if not cursor_field:
            raise SyncPlanError(f"Cursor field is missing for {table}.")
        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        processed = int(shard.get("processed_rows") or 0)
        processed_bytes = int(shard.get("processed_bytes") or 0)
        last_pk = shard.get("last_pk")
        extra_conditions = self._extra_conditions(run.get("incremental_field", ""), run.get("incremental_since", ""))
        last_aggregate_at = monotonic()
        try:
            self.store.update_run_shard(run_id, table, shard_index, fetch=False, status="running", error=None)
            self.log(
                run_id,
                "info",
                f"{table} shard {shard_index}: range {shard.get('start_pk') or '-'}..{shard.get('end_pk') or '-'}, resume from {last_pk or '-'}.",
            )
            while True:
                rows = mysql.fetch_cursor_batch(
                    prod_conn,
                    table,
                    columns,
                    run["where_clause"],
                    cursor_field,
                    int(run["batch_size"]),
                    last_pk=last_pk,
                    shard_start=shard.get("start_pk"),
                    shard_end=shard.get("end_pk"),
                    extra_conditions=extra_conditions,
                )
                if not rows:
                    break

                if run["mode"] == "replace":
                    mysql.insert_rows(test_conn, table, columns, rows)
                elif run["mode"] == "upsert":
                    mysql.upsert_rows(test_conn, table, columns, primary_keys, rows)
                else:
                    raise SyncPlanError(f"Unsupported mode: {run['mode']}")

                test_conn.commit()
                batch_bytes = self._estimate_rows_bytes(rows, columns)
                processed += len(rows)
                processed_bytes += batch_bytes
                last_pk = rows[-1][cursor_field]
                self.store.update_run_shard(
                    run_id,
                    table,
                    shard_index,
                    fetch=False,
                    last_pk=last_pk,
                    processed_rows=processed,
                    processed_bytes=processed_bytes,
                    status="running",
                )
                now = monotonic()
                if now - last_aggregate_at >= PROGRESS_AGGREGATE_INTERVAL_SECONDS:
                    self.store.aggregate_shards(run_id, table)
                    self._refresh_run_progress(run_id)
                    last_aggregate_at = now

            self.store.update_run_shard(
                run_id,
                table,
                shard_index,
                fetch=False,
                last_pk=last_pk,
                processed_rows=processed,
                processed_bytes=processed_bytes,
                status="success",
            )
            self.store.aggregate_shards(run_id, table)
            self._refresh_run_progress(run_id)
            self.log(run_id, "info", f"{table} shard {shard_index}: {processed} row(s) synced, last_pk={last_pk}.")
        except Exception as exc:
            test_conn.rollback()
            self.store.update_run_shard(run_id, table, shard_index, fetch=False, status="failed", error=str(exc))
            raise
        finally:
            prod_conn.close()
            test_conn.close()

    def log(self, run_id: str, level: str, message: str) -> None:
        self.store.append_log(run_id, level, message)
        log_dir = self.config.app.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{run_id}.log"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} [{level.upper()}] {message}\n")

    def _ensure_destination_table(self, prod_conn, test_conn, run: dict[str, Any], table: str) -> bool:
        if mysql.table_exists(test_conn, table):
            return False
        if not run["create_missing_tables"]:
            raise SyncPlanError(
                f"Test database table is missing or not visible: {table}. "
                "Enable 'create missing test tables' and start a new run."
            )
        ddl = mysql.show_create_table(prod_conn, table)
        mysql.create_table_from_ddl(test_conn, ddl)
        test_conn.commit()
        self.log(run["id"], "info", f"{table}: created test table from product structure.")
        return True

    def _prepare_destination_table(
        self,
        test_conn,
        run: dict[str, Any],
        table: str,
        extra_conditions: list[tuple[str, tuple[Any, ...]]] | None = None,
    ) -> None:
        if run["mode"] != "replace":
            return
        if self._has_filter(run.get("where_clause", ""), run.get("incremental_field", ""), run.get("incremental_since", "")):
            deleted = mysql.delete_where(test_conn, table, run["where_clause"], extra_conditions)
            test_conn.commit()
            self.log(run["id"], "info", f"{table}: deleted {deleted} matching row(s) from test.")
        else:
            mysql.truncate_table(test_conn, table)
            test_conn.commit()
            self.log(run["id"], "info", f"{table}: truncated test table.")

    def _complete_dry_run(self, run_id: str) -> None:
        self.store.update_run(run_id, fetch=False, status="running", started_at=utc_now())
        self.log(run_id, "info", "Dry run completed. No data was written to test.")
        for table_state in self.store.get_run_tables(run_id):
            self.store.update_run_table(
                run_id,
                table_state["table_name"],
                fetch=False,
                status="success",
                processed_rows=0,
                offset_value=0,
            )
            for shard in self.store.get_run_shards(run_id, table_state["table_name"]):
                self.store.update_run_shard(
                    run_id,
                    table_state["table_name"],
                    int(shard["shard_index"]),
                    fetch=False,
                    status="success",
                    processed_rows=0,
                    processed_bytes=0,
                )
        self.store.update_run(run_id, fetch=False, status="success", finished_at=utc_now(), current_table=None)

    def _refresh_run_progress(self, run_id: str) -> None:
        self.store.update_run(
            run_id,
            fetch=False,
            processed_rows=self.store.sum_processed_rows(run_id),
            processed_bytes=self.store.sum_processed_bytes(run_id),
        )

    def _extra_conditions(self, incremental_field: str, incremental_since: str) -> list[tuple[str, tuple[Any, ...]]]:
        if not incremental_field or not incremental_since:
            return []
        condition = mysql.incremental_condition(incremental_field, incremental_since)
        return [condition] if condition else []

    def _has_filter(self, where_clause: str | None, incremental_field: str, incremental_since: str) -> bool:
        return bool(mysql.normalize_where_clause(where_clause) or (incremental_field and incremental_since))

    def _build_shards(self, min_value: Any, max_value: Any, shard_count: int) -> list[dict[str, Any]]:
        if min_value is None or max_value is None:
            return [{"start": None, "end": None}]
        if not isinstance(min_value, int) or not isinstance(max_value, int) or shard_count <= 1 or min_value >= max_value:
            return [{"start": min_value, "end": max_value}]
        span = max_value - min_value + 1
        shard_size = max(1, ceil(span / shard_count))
        shards: list[dict[str, Any]] = []
        start = min_value
        while start <= max_value and len(shards) < shard_count:
            end = min(max_value, start + shard_size - 1)
            shards.append({"start": start, "end": end})
            start = end + 1
        return shards or [{"start": min_value, "end": max_value}]

    def _estimate_rows_bytes(self, rows: list[dict[str, Any]], columns: list[str]) -> int:
        total = 0
        for row in rows:
            for column in columns:
                value = row.get(column)
                if value is None:
                    continue
                if isinstance(value, bytes):
                    total += len(value)
                elif isinstance(value, str):
                    total += len(value.encode("utf-8"))
                else:
                    total += len(str(value).encode("utf-8"))
        return total

    def _ensure_config_ready(self) -> None:
        missing = []
        for label, db in (("prod", self.config.prod), ("test", self.config.test)):
            if not db.host:
                missing.append(f"{label}.host")
            if not db.user:
                missing.append(f"{label}.user")
            if not db.database:
                missing.append(f"{label}.database")
        if missing:
            raise SyncPlanError(
                f"Missing database connection settings: {', '.join(missing)}. Set connections in the web UI first."
            )

    def _normalize_tables(self, tables: list[str]) -> list[str]:
        normalized: list[str] = []
        seen = set()
        for raw in tables:
            table = mysql.validate_identifier(str(raw).strip())
            if table in seen:
                continue
            seen.add(table)
            normalized.append(table)
        if not normalized:
            raise SyncPlanError("Select at least one table.")
        return normalized

    def _validate_mode(self, mode: str) -> str:
        mode = str(mode).strip().lower()
        if mode not in VALID_MODES:
            raise SyncPlanError(f"Unsupported mode: {mode}. Expected one of: {', '.join(sorted(VALID_MODES))}")
        return mode

    def _validate_strategy(self, strategy: str) -> str:
        strategy = str(strategy or "offset").strip().lower()
        if strategy not in VALID_STRATEGIES:
            raise SyncPlanError(
                f"Unsupported sync strategy: {strategy}. Expected one of: {', '.join(sorted(VALID_STRATEGIES))}"
            )
        return strategy

    def _normalize_batch_size(self, batch_size: int | None) -> int:
        value = int(batch_size or self.config.app.page_size)
        if value < 1:
            raise SyncPlanError("Batch size must be greater than 0.")
        return value

    def _normalize_shard_count(self, shard_count: int | None) -> int:
        value = int(shard_count or 1)
        if value < 1:
            raise SyncPlanError("Shard count must be greater than 0.")
        return min(value, MAX_SHARDS)

    def _normalize_worker_count(self, worker_count: int | None) -> int:
        value = int(worker_count or 1)
        if value < 1:
            raise SyncPlanError("Worker count must be greater than 0.")
        return min(value, MAX_WORKERS)


class SyncManager:
    def __init__(self, engine: SyncEngine, *, max_workers: int = 1):
        self.engine = engine
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = Lock()
        self._submitted: set[str] = set()

    def start(self, payload: dict[str, Any], *, job_id: int | None = None, name: str | None = None) -> dict[str, Any]:
        run = self.engine.prepare_run(payload, job_id=job_id, name=name)
        self._submit(run["id"], resume=False)
        return self.get_run(run["id"])

    def resume(self, run_id: str) -> dict[str, Any]:
        run = self.engine.store.get_run(run_id)
        if run["status"] not in {"failed", "queued"}:
            raise SyncPlanError(f"Run {run_id} cannot be resumed from status {run['status']}.")
        self.engine.store.update_run(run_id, status="queued", error=None, finished_at=None)
        self.engine.log(run_id, "info", "Run queued for resume.")
        self._submit(run_id, resume=True)
        return self.get_run(run_id)

    def start_job(self, job_id: int, *, source: str = "manual") -> dict[str, Any]:
        job = self.engine.store.get_job(job_id)
        payload = {
            "tables": job["tables"],
            "mode": job["mode"],
            "where_clause": job["where_clause"],
            "batch_size": job["batch_size"],
            "create_missing_tables": job["create_missing_tables"],
            "sync_strategy": job.get("sync_strategy", "offset"),
            "cursor_field": job.get("cursor_field", ""),
            "incremental_field": job.get("incremental_field", ""),
            "incremental_since": job.get("incremental_since", ""),
            "skip_exact_count": job.get("skip_exact_count", False),
            "shard_count": job.get("shard_count", 1),
            "worker_count": job.get("worker_count", 1),
            "dry_run": False,
            "name": job["name"],
        }
        return self.start(payload, job_id=job_id, name=f"{job['name']} ({source})")

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.engine.store.get_run(run_id)
        metrics = self._run_metrics(run)
        return {
            **run,
            **metrics,
            "tables_state": self.engine.store.get_run_tables(run_id),
            "shards_state": self.engine.store.get_run_shards(run_id),
            "logs": self.engine.store.get_logs(run_id),
        }

    def shutdown(self, wait: bool = False) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=not wait)

    def _submit(self, run_id: str, *, resume: bool) -> None:
        with self._lock:
            self._submitted.add(run_id)
        future = self.executor.submit(self.engine.execute_run, run_id, resume=resume)
        future.add_done_callback(lambda _: self._discard(run_id))

    def _discard(self, run_id: str) -> None:
        with self._lock:
            self._submitted.discard(run_id)

    def _run_metrics(self, run: dict[str, Any]) -> dict[str, Any]:
        started_at = run.get("started_at")
        elapsed_seconds = 0.0
        if started_at:
            try:
                start = datetime.fromisoformat(started_at)
                elapsed_seconds = max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
            except ValueError:
                elapsed_seconds = 0.0
        processed_rows = int(run.get("processed_rows") or 0)
        processed_bytes = int(run.get("processed_bytes") or 0)
        total_rows = int(run.get("total_rows") or 0)
        rows_per_second = processed_rows / elapsed_seconds if elapsed_seconds > 0 else 0.0
        bytes_per_second = processed_bytes / elapsed_seconds if elapsed_seconds > 0 else 0.0
        eta_seconds = None
        if rows_per_second > 0 and total_rows > processed_rows:
            eta_seconds = int((total_rows - processed_rows) / rows_per_second)
        return {
            "elapsed_seconds": round(elapsed_seconds, 1),
            "rows_per_second": round(rows_per_second, 2),
            "bytes_per_second": round(bytes_per_second, 2),
            "synced_gb": round(processed_bytes / (1024 ** 3), 4),
            "eta_seconds": eta_seconds,
        }
