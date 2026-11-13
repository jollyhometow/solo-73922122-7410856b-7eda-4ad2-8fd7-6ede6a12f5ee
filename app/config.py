"""运行配置，全部来自环境变量（12-factor）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    min_pool_size: int
    max_pool_size: int
    # 提交前可暂停的秒数上限（演示交错补录用；0 表示禁用暂停功能）。
    blocker_enabled: bool
    blocker_timeout_seconds: int
    max_upload_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_env(
                "DATABASE_URL",
                "postgresql://water:water@db:5432/water",
            ),
            min_pool_size=int(_env("DB_POOL_MIN", "2")),
            max_pool_size=int(_env("DB_POOL_MAX", "10")),
            blocker_enabled=_env("BLOCKER_ENABLED", "1") not in ("0", "false", "False", ""),
            blocker_timeout_seconds=int(_env("BLOCKER_TIMEOUT_SECONDS", "300")),
            max_upload_bytes=int(_env("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))),
        )


SETTINGS = Settings.from_env()
