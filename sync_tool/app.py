from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import mysql
from .config import MySQLConfig, load_config
from .scheduler import JobScheduler
from .store import SyncStore
from .sync import SyncEngine, SyncManager, SyncPlanError


class SyncPayload(BaseModel):
    tables: list[str] = Field(default_factory=list)
    mode: Literal["replace", "upsert"] = "replace"
    where_clause: str = ""
    batch_size: int | None = None
    create_missing_tables: bool = False
    sync_strategy: Literal["offset", "cursor"] = "offset"
    cursor_field: str = ""
    incremental_field: str = ""
    incremental_since: str = ""
    skip_exact_count: bool = False
    shard_count: int = 1
    worker_count: int = 1
    dry_run: bool = False
    name: str | None = None


class JobPayload(BaseModel):
    name: str
    tables: list[str] = Field(default_factory=list)
    mode: Literal["replace", "upsert"] = "replace"
    where_clause: str = ""
    batch_size: int | None = None
    create_missing_tables: bool = False
    sync_strategy: Literal["offset", "cursor"] = "offset"
    cursor_field: str = ""
    incremental_field: str = ""
    incremental_since: str = ""
    skip_exact_count: bool = False
    shard_count: int = 1
    worker_count: int = 1
    schedule_enabled: bool = False
    cron_expr: str = ""


class ConnectionPayload(BaseModel):
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str | None = None
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 120
    write_timeout: int = 120


class ConnectionsPayload(BaseModel):
    prod: ConnectionPayload
    test: ConnectionPayload


class ConnectionTestPayload(BaseModel):
    env: Literal["prod", "test"]
    connection: ConnectionPayload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
CONFIG_PATH = Path(os.getenv("DB_SYNC_CONFIG") or os.getenv("MYSQL_SYNC_CONFIG") or PROJECT_ROOT / "config.json").expanduser().resolve()

base_config = load_config(CONFIG_PATH, require_exists=False)
store = SyncStore(base_config.app.data_dir / "sync_console.db")
engine = SyncEngine(lambda: effective_config(), store)
manager = SyncManager(engine)
scheduler = JobScheduler(base_config, store, manager)

