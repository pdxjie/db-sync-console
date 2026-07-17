from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_config
from .store import SyncStore
from .sync import SyncEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Database prod-to-test sync console")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Show sync plan")
    _add_sync_args(plan_parser, dry_run_flag=False)

    sync_parser = subparsers.add_parser("sync", help="Run sync in foreground")
    _add_sync_args(sync_parser, dry_run_flag=True)

    resume_parser = subparsers.add_parser("resume", help="Resume a failed run")
    resume_parser.add_argument("run_id")

    serve_parser = subparsers.add_parser("serve", help="Start local web UI")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    if args.command == "serve":
        os.environ["DB_SYNC_CONFIG"] = str(config_path)
        import uvicorn

        uvicorn.run("sync_tool.app:app", host=args.host, port=args.port, reload=False)
        return 0

    config = load_config(config_path, require_exists=True)
    store = SyncStore(config.app.data_dir / "sync_console.db")
    store.init()
    engine = SyncEngine(config, store)

    if args.command == "plan":
        plan = engine.create_plan(
            tables=_parse_tables(args.tables),
            mode=args.mode,
            where_clause=args.where_clause,
            batch_size=args.batch_size,
            create_missing_tables=args.create_missing_tables,
            sync_strategy=args.sync_strategy,
            cursor_field=args.cursor_field,
            incremental_field=args.incremental_field,
            incremental_since=args.incremental_since,
            skip_exact_count=args.skip_exact_count,
            shard_count=args.shard_count,
            worker_count=args.worker_count,
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync":
        run = engine.prepare_run(
            {
                "tables": _parse_tables(args.tables),
                "mode": args.mode,
                "where_clause": args.where_clause,
                "batch_size": args.batch_size,
                "create_missing_tables": args.create_missing_tables,
                "sync_strategy": args.sync_strategy,
                "cursor_field": args.cursor_field,
                "incremental_field": args.incremental_field,
                "incremental_since": args.incremental_since,
                "skip_exact_count": args.skip_exact_count,
                "shard_count": args.shard_count,
                "worker_count": args.worker_count,
                "dry_run": args.dry_run,
                "name": args.name or "CLI sync",
            }
        )
        engine.execute_run(run["id"])
        result = store.get_run(run["id"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 1

    if args.command == "resume":
        engine.execute_run(args.run_id, resume=True)
        result = store.get_run(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 1

    return 1


def _add_sync_args(parser: argparse.ArgumentParser, *, dry_run_flag: bool) -> None:
    parser.add_argument("--tables", required=True, help="Comma separated table list, for example users,orders")
    parser.add_argument("--mode", choices=["replace", "upsert"], default="replace")
    parser.add_argument("--where", dest="where_clause", default="", help="Optional SQL condition without WHERE")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--sync-strategy", choices=["offset", "cursor"], default="offset")
    parser.add_argument("--cursor-field", default="", help="Cursor field for cursor strategy; defaults to first primary key")
    parser.add_argument("--incremental-field", default="", help="Incremental column, for example updated_at")
    parser.add_argument("--incremental-since", default="", help="Incremental lower bound, for example 2026-07-01 00:00:00")
    parser.add_argument("--skip-exact-count", action="store_true", help="Use estimated row count instead of COUNT(*)")
    parser.add_argument("--shard-count", type=int, default=1, help="Number of cursor shards")
    parser.add_argument("--worker-count", type=int, default=1, help="Concurrent workers for cursor shards")
    parser.add_argument(
        "--create-missing-tables",
        action="store_true",
        help="Create missing test tables from product table structure before syncing",
    )
    parser.add_argument("--name", default="")
    if dry_run_flag:
        parser.add_argument("--dry-run", action="store_true", help="Plan only; do not write to test")


def _parse_tables(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
