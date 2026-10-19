"""物流服务客户端（**只读**）—— 已退化为**薄封装**。

取数实现按 `DATA_SOURCE` 在 `app/ports` 里选择（本地夹具 / 真实 HTTP）。
契约见 `docs/契约_外部数据源_v1.md` §二。
"""

from __future__ import annotations

from typing import Any

from app.adapters.datasource_mock import get_fixtures
from app.models import LogisticsInfo
from app.ports import get_datasource


def seed_logistics(order_id: str, **facts: Any) -> None:
    """测试/演示用：注入一条物流事实（写入本地夹具）。"""
    get_fixtures().seed_logistics(order_id, **facts)


async def fetch_logistics(order_id: str) -> LogisticsInfo | None:
    """拉取物流信息。取不到返回 `None`。"""
    return await get_datasource().fetch_logistics(order_id)
