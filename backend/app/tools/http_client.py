"""外部只读 HTTP 客户端基础设施。

三条硬约束：
1. **只读**：仅暴露 GET，不提供任何写方法。
2. **绝不抛异常**：未配置 / 超时 / 非 2xx / 解析失败，统一返回 `None`。
   调用方据此交由规则引擎判为"数据不足 → 转人工"，**不让网络异常中断判定链路**。
3. **背压**：全局信号量限制并发，对齐外部 API 配额（方案 §10）。

说明：本模块只负责"怎么取"，不负责"字段怎么映射"——映射在各 service 客户端内。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

import httpx

# 并发上限：同时进行的外部只读请求数（方案 §10 背压）
_MAX_CONCURRENCY = 10

_DEFAULT_TIMEOUT = 3.0

_semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

# 进程内复用同一连接池，避免每次请求重建 TCP 连接
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """惰性创建并复用全局 HTTP 客户端。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _client


async def aclose() -> None:
    """关闭全局客户端（测试与进程退出时调用，避免连接泄漏）。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_json(
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """发起只读 GET 并返回 JSON 对象。

    返回 None 表示不可用（未配置 / 网络失败 / 非 2xx / 响应不是 JSON 对象）。
    """
    if not base_url:
        return None

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    client = _get_client()

    # 信号量限流：超出并发上限时在此排队
    async with _semaphore:
        try:
            response = await client.get(url, params=params, timeout=timeout or _DEFAULT_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            # 任何异常都不上抛：判定链路的健壮性优先于错误传播
            return None

    return payload if isinstance(payload, dict) else None


_E = TypeVar("_E", bound=Enum)


def coerce_enum(enum_cls: type[_E], value: Any, default: _E) -> _E:
    """把外部返回值安全转换为枚举；非法值回落到默认值（不抛 ValueError）。"""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def coerce_float(value: Any, default: float = 0.0) -> float:
    """安全转 float；None/非法值回落默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: Any, default: int = 0) -> int:
    """安全转 int；None/非法值回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_datetime(value: Any) -> datetime | None:
    """把外部返回值安全转为 datetime；None/空/非法值返回 None（不抛异常）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
