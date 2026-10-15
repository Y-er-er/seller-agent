"""买家画像客户端（**只读**）。

取数策略同订单服务：配置了 `AFTERSALE_API_BASE` 走真实 HTTP，否则降级内存 mock。

**期望响应契约**（⚠️ 待与真实接口核对）：
    GET {AFTERSALE_API_BASE}/profiles/{order_id}
    {
      "buyer_id": "B001",
      "buyer_credit_score": 700,
      "buyer_refund_rate_90d": 0.05,
      "refund_count_90d": 1
    }

M1 简化说明：当前以 `order_id` 作为查询键（沿用 mock 约定）。
若真实业务提供 `buyer_id`，应在 `RefundRequest` 中补字段后改为按买家查询。
"""

from __future__ import annotations

from app.config import settings
from app.models import BuyerProfile
from app.tools.http_client import coerce_float, coerce_int, get_json

_MOCK: dict[str, dict] = {}


def seed_profile(order_id: str, **facts) -> None:
    """测试/演示用：注入一条画像事实（以 order_id 为键，M1 简化处理）。"""
    _MOCK[order_id] = {**_MOCK.get(order_id, {}), **facts}


async def fetch_profile(order_id: str) -> BuyerProfile | None:
    """拉取买家画像。取不到返回 None（规则侧有默认值兜底）。"""
    if settings.aftersale_api_base:
        data = await get_json(settings.aftersale_api_base, f"/profiles/{order_id}")
        if data is None:
            return None
        return BuyerProfile(
            buyer_id=str(data.get("buyer_id") or ""),
            buyer_credit_score=coerce_int(data.get("buyer_credit_score"), 600),
            buyer_refund_rate_90d=coerce_float(data.get("buyer_refund_rate_90d")),
            refund_count_90d=coerce_int(data.get("refund_count_90d")),
        )

    row = _MOCK.get(order_id)
    if row is None:
        return None
    return BuyerProfile(
        buyer_id=row.get("buyer_id", ""),
        buyer_credit_score=coerce_int(row.get("buyer_credit_score"), 600),
        buyer_refund_rate_90d=coerce_float(row.get("buyer_refund_rate_90d")),
        refund_count_90d=coerce_int(row.get("refund_count_90d")),
    )