app = FastAPI(title="同步犬", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


@app.on_event("startup")
def on_startup() -> None:
    store.init()
    store.mark_interrupted_runs()
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.shutdown()
    manager.shutdown(wait=False)


@app.get("/")
def index():
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/api/status")
def get_status():
    current_config = effective_config()
    return {
        "config": current_config.redacted(),
        "connections": _connection_snapshot(current_config),
        "connection_ready": _connection_ready(current_config),
        "scheduler": scheduler.snapshot(),
    }


@app.get("/api/connections")
def get_connections():
    current_config = effective_config()
    return {"connections": _connection_snapshot(current_config), "connection_ready": _connection_ready(current_config)}


@app.post("/api/connections")
def save_connections(payload: ConnectionsPayload):
    prod = store.save_connection("prod", _connection_dict(payload.prod))
    test = store.save_connection("test", _connection_dict(payload.test))
    return {"connections": {"prod": prod, "test": test}, "connection_ready": _connection_ready(effective_config())}


@app.post("/api/connections/login")
def login_connections(payload: ConnectionsPayload):
    prod_payload, prod_config = _connection_config("prod", payload.prod)
    test_payload, test_config = _connection_config("test", payload.test)
    prod_result = _call(lambda: mysql.test_connection(prod_config))
    test_result = _call(lambda: mysql.test_connection(test_config))
    prod = store.save_connection("prod", prod_payload)
    test = store.save_connection("test", test_payload)
    return {
        "ok": True,
        "connections": {"prod": prod, "test": test},
        "tests": {"prod": prod_result, "test": test_result},
        "connection_ready": _connection_ready(effective_config()),
    }


@app.post("/api/connections/test")
def test_connection(payload: ConnectionTestPayload):
    _, config = _connection_config(payload.env, payload.connection)
    result = _call(lambda: mysql.test_connection(config))
    return {"ok": True, "env": payload.env, "result": result}


@app.get("/api/tables")
def get_tables(q: str = ""):
    return {"tables": _call(lambda: engine.list_source_tables(q))}


@app.post("/api/plan")
def create_plan(payload: SyncPayload):
    return _call(
        lambda: engine.create_plan(
            tables=payload.tables,
            mode=payload.mode,
            where_clause=payload.where_clause,
            batch_size=payload.batch_size,
            create_missing_tables=payload.create_missing_tables,
            sync_strategy=payload.sync_strategy,
            cursor_field=payload.cursor_field,
            incremental_field=payload.incremental_field,
            incremental_since=payload.incremental_since,
            skip_exact_count=payload.skip_exact_count,
            shard_count=payload.shard_count,
            worker_count=payload.worker_count,
        )
    )


@app.post("/api/runs")
def create_run(payload: SyncPayload):
    return _call(lambda: manager.start(_payload_dict(payload)))


@app.get("/api/runs")
def list_runs(limit: int = 30):
    return {"runs": store.list_runs(limit)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return _call(lambda: manager.get_run(run_id))


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str):
    return _call(lambda: manager.resume(run_id))


@app.get("/api/runs/{run_id}/logs")
def get_logs(run_id: str, limit: int = 300):
    return {"logs": store.get_logs(run_id, limit)}


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": store.list_jobs(), "scheduler": scheduler.snapshot()}


@app.post("/api/jobs")
def create_job(payload: JobPayload):
    data = _job_dict(payload)
    _validate_schedule(data)
    _call(
        lambda: engine.create_plan(
            tables=data["tables"],
            mode=data["mode"],
            where_clause=data["where_clause"],
            batch_size=data["batch_size"],
            create_missing_tables=data["create_missing_tables"],
            sync_strategy=data["sync_strategy"],
            cursor_field=data["cursor_field"],
            incremental_field=data["incremental_field"],
            incremental_since=data["incremental_since"],
            skip_exact_count=data["skip_exact_count"],
            shard_count=data["shard_count"],
            worker_count=data["worker_count"],
        )
    )
    job = store.create_job(data)
    scheduler.reload()
    return job


@app.put("/api/jobs/{job_id}")
def update_job(job_id: int, payload: JobPayload):
    data = _job_dict(payload)
    _validate_schedule(data)
    _call(
        lambda: engine.create_plan(
            tables=data["tables"],
            mode=data["mode"],
            where_clause=data["where_clause"],
            batch_size=data["batch_size"],
            create_missing_tables=data["create_missing_tables"],
            sync_strategy=data["sync_strategy"],
            cursor_field=data["cursor_field"],
            incremental_field=data["incremental_field"],
            incremental_since=data["incremental_since"],
            skip_exact_count=data["skip_exact_count"],
            shard_count=data["shard_count"],
            worker_count=data["worker_count"],
        )
    )
    try:
        job = store.update_job(job_id, data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    scheduler.reload()
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    store.delete_job(job_id)
    scheduler.reload()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: int):
    return _call(lambda: manager.start_job(job_id))


def _payload_dict(payload: SyncPayload) -> dict:
    return _model_dump(payload)


def _job_dict(payload: JobPayload) -> dict:
    data = _model_dump(payload)
    if data["batch_size"] is None:
        data["batch_size"] = base_config.app.page_size
    return data


def _connection_dict(payload: ConnectionPayload) -> dict:
    return _model_dump(payload)


def _connection_config(env: str, payload: ConnectionPayload) -> tuple[dict, MySQLConfig]:
    raw = store.build_connection_payload(env, _connection_dict(payload))
    config = MySQLConfig.from_dict(raw)
    missing = []
    if not config.host:
        missing.append("host")
    if not config.user:
        missing.append("user")
    if not config.database:
        missing.append("database")
    if missing:
        raise HTTPException(status_code=400, detail=f"{env} connection missing: {', '.join(missing)}")
    _validate_connection_host(env, config.host)
    return raw, config


def _validate_connection_host(env: str, host: str) -> None:
    if host.endswith(".comz"):
        suggestion = f"{host[:-1]}"
        raise HTTPException(
            status_code=400,
            detail=f"{env} host looks invalid: {host}. Did you mean {suggestion}?",
        )
    if "://" in host or "/" in host:
        raise HTTPException(
            status_code=400,
            detail=f"{env} host should be a hostname only, not a URL: {host}",
        )
    if ":" in host:
        raise HTTPException(
            status_code=400,
            detail=f"{env} host should not include a port. Put the port in the Port field instead: {host}",
        )


def _model_dump(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _validate_schedule(data: dict) -> None:
    if not data.get("schedule_enabled"):
        return
    if not data.get("cron_expr"):
        raise HTTPException(status_code=400, detail="cron_expr is required when schedule is enabled.")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(data["cron_expr"], timezone=base_config.app.timezone)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {exc}") from exc


def effective_config():
    return base_config.with_databases(
        prod=store.mysql_config("prod") or base_config.prod,
        test=store.mysql_config("test") or base_config.test,
    )


def _connection_ready(current_config) -> bool:
    return all(
        db.host and db.user and db.database
        for db in (current_config.prod, current_config.test)
    )


def _connection_snapshot(current_config) -> dict:
    saved = store.list_connections()
    return {
        "prod": saved["prod"] or current_config.prod.redacted(),
        "test": saved["test"] or current_config.test.redacted(),
    }


def _call(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SyncPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
