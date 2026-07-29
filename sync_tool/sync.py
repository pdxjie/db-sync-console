from __future__ import annotations

import uuid
import json
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
VALID_STRATEGIES = {"auto", "offset", "cursor"}
MAX_WORKERS = 8
MAX_SHARDS = 64
RECOMMENDED_CURSOR_BATCH_SIZE = 5000
PROGRESS_AGGREGATE_INTERVAL_SECONDS = 1.5


class SyncPlanError(RuntimeError):
    pass


class SyncPaused(RuntimeError):
    pass


class SyncCancelled(RuntimeError):
    pass


class SyncEngine:
    def __init__(
        self,
        config: Config | Callable[[], Config],
        store: SyncStore,
        event_callback: Callable[[str], None] | None = None,
    ):
        if callable(config):
            self._config_provider = config
        else:
            self._config_provider = lambda: config
        self.store = store
        self._event_callback = event_callback

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
        sync_strategy: str = "auto",
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

                prod_column_names = mysql.column_names(prod_columns)
                nullable_source_columns = {
                    str(column["name"])
                    for column in prod_columns
                    if str(column.get("nullable", "")).upper() == "YES"
                }
                target_primary_keys = []
                if target_missing:
                    column_plan = {
                        "write_columns": prod_column_names,
                        "source_only_columns": [],
                        "target_only_columns": [],
                        "type_mismatches": [],
                        "required_target_only_columns": [],
                    }
                else:
                    schema_errors = mysql.compare_column_shapes(prod_columns, test_columns)
                    if schema_errors and self.config.app.strict_schema:
                        raise SyncPlanError(f"Schema mismatch for {table}: {'; '.join(schema_errors)}")
                    column_plan = mysql.sync_column_plan(prod_columns, test_columns)
                    target_primary_keys = mysql.primary_key_columns(test_conn, table)
                    if not column_plan["write_columns"]:
                        raise SyncPlanError(f"Table {table} has no common columns between product and test.")
                    if column_plan["source_only_columns"]:
                        table_warnings.append(
                            "source-only columns will be skipped: " + ", ".join(column_plan["source_only_columns"])
                        )
                    if column_plan["target_only_columns"]:
                        table_warnings.append(
                            "test-only columns will be preserved by schema and not written: "
                            + ", ".join(column_plan["target_only_columns"])
                        )
                    if column_plan["required_target_only_columns"]:
                        table_warnings.append(
                            "test-only required columns without defaults may reject new rows: "
                            + ", ".join(column_plan["required_target_only_columns"])
                        )
                    for mismatch in column_plan["type_mismatches"]:
                        table_warnings.append(
                            "common column type differs and will rely on MySQL conversion: "
                            f"{mismatch['name']} prod={mismatch['prod_type']} test={mismatch['test_type']}"
                        )

                primary_keys = mysql.primary_key_columns(prod_conn, table)
                source_indexes = mysql.list_indexes(prod_conn, table)
                if target_missing:
                    target_primary_keys = primary_keys
                write_primary_keys = target_primary_keys or primary_keys
                if mode == "upsert" and not target_primary_keys:
                    raise SyncPlanError(f"Table {table} has no target primary key; upsert cannot be used.")
                missing_write_keys = [key for key in write_primary_keys if key not in column_plan["write_columns"]]
                if mode == "upsert" and missing_write_keys:
                    raise SyncPlanError(
                        f"Table {table} target primary key column(s) are not available from product data: "
                        + ", ".join(missing_write_keys)
                    )
                if target_primary_keys and primary_keys and target_primary_keys != primary_keys:
                    table_warnings.append(
                        "test primary key differs from product primary key; upsert will use test primary key: "
                        + ", ".join(target_primary_keys)
                    )
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
                resolved_cursor_fields: list[str] = []
                cursor_min = None
                cursor_max = None
                shards = []
                effective_shard_count = 1
                pagination = self._choose_pagination(
                    table=table,
                    requested_strategy=sync_strategy,
                    cursor_field=cursor_field,
                    prod_column_names=prod_column_names,
                    nullable_source_columns=nullable_source_columns,
                    primary_keys=primary_keys,
                    indexes=source_indexes,
                    row_count=row_count,
                )
                table_warnings.extend(pagination["warnings"])
                effective_strategy = pagination["effective_strategy"]
                if effective_strategy == "cursor":
                    if batch_size < RECOMMENDED_CURSOR_BATCH_SIZE:
                        table_warnings.append(
                            f"batch size {batch_size} is small for keyset pagination; consider {RECOMMENDED_CURSOR_BATCH_SIZE}+"
                        )
                    resolved_cursor_fields = pagination["cursor_fields"]
                    resolved_cursor = self._format_cursor_fields(resolved_cursor_fields)
                    if len(resolved_cursor_fields) == 1:
                        cursor_range = mysql.min_max_cursor(
                            prod_conn,
                            table,
                            resolved_cursor_fields[0],
                            where_clause,
                            extra_conditions,
                        )
                        cursor_min = cursor_range["min"]
                        cursor_max = cursor_range["max"]
                        shards = self._build_shards(cursor_min, cursor_max, shard_count)
                    else:
                        shards = [{"start": None, "end": None}]
                        if shard_count > 1:
                            table_warnings.append(
                                "composite cursor uses one shard for correctness; numeric single-column cursors can use sharding"
                            )
                    effective_shard_count = len(shards)
                    if shard_count > 1 and effective_shard_count == 1:
                        table_warnings.append("cursor range is not numeric or empty; using single cursor worker")

                action = "truncate and insert" if mode == "replace" and not where_clause else mode
                if mode == "replace" and self._has_filter(where_clause, incremental_field, incremental_since):
                    action = "delete matching rows and insert"
                if target_missing:
                    action = f"create test table, {action}"
                if effective_strategy == "cursor":
                    action = f"keyset {action}"
                elif sync_strategy == "auto":
                    action = f"offset {action}"

                plan_tables.append(
                    {
                        "name": table,
                        "mode": mode,
                        "action": action,
                        "row_count": row_count,
                        "estimated": not precise_count,
                        "total_bytes": row_bytes,
                        "avg_row_length": avg_row_length,
                        "columns": column_plan["write_columns"],
                        "source_columns": prod_column_names,
                        "target_columns": mysql.column_names(test_columns),
                        "source_only_columns": column_plan["source_only_columns"],
                        "target_only_columns": column_plan["target_only_columns"],
                        "primary_keys": primary_keys,
                        "target_primary_keys": target_primary_keys,
                        "write_primary_keys": write_primary_keys,
                        "create_missing_table": target_missing,
                        "sync_strategy": effective_strategy,
                        "requested_sync_strategy": sync_strategy,
                        "pagination_strategy": effective_strategy,
                        "cursor_field": resolved_cursor,
                        "cursor_fields": resolved_cursor_fields,
                        "cursor_index": pagination.get("cursor_index"),
                        "cursor_unique": pagination.get("cursor_unique", False),
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

        effective_strategies = {table["sync_strategy"] for table in plan_tables}
        effective_sync_strategy = (
            next(iter(effective_strategies))
            if len(effective_strategies) == 1
            else "mixed"
            if effective_strategies
            else sync_strategy
        )
        return {
            "mode": mode,
            "where_clause": where_clause,
            "batch_size": batch_size,
            "create_missing_tables": create_missing_tables,
            "sync_strategy": sync_strategy,
            "effective_sync_strategy": effective_sync_strategy,
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
            sync_strategy=payload.get("sync_strategy", "auto"),
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
        if run["status"] in {"canceled", "cancel_requested"} and not resume:
            self._finalize_canceled_run(run_id)
            return
        if run["status"] in {"paused", "pause_requested"} and not resume:
            self._finalize_paused_run(run_id)
            return
        if run["dry_run"]:
            self._complete_dry_run(run_id)
            return

        self.store.update_run(run_id, fetch=False, status="running", started_at=run["started_at"] or utc_now(), error=None)
        self.log(run_id, "info", "Sync started.")
        current_table = None
        try:
            for table_state in self.store.get_run_tables(run_id):
                table = table_state["table_name"]
                current_table = table
                if resume and table_state["status"] == "success":
                    continue
                self._raise_if_stop_requested(run_id)

                if table_state.get("cursor_field"):
                    self._execute_cursor_table(run, table_state, resume=resume)
                else:
                    self._execute_offset_table(run, table_state, resume=resume)

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
        except SyncPaused:
            self._finalize_paused_run(run_id, current_table)
        except SyncCancelled:
            self._finalize_canceled_run(run_id, current_table)
        except Exception as exc:
            message = str(exc)
            if current_table:
                self.store.update_run_table(run_id, current_table, status="failed", error=message)
            self.store.update_run(run_id, status="failed", error=message, finished_at=utc_now())
            self.log(run_id, "error", message)
            raise

    def _execute_offset_table(self, run: dict[str, Any], table_state: dict[str, Any], *, resume: bool = False) -> None:
        run_id = run["id"]
        table = table_state["table_name"]
        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        try:
            self.store.update_run(run_id, fetch=False, current_table=table)
            self.store.update_run_table(run_id, table, fetch=False, status="running", error=None)
            self._raise_if_stop_requested(run_id, table)
            extra_conditions = self._extra_conditions(run.get("incremental_field", ""), run.get("incremental_since", ""))
            offset = int(table_state["offset_value"]) if resume else 0
            processed = int(table_state["processed_rows"]) if resume else 0
            processed_bytes = int(table_state.get("processed_bytes") or 0) if resume else 0
            total_rows = int(table_state["total_rows"])

            target_created = self._ensure_destination_table(prod_conn, test_conn, run, table)
            column_info = self._resolve_table_columns(prod_conn, test_conn, run, table)
            fetch_columns = column_info["fetch_columns"]
            write_columns = column_info["write_columns"]
            order_columns = column_info["source_primary_keys"]
            write_primary_keys = column_info["write_primary_keys"]

            if offset == 0 and not target_created:
                self._prepare_destination_table(test_conn, run, table, extra_conditions)

            self.log(run_id, "warning", f"{table}: using offset pagination; large offsets can become slower over time.")
            count_is_estimated = bool(run.get("skip_exact_count"))
            while count_is_estimated or offset < total_rows:
                self._raise_if_stop_requested(run_id, table)
                rows = mysql.fetch_batch(
                    prod_conn,
                    table,
                    fetch_columns,
                    run["where_clause"],
                    order_columns,
                    int(run["batch_size"]),
                    offset,
                    extra_conditions,
                )
                if not rows:
                    break

                if run["mode"] == "replace":
                    mysql.insert_rows(test_conn, table, write_columns, rows)
                elif run["mode"] == "upsert":
                    mysql.upsert_rows(test_conn, table, write_columns, write_primary_keys, rows)
                else:
                    raise SyncPlanError(f"Unsupported mode: {run['mode']}")

                test_conn.commit()
                batch_bytes = self._estimate_rows_bytes(rows, write_columns)
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

            final_total_rows = max(total_rows, processed) if count_is_estimated else total_rows
            self.store.update_run_table(
                run_id,
                table,
                fetch=False,
                total_rows=final_total_rows,
                processed_rows=min(processed, final_total_rows),
                processed_bytes=processed_bytes,
                offset_value=min(offset, final_total_rows),
                status="success",
            )
            self._refresh_run_progress(run_id)
            self.log(run_id, "info", f"{table}: completed.")
        except Exception:
            test_conn.rollback()
            raise
        finally:
            prod_conn.close()
            test_conn.close()

    def _execute_cursor_table(self, run: dict[str, Any], table_state: dict[str, Any], *, resume: bool = False) -> None:
        run_id = run["id"]
        table = table_state["table_name"]
        self.store.update_run(run_id, fetch=False, current_table=table)
        self.store.update_run_table(run_id, table, fetch=False, status="running", error=None)
        self._raise_if_stop_requested(run_id, table)

        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        try:
            extra_conditions = self._extra_conditions(run.get("incremental_field", ""), run.get("incremental_since", ""))
            should_prepare = not resume or int(table_state.get("processed_rows") or 0) == 0
            target_created = False
            if should_prepare:
                target_created = self._ensure_destination_table(prod_conn, test_conn, run, table)
                if not target_created:
                    self._prepare_destination_table(test_conn, run, table, extra_conditions)
            cursor_field = table_state.get("cursor_field") or run.get("cursor_field", "")
            column_info = self._resolve_table_columns(
                prod_conn,
                test_conn,
                run,
                table,
                cursor_field=cursor_field,
            )
            fetch_columns = column_info["fetch_columns"]
            write_columns = column_info["write_columns"]
            write_primary_keys = column_info["write_primary_keys"]
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
            f"{table}: keyset sync using {len(shards)} shard(s), {worker_count} worker(s), batch size {run['batch_size']}.",
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
                        fetch_columns,
                        write_columns,
                        write_primary_keys,
                    )
                )
            control_error: SyncPaused | SyncCancelled | None = None
            for future in as_completed(futures):
                try:
                    future.result()
                except (SyncPaused, SyncCancelled) as exc:
                    control_error = exc
            if control_error:
                self.store.aggregate_shards(run_id, table)
                self._refresh_run_progress(run_id)
                raise control_error

        self.store.aggregate_shards(run_id, table)
        self._refresh_run_progress(run_id)
        final_table = self.store.get_run_table(run_id, table)
        if final_table["status"] != "success":
            self.store.update_run_table(run_id, table, status="success")
        self.log(run_id, "info", f"{table}: keyset sync completed.")

    def _execute_cursor_shard(
        self,
        run: dict[str, Any],
        table: str,
        shard: dict[str, Any],
        fetch_columns: list[str],
        write_columns: list[str],
        write_primary_keys: list[str],
    ) -> None:
        run_id = run["id"]
        shard_index = int(shard["shard_index"])
        cursor_field = self.store.get_run_table(run_id, table).get("cursor_field") or run.get("cursor_field", "")
        cursor_fields = self._parse_cursor_fields(cursor_field)
        if not cursor_fields:
            raise SyncPlanError(f"Cursor field is missing for {table}.")
        prod_conn = mysql.connect(self.config.prod)
        test_conn = mysql.connect(self.config.test)
        processed = int(shard.get("processed_rows") or 0)
        processed_bytes = int(shard.get("processed_bytes") or 0)
        last_pk = shard.get("last_pk")
        last_values = self._decode_cursor_values(last_pk, len(cursor_fields))
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
                self._raise_if_stop_requested(run_id, table, shard_index)
                rows = mysql.fetch_keyset_batch(
                    prod_conn,
                    table,
                    fetch_columns,
                    run["where_clause"],
                    cursor_fields,
                    int(run["batch_size"]),
                    last_values=last_values,
                    shard_start=shard.get("start_pk"),
                    shard_end=shard.get("end_pk"),
                    extra_conditions=extra_conditions,
                )
                if not rows:
                    break

                if run["mode"] == "replace":
                    mysql.insert_rows(test_conn, table, write_columns, rows)
                elif run["mode"] == "upsert":
                    mysql.upsert_rows(test_conn, table, write_columns, write_primary_keys, rows)
                else:
                    raise SyncPlanError(f"Unsupported mode: {run['mode']}")

                test_conn.commit()
                batch_bytes = self._estimate_rows_bytes(rows, write_columns)
                processed += len(rows)
                processed_bytes += batch_bytes
                last_values = [rows[-1][field] for field in cursor_fields]
                last_pk = self._encode_cursor_values(last_values)
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
        except (SyncPaused, SyncCancelled):
            raise
        except Exception as exc:
            test_conn.rollback()
            self.store.update_run_shard(run_id, table, shard_index, fetch=False, status="failed", error=str(exc))
            raise
        finally:
            prod_conn.close()
            test_conn.close()

    def _raise_if_pause_requested(self, run_id: str, table: str | None = None, shard_index: int | None = None) -> None:
        self._raise_if_stop_requested(run_id, table, shard_index)

    def _raise_if_stop_requested(self, run_id: str, table: str | None = None, shard_index: int | None = None) -> None:
        run = self.store.get_run(run_id)
        status = run["status"]
        if status not in {"pause_requested", "cancel_requested"}:
            return
        next_status = "paused" if status == "pause_requested" else "canceled"
        if table and shard_index is not None:
            self.store.update_run_shard(run_id, table, shard_index, fetch=False, status=next_status, error=None)
        elif table:
            self.store.update_run_table(run_id, table, fetch=False, status=next_status, error=None)
        if status == "cancel_requested":
            raise SyncCancelled("Sync canceled.")
        raise SyncPaused("Sync paused.")

    def _finalize_paused_run(self, run_id: str, current_table: str | None = None) -> None:
        if self.store.get_run(run_id)["status"] == "cancel_requested":
            self._finalize_canceled_run(run_id, current_table)
            return
        if current_table:
            try:
                table_state = self.store.get_run_table(run_id, current_table)
            except KeyError:
                table_state = None
            if table_state and table_state["status"] in {"pending", "running", "pause_requested"}:
                self.store.update_run_table(run_id, current_table, fetch=False, status="paused", error=None)
            for shard in self.store.get_run_shards(run_id, current_table):
                if shard["status"] in {"pending", "running", "pause_requested"}:
                    self.store.update_run_shard(
                        run_id,
                        current_table,
                        int(shard["shard_index"]),
                        fetch=False,
                        status="paused",
                        error=None,
                    )
        self._refresh_run_progress(run_id)
        self.store.update_run(
            run_id,
            fetch=False,
            status="paused",
            current_table=current_table,
            error=None,
            finished_at=utc_now(),
        )
        self.log(run_id, "info", "Sync paused. Resume will continue from the saved offset or last_pk.")

    def _finalize_canceled_run(self, run_id: str, current_table: str | None = None) -> None:
        for table_state in self.store.get_run_tables(run_id):
            if table_state["status"] in {"pending", "running", "paused", "pause_requested", "cancel_requested"}:
                self.store.update_run_table(
                    run_id,
                    table_state["table_name"],
                    fetch=False,
                    status="canceled",
                    error=None,
                )
            for shard in self.store.get_run_shards(run_id, table_state["table_name"]):
                if shard["status"] in {"pending", "running", "paused", "pause_requested", "cancel_requested"}:
                    self.store.update_run_shard(
                        run_id,
                        table_state["table_name"],
                        int(shard["shard_index"]),
                        fetch=False,
                        status="canceled",
                        error=None,
                    )
        self._refresh_run_progress(run_id)
        self.store.update_run(
            run_id,
            fetch=False,
            status="canceled",
            current_table=current_table,
            error=None,
            finished_at=utc_now(),
        )
        self.log(run_id, "info", "Sync canceled. This run cannot be resumed.")

    def log(self, run_id: str, level: str, message: str) -> None:
        self.store.append_log(run_id, level, message)
        log_dir = self.config.app.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{run_id}.log"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} [{level.upper()}] {message}\n")
        self.emit_run_event(run_id)

    def emit_run_event(self, run_id: str) -> None:
        if not self._event_callback:
            return
        try:
            self._event_callback(run_id)
        except Exception:
            pass

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

    def _resolve_table_columns(
        self,
        prod_conn,
        test_conn,
        run: dict[str, Any],
        table: str,
        *,
        cursor_field: str = "",
    ) -> dict[str, Any]:
        prod_columns = mysql.describe_columns(prod_conn, table)
        test_columns = mysql.describe_columns(test_conn, table)
        column_plan = mysql.sync_column_plan(prod_columns, test_columns)
        write_columns = column_plan["write_columns"]
        if not write_columns:
            raise SyncPlanError(f"Table {table} has no common columns between product and test.")

        source_primary_keys = mysql.primary_key_columns(prod_conn, table)
        target_primary_keys = mysql.primary_key_columns(test_conn, table)
        write_primary_keys = target_primary_keys or source_primary_keys
        if run["mode"] == "upsert":
            if not target_primary_keys:
                raise SyncPlanError(f"Table {table} has no target primary key; upsert cannot be used.")
            missing_write_keys = [key for key in write_primary_keys if key not in write_columns]
            if missing_write_keys:
                raise SyncPlanError(
                    f"Table {table} target primary key column(s) are not available from product data: "
                    + ", ".join(missing_write_keys)
                )

        fetch_columns = list(write_columns)
        prod_column_names = mysql.column_names(prod_columns)
        for field in self._parse_cursor_fields(cursor_field):
            if field not in fetch_columns:
                if field not in prod_column_names:
                    raise SyncPlanError(f"Cursor field {field} does not exist in {table}.")
                fetch_columns.append(field)

        return {
            "fetch_columns": fetch_columns,
            "write_columns": write_columns,
            "source_primary_keys": source_primary_keys,
            "target_primary_keys": target_primary_keys,
            "write_primary_keys": write_primary_keys,
            "source_only_columns": column_plan["source_only_columns"],
            "target_only_columns": column_plan["target_only_columns"],
        }

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
        self.emit_run_event(run_id)

    def _refresh_run_progress(self, run_id: str) -> None:
        tables = self.store.get_run_tables(run_id)
        self.store.update_run(
            run_id,
            fetch=False,
            total_rows=sum(int(table.get("total_rows") or 0) for table in tables),
            processed_rows=sum(int(table.get("processed_rows") or 0) for table in tables),
            total_bytes=sum(int(table.get("total_bytes") or 0) for table in tables),
            processed_bytes=sum(int(table.get("processed_bytes") or 0) for table in tables),
        )
        self.emit_run_event(run_id)

    def _extra_conditions(self, incremental_field: str, incremental_since: str) -> list[tuple[str, tuple[Any, ...]]]:
        if not incremental_field or not incremental_since:
            return []
        condition = mysql.incremental_condition(incremental_field, incremental_since)
        return [condition] if condition else []

    def _choose_pagination(
        self,
        *,
        table: str,
        requested_strategy: str,
        cursor_field: str,
        prod_column_names: list[str],
        nullable_source_columns: set[str],
        primary_keys: list[str],
        indexes: list[dict[str, Any]],
        row_count: int,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        manual_fields = self._parse_cursor_fields(cursor_field)
        if requested_strategy == "offset":
            if row_count > self.config.safety.max_rows_without_where:
                warnings.append(
                    "forced offset pagination may become slower as OFFSET grows; use auto or keyset pagination when possible"
                )
            return {
                "effective_strategy": "offset",
                "cursor_fields": [],
                "cursor_index": None,
                "cursor_unique": False,
                "warnings": warnings,
            }

        if manual_fields:
            missing = [field for field in manual_fields if field not in prod_column_names]
            if missing:
                raise SyncPlanError(f"Cursor field(s) do not exist in {table}: {', '.join(missing)}")

            selected_fields = list(manual_fields)
            if not self._cursor_tuple_is_unique(selected_fields, primary_keys, indexes, nullable_source_columns):
                unique_prefix = self._unique_index_with_prefix(indexes, selected_fields, prod_column_names, nullable_source_columns)
                if unique_prefix:
                    selected_fields = list(unique_prefix["columns"])
                    warnings.append(
                        "cursor field(s) expanded to unique index "
                        f"{unique_prefix['name']}: {self._format_cursor_fields(selected_fields)}"
                    )
                elif primary_keys:
                    appended = [field for field in primary_keys if field not in selected_fields]
                    selected_fields.extend(appended)
                    warnings.append(
                        "primary key appended to cursor for stable keyset pagination: "
                        + self._format_cursor_fields(selected_fields)
                    )
                else:
                    unique_tie_breaker = self._best_unique_index(indexes, prod_column_names, nullable_source_columns)
                    if unique_tie_breaker:
                        appended = [field for field in unique_tie_breaker["columns"] if field not in selected_fields]
                        selected_fields.extend(appended)
                        warnings.append(
                            "unique key appended to cursor for stable keyset pagination: "
                            + self._format_cursor_fields(selected_fields)
                        )

            cursor_unique = self._cursor_tuple_is_unique(
                selected_fields,
                primary_keys,
                indexes,
                nullable_source_columns,
            )
            matching_index = mysql.unique_index_for_columns(indexes, selected_fields) or self._index_with_prefix(
                indexes,
                selected_fields,
            )
            if not matching_index:
                prefix_index = self._index_with_prefix(indexes, manual_fields)
                if prefix_index:
                    warnings.append(
                        "cursor uses index prefix "
                        f"{prefix_index['name']}; add a composite index on "
                        f"({self._format_cursor_fields(selected_fields)}) for best speed"
                    )
                else:
                    warnings.append(
                        f"cursor field(s) {self._format_cursor_fields(selected_fields)} are not backed by a matching index; keyset pagination may be slow"
                    )
            if self._has_nullable_cursor_fields(selected_fields, nullable_source_columns):
                warnings.append(
                    f"cursor field(s) {self._format_cursor_fields(selected_fields)} include nullable columns; non-null indexed cursors are faster and easier to resume"
                )
            if not cursor_unique:
                warnings.append(
                    f"cursor field(s) {self._format_cursor_fields(selected_fields)} are not unique; duplicate values can cause skipped rows"
                )
            return {
                "effective_strategy": "cursor",
                "cursor_fields": selected_fields,
                "cursor_index": (matching_index or {}).get("name"),
                "cursor_unique": cursor_unique,
                "warnings": warnings,
            }

        if primary_keys:
            return {
                "effective_strategy": "cursor",
                "cursor_fields": primary_keys,
                "cursor_index": "PRIMARY",
                "cursor_unique": True,
                "warnings": warnings,
            }

        selected = self._best_unique_index(indexes, prod_column_names, nullable_source_columns)
        if selected:
            warnings.append(
                f"table has no primary key; using unique index {selected['name']} for keyset pagination"
            )
            return {
                "effective_strategy": "cursor",
                "cursor_fields": list(selected["columns"]),
                "cursor_index": selected["name"],
                "cursor_unique": True,
                "warnings": warnings,
            }

        if requested_strategy == "cursor":
            raise SyncPlanError(
                f"Table {table} has no primary key or non-null unique index; choose a stable unique cursor field for keyset pagination."
            )

        nullable_unique_indexes = [
            index
            for index in indexes
            if index.get("unique")
            and index.get("columns")
            and all(column in prod_column_names for column in index["columns"])
            and self._has_nullable_cursor_fields(list(index["columns"]), nullable_source_columns)
        ]
        if nullable_unique_indexes:
            warnings.append(
                "only nullable unique indexes are available; auto mode avoids them because MySQL allows duplicate NULL values"
            )
        warnings.append(
            "table has no primary key or unique index; falling back to offset pagination, which can slow down on large tables"
        )
        return {
            "effective_strategy": "offset",
            "cursor_fields": [],
            "cursor_index": None,
            "cursor_unique": False,
            "warnings": warnings,
        }

    def _best_unique_index(
        self,
        indexes: list[dict[str, Any]],
        prod_column_names: list[str],
        nullable_source_columns: set[str],
    ) -> dict[str, Any] | None:
        candidates = [
            index
            for index in indexes
            if index.get("unique")
            and index.get("columns")
            and all(column in prod_column_names for column in index["columns"])
            and not self._has_nullable_cursor_fields(list(index["columns"]), nullable_source_columns)
        ]
        candidates.sort(key=lambda item: (len(item["columns"]), 0 if item.get("primary") else 1, item["name"]))
        return candidates[0] if candidates else None

    def _unique_index_with_prefix(
        self,
        indexes: list[dict[str, Any]],
        fields: list[str],
        prod_column_names: list[str],
        nullable_source_columns: set[str],
    ) -> dict[str, Any] | None:
        for index in indexes:
            columns = list(index.get("columns") or [])
            if (
                index.get("unique")
                and columns[: len(fields)] == fields
                and all(column in prod_column_names for column in columns)
                and not self._has_nullable_cursor_fields(columns, nullable_source_columns)
            ):
                return index
        return None

    def _cursor_tuple_is_unique(
        self,
        fields: list[str],
        primary_keys: list[str],
        indexes: list[dict[str, Any]],
        nullable_source_columns: set[str],
    ) -> bool:
        field_set = set(fields)
        if primary_keys and set(primary_keys).issubset(field_set):
            return True
        for index in indexes:
            columns = list(index.get("columns") or [])
            if (
                index.get("unique")
                and columns
                and set(columns).issubset(field_set)
                and not self._has_nullable_cursor_fields(columns, nullable_source_columns)
            ):
                return True
        return False

    def _has_nullable_cursor_fields(self, fields: list[str], nullable_source_columns: set[str]) -> bool:
        return any(field in nullable_source_columns for field in fields)

    def _parse_cursor_fields(self, cursor_field: str | None) -> list[str]:
        fields = []
        for raw in str(cursor_field or "").replace("，", ",").split(","):
            field = raw.strip()
            if field:
                fields.append(mysql.validate_identifier(field))
        if len(fields) != len(set(fields)):
            raise SyncPlanError("Cursor fields must not contain duplicates.")
        return fields

    def _format_cursor_fields(self, fields: list[str]) -> str:
        return ",".join(fields)

    def _index_with_prefix(self, indexes: list[dict[str, Any]], fields: list[str]) -> dict[str, Any] | None:
        for index in indexes:
            columns = list(index.get("columns") or [])
            if columns[: len(fields)] == fields:
                return index
        return None

    def _encode_cursor_values(self, values: list[Any]) -> Any:
        if len(values) == 1:
            return None if values[0] is None else str(values[0])
        return json.dumps([None if value is None else str(value) for value in values], ensure_ascii=False)

    def _decode_cursor_values(self, raw: Any, field_count: int) -> list[Any] | None:
        if raw is None or raw == "":
            return None
        if field_count == 1:
            return [raw]
        if isinstance(raw, str):
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                values = [item.strip() for item in raw.split(",")]
        else:
            values = list(raw)
        if len(values) != field_count:
            return None
        return values

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
        strategy = str(strategy or "auto").strip().lower()
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
        if run["status"] not in {"failed", "queued", "paused"}:
            raise SyncPlanError(f"Run {run_id} cannot be resumed from status {run['status']}.")
        with self._lock:
            if run_id in self._submitted:
                raise SyncPlanError(f"Run {run_id} is still active. Wait for pause to finish before resuming.")
        self.engine.store.update_run(run_id, status="queued", error=None, finished_at=None)
        self.engine.log(run_id, "info", "Run queued for resume.")
        self._submit(run_id, resume=True)
        return self.get_run(run_id)

    def pause(self, run_id: str) -> dict[str, Any]:
        run = self.engine.store.get_run(run_id)
        if run["status"] == "paused":
            return self.get_run(run_id)
        if run["status"] not in {"queued", "running", "pause_requested"}:
            raise SyncPlanError(f"Run {run_id} cannot be paused from status {run['status']}.")
        next_status = "pause_requested"
        with self._lock:
            if run["status"] == "queued" and run_id not in self._submitted:
                next_status = "paused"
        self.engine.store.update_run(
            run_id,
            fetch=False,
            status=next_status,
            error=None,
            finished_at=utc_now() if next_status == "paused" else None,
        )
        self.engine.log(
            run_id,
            "info",
            "Pause requested. Sync will stop after the current batch is committed.",
        )
        if next_status == "paused":
            self.engine.log(run_id, "info", "Sync paused before execution started.")
        return self.get_run(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.engine.store.get_run(run_id)
        if run["status"] == "canceled":
            return self.get_run(run_id)
        if run["status"] not in {"queued", "running", "pause_requested", "paused", "cancel_requested"}:
            raise SyncPlanError(f"Run {run_id} cannot be canceled from status {run['status']}.")

        next_status = "cancel_requested"
        with self._lock:
            if run["status"] == "paused" or (run["status"] == "queued" and run_id not in self._submitted):
                next_status = "canceled"

        if next_status == "canceled":
            self.engine._finalize_canceled_run(run_id, run.get("current_table"))
        else:
            self.engine.store.update_run(
                run_id,
                fetch=False,
                status=next_status,
                error=None,
                finished_at=None,
            )
            self.engine.log(
                run_id,
                "warning",
                "Cancel requested. Sync will stop after the current batch is committed.",
            )
        return self.get_run(run_id)

    def start_job(self, job_id: int, *, source: str = "manual") -> dict[str, Any]:
        job = self.engine.store.get_job(job_id)
        payload = {
            "tables": job["tables"],
            "mode": job["mode"],
            "where_clause": job["where_clause"],
            "batch_size": job["batch_size"],
            "create_missing_tables": job["create_missing_tables"],
            "sync_strategy": job.get("sync_strategy", "auto"),
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

    def get_run(self, run_id: str, *, log_limit: int = 120) -> dict[str, Any]:
        run = self.engine.store.get_run(run_id)
        metrics = self._run_metrics(run)
        return {
            **run,
            **metrics,
            "tables_state": self.engine.store.get_run_tables(run_id),
            "shards_state": self.engine.store.get_run_shards(run_id),
            "logs": self.engine.store.get_logs(run_id, log_limit),
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
                end = datetime.now(timezone.utc)
                if run.get("status") in {"success", "failed", "paused", "canceled"} and run.get("finished_at"):
                    end = datetime.fromisoformat(run["finished_at"])
                elapsed_seconds = max(0.0, (end - start).total_seconds())
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
