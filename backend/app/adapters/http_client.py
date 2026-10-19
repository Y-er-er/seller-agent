"""外部只读 HTTP 客户端基础设施（**适配层**）。

放在 adapters 而不是 tools：本模块是**传输层**实现，与具体业务无关；
若留在 tools 包内，`adapters → tools` 的 import 会触发 tools 包初始化，
而 tools 各模块又依赖 ports，形成循环导入（实测踩到）。

三条硬约束：
1. **只读**：仅暴露 GET，不提供任何写方法。
2. **绝不抛异常**：未配置 / 超时 / 非 2xx / 解析失败，统一返回 `None`。
   调用方据此交由规则引擎判为"数据不足 → 转人工"，**不让网络异常中断判定链路**。
3. **背压**：全局信号量限制并发，对齐外部 API 配额（方案 §10）。

说明：本模块只负责"怎么取"（传输层），不负责"字段怎么映射"。
      映射与外部值转换分别在 `app/adapters/mapping.py` 与 `app/adapters/coerce.py`
      （原先的 `coerce_*` 已移出本模块：它们属于适配层，留在这里会造成 adapters→tools 的反向依赖）。
"""

from __future__ import annotations

import asyncio
from typing import Any

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
