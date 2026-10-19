"""开发 / 演示期数据源 —— 读**进程内夹具**。

夹具由 `seed_order` / `seed_logistics` / `seed_profile` 注入，进程内有效、不落库。
零外部依赖、确定性好，因此也是**离线测试**的默认数据源。

⚠️ 夹具**只在 `DATA_SOURCE=local`（或未配置时推断为 local）下参与取数**。
生产若显式声明 `DATA_SOURCE=remote`，取数**绝不回落**到这里。
"""

from __future__ import annotations

from typing import Any

from app.adapters.mapping import to_logistics, to_order, to_profile
from app.models import BuyerProfile, LogisticsInfo, OrderSnapshot


class MockFixtures:
    """进程内夹具仓库（确定性、无外部依赖）。"""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.logistics: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}

    def seed_order(self, order_id: str, **facts: Any) -> None:
        """注入一条订单事实（合并写入，保留既有字段）。"""
        self.orders[order_id] = {**self.orders.get(order_id, {}), **facts}

    def seed_logistics(self, order_id: str, **facts: Any) -> None:
        """注入一条物流事实（合并写入）。"""
        self.logistics[order_id] = {**self.logistics.get(order_id, {}), **facts}

    def seed_profile(self, order_id: str, **facts: Any) -> None:
        """注入一条画像事实（以 order_id 为键，M1 简化处理）。"""
        self.profiles[order_id] = {**self.profiles.get(order_id, {}), **facts}

    def clear(self) -> None:
        """清空全部夹具（测试用）。"""
        self.orders.clear()
        self.logistics.clear()
        self.profiles.clear()


_fixtures = MockFixtures()


def get_fixtures() -> MockFixtures:
    """全局夹具实例 —— `seed_*` 与 `MockDataSource` **共用同一份**。"""
    return _fixtures


class MockDataSource:
    """本地夹具数据源：实现 `ports.DataSource`。"""

    async def fetch_order(self, order_id: str) -> OrderSnapshot | None:
        return to_order(order_id, _fixtures.orders.get(order_id))

    async def fetch_logistics(self, order_id: str) -> LogisticsInfo | None:
        return to_logistics(order_id, _fixtures.logistics.get(order_id))

    async def fetch_profile(self, order_id: str) -> BuyerProfile | None:
        return to_profile(order_id, _fixtures.profiles.get(order_id))
