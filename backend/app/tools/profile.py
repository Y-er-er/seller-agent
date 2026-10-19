"""买家画像客户端（**只读**）—— 已退化为**薄封装**。

取数实现按 `DATA_SOURCE` 在 `app/ports` 里选择（本地夹具 / 真实 HTTP）。
契约见 `docs/契约_外部数据源_v1.md` §三。

M1 简化说明：当前以 `order_id` 作为查询键（沿用夹具约定）。
若真实业务提供 `buyer_id`，应在 `RefundRequest` 中补字段后改为按买家查询。
"""

from __future__ import annotations

from typing import Any

from app.adapters.datasource_mock import get_fixtures
from app.models import BuyerProfile
from app.ports import get_datasource


def seed_profile(order_id: str, **facts: Any) -> None:
    """测试/演示用：注入一条画像事实（以 order_id 为键，M1 简化处理）。"""
    get_fixtures().seed_profile(order_id, **facts)


async def fetch_profile(order_id: str) -> BuyerProfile | None:
    """拉取买家画像。取不到返回 `None`（规则侧有默认值兜底）。"""
    return await get_datasource().fetch_profile(order_id)
