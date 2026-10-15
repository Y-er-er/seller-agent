"""订单服务客户端（**只读**）。

取数策略（两级降级）：
1. `.env` 配置了 `ORDER_API_BASE` → 走真实只读 HTTP；
2. 未配置 → 降级为本地内存 mock（由 `seed_order()` 注入，仅供开发/测试）。

**期望响应契约**（⚠️ 待与真实接口核对，映射层需按实际字段调整）：
    GET {ORDER_API_BASE}/orders/{order_id}
    {
      "order_id": "O202610130001",
      "item_category": "日用百货",
      "pay_amount": 39.0,
      "logistics_status": "not_signed" | "in_transit" | "signed"
    }

任一环节失败（未配置/超时/非 2xx/字段非法）→ 返回 None，
交由规则 R8「数据不足 → 转人工」处理，不在网络层抛异常。
"""

from __future__ import annotations

from app.config import settings
from app.models import LogisticsStatus, OrderSnapshot
from app.tools.http_client import coerce_datetime, coerce_enum, coerce_float, get_json

# order_id -> 订单事实（仅本地降级路径使用）
_MOCK: dict[str, dict] = {}


def seed_order(order_id: str, **facts) -> None:
    """测试/演示用：注入一条订单事实（不落库、进程内有效）。"""
    _MOCK[order_id] = {**_MOCK.get(order_id, {}), **facts}


async def fetch_order(order_id: str) -> OrderSnapshot | None:
    """拉取订单快照。取不到返回 None。"""
    if settings.order_api_base:
        data = await get_json(settings.order_api_base, f"/orders/{order_id}")
        if data is None:
            return None
        return OrderSnapshot(
            order_id=order_id,
            item_category=str(data.get("item_category") or ""),
            pay_amount=coerce_float(data.get("pay_amount")),
            signed_at=coerce_datetime(data.get("signed_at")),
            logistics_status=coerce_enum(
                LogisticsStatus, data.get("logistics_status"), LogisticsStatus.IN_TRANSIT
            ),
        )

    row = _MOCK.get(order_id)
    if row is None:
        return None
    return OrderSnapshot(
        order_id=order_id,
        item_category=row.get("item_category", ""),
        pay_amount=coerce_float(row.get("pay_amount")),
        signed_at=coerce_datetime(row.get("signed_at")),
        logistics_status=coerce_enum(
            LogisticsStatus, row.get("logistics_status"), LogisticsStatus.IN_TRANSIT
        ),
    )
