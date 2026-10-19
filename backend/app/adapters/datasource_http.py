"""生产期数据源 —— 真实只读 HTTP。

⚠️ 与 mock 实现的**关键差异**：本实现**绝不回落到本地夹具**。

为什么这一点必须写死：若在"已声明 remote 但忘了配 base_url"时静默回落，
就会出现"以为在用真实数据、其实在读演示数据"——这是最难排查的一类事故。
本实现改为**明确失败 + 记 error 日志**，让问题在第一次取数时就暴露。
"""

from __future__ import annotations

import logging

from app.adapters.mapping import to_logistics, to_order, to_profile
from app.config import settings
from app.models import BuyerProfile, LogisticsInfo, OrderSnapshot
from app.adapters.http_client import get_json

logger = logging.getLogger(__name__)

#: 同一项配置缺失只报一次，避免刷屏（一次请求会连打三次取数）
_warned: set[str] = set()


def _warn_missing(api_name: str) -> None:
    """数据源模式为 remote，但某项 base_url 未配置 —— 报一次 error。"""
    if api_name in _warned:
        return
    _warned.add(api_name)
    logger.error(
        "数据源=remote 但 %s 未配置：该项取数一律返回 None（**不会回落到本地夹具**）", api_name
    )


def reset_warnings() -> None:
    """清空"已告警"标记（测试用）。"""
    _warned.clear()


class HttpDataSource:
    """真实只读 HTTP 数据源：实现 `ports.DataSource`。契约见 docs/契约_外部数据源_v1.md。"""

    async def fetch_order(self, order_id: str) -> OrderSnapshot | None:
        if not settings.order_api_base:
            _warn_missing("ORDER_API_BASE")
            return None
        data = await get_json(settings.order_api_base, f"/orders/{order_id}")
        return to_order(order_id, data)

    async def fetch_logistics(self, order_id: str) -> LogisticsInfo | None:
        if not settings.logistics_api_base:
            _warn_missing("LOGISTICS_API_BASE")
            return None
        data = await get_json(settings.logistics_api_base, f"/logistics/{order_id}")
        return to_logistics(order_id, data)

    async def fetch_profile(self, order_id: str) -> BuyerProfile | None:
        if not settings.aftersale_api_base:
            _warn_missing("AFTERSALE_API_BASE")
            return None
        data = await get_json(settings.aftersale_api_base, f"/profiles/{order_id}")
        return to_profile(order_id, data)
