"""MySQL 连接工具。

三条约束：
1. **连接串来自 `.env`**（纪律 9），代码中不出现任何主机/账号/口令；
2. **不阻塞事件循环**：pymysql 是同步驱动，统一通过 `run_sync()` 丢到线程池执行；
3. **失败可降级**：本层只负责连接与执行，异常交由调用方决定是否降级
   （审计场景下，写库失败绝不可阻断判定链路）。

关于连接复用：当前按"每次操作一个连接"实现。审计写入是低频动作（每笔申请一行），
此实现最稳妥；若后续写入量上升，可替换为连接池而不影响调用方。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse

import pymysql

from app.config import settings


def parse_mysql_uri(uri: str) -> dict[str, Any]:
    """解析 `mysql+pymysql://user:pwd@host:port/db` 形式的连接串。

    兼容没有 scheme 的简写；缺失端口回落 3306。
    """
    raw = uri if "://" in uri else f"mysql://{uri}"
    parsed = urlparse(raw.replace("mysql+pymysql://", "mysql://", 1))
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "").lstrip("/"),
    }


@contextmanager
def connect() -> Iterator[pymysql.connections.Connection]:
    """打开一次性 MySQL 连接（自动提交）。未配置连接串时立刻抛出。

    ⚠️ 为何 `connect_timeout` 放宽到 15s（本机实测结论）：
    数据库跑在虚拟机内的 Docker 中，MySQL 会对客户端 IP 做反向解析；
    实测同一连接既有 0.05s 完成的、也有超过 5s 未完成握手的（网络层 ping 与
    端口探测均正常，说明不是网络不通）。5s 会把它误判为"数据库不可用"，
    故放宽到 15s；若长期受此影响，建议在 MySQL 侧开启 `skip-name-resolve`。
    """
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，无法建立数据库连接")

    conn = pymysql.connect(
        connect_timeout=15,
        read_timeout=10,
        write_timeout=10,
        charset="utf8mb4",
        autocommit=True,
        **parse_mysql_uri(settings.mysql_uri),
    )
    try:
        yield conn
    finally:
        conn.close()


async def run_sync(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在线程池中执行同步 DB 操作，避免阻塞 asyncio 事件循环。"""
    return await asyncio.to_thread(func, *args, **kwargs)
