from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MySQLConfig:
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 120
    write_timeout: int = 120

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "MySQLConfig":
        raw = raw or {}
        return cls(
            host=str(raw.get("host", "")).strip(),
            port=int(raw.get("port", 3306)),
            user=str(raw.get("user", "")).strip(),
            password=str(raw.get("password", "")),
            database=str(raw.get("database", "")).strip(),
            charset=str(raw.get("charset", "utf8mb4")).strip(),
            connect_timeout=int(raw.get("connect_timeout", 10)),
            read_timeout=int(raw.get("read_timeout", 120)),
            write_timeout=int(raw.get("write_timeout", 120)),
        )

    def to_pymysql_args(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "write_timeout": self.write_timeout,
        }

    def fingerprint(self) -> tuple[str, int, str]:
        return (self.host.lower(), self.port, self.database.lower())

    def redacted(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "charset": self.charset,
            "password_set": bool(self.password),
        }


@dataclass(frozen=True)
class AppConfig:
    page_size: int = 1000
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    timezone: str = "Asia/Shanghai"
    strict_schema: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, base_dir: Path) -> "AppConfig":
        raw = raw or {}
        data_dir = Path(str(os.getenv("DB_SYNC_DATA_DIR") or raw.get("data_dir", "./data")))
        log_dir = Path(str(os.getenv("DB_SYNC_LOG_DIR") or raw.get("log_dir", "./logs")))
        if not data_dir.is_absolute():
            data_dir = base_dir / data_dir
        if not log_dir.is_absolute():
            log_dir = base_dir / log_dir
        return cls(
            page_size=int(raw.get("page_size", 1000)),
            data_dir=data_dir,
            log_dir=log_dir,
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            strict_schema=bool(raw.get("strict_schema", False)),
        )


@dataclass(frozen=True)
class SafetyConfig:
    blocked_tables: set[str] = field(default_factory=set)
    max_rows_without_where: int = 1_000_000

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SafetyConfig":
        raw = raw or {}
        blocked = {str(item) for item in raw.get("blocked_tables", [])}
        return cls(
            blocked_tables=blocked,
            max_rows_without_where=int(raw.get("max_rows_without_where", 1_000_000)),
        )


@dataclass(frozen=True)
class Config:
    prod: MySQLConfig
    test: MySQLConfig
    app: AppConfig
    safety: SafetyConfig
    path: Path
    exists: bool

    def with_databases(
        self,
        *,
        prod: MySQLConfig | None = None,
        test: MySQLConfig | None = None,
    ) -> "Config":
        return Config(
            prod=prod or self.prod,
            test=test or self.test,
            app=self.app,
            safety=self.safety,
            path=self.path,
            exists=self.exists,
        )

    def redacted(self) -> dict[str, Any]:
        return {
            "config_path": str(self.path),
            "config_exists": self.exists,
            "prod": self.prod.redacted(),
            "test": self.test.redacted(),
            "app": {
                "page_size": self.app.page_size,
                "data_dir": str(self.app.data_dir),
                "log_dir": str(self.app.log_dir),
                "timezone": self.app.timezone,
                "strict_schema": self.app.strict_schema,
            },
            "safety": {
                "blocked_tables": sorted(self.safety.blocked_tables),
                "max_rows_without_where": self.safety.max_rows_without_where,
            },
        }


def default_config_path() -> Path:
    env_path = os.getenv("DB_SYNC_CONFIG") or os.getenv("MYSQL_SYNC_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path.cwd() / "config.json").resolve()


def load_config(path: str | Path | None = None, *, require_exists: bool = True) -> Config:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    exists = config_path.exists()
    if not exists:
        if require_exists:
            raise FileNotFoundError(
                f"Config file not found: {config_path}. Copy config.example.json to config.json first."
            )
        raw: dict[str, Any] = {}
    else:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

    base_dir = config_path.parent if config_path.parent else Path.cwd()
    return Config(
        prod=MySQLConfig.from_dict(raw.get("prod")),
        test=MySQLConfig.from_dict(raw.get("test")),
        app=AppConfig.from_dict(raw.get("app"), base_dir),
        safety=SafetyConfig.from_dict(raw.get("safety")),
        path=config_path,
        exists=exists,
    )
