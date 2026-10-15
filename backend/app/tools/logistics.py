"""物流服务客户端（**只读**）。

取数策略同订单服务：配置了 `LOGISTICS_API_BASE` 走真实 HTTP，否则降级内存 mock。

**期望响应契约**（⚠️ 待与真实接口核对）：
    GET {LOGISTICS_API_BASE}/logistics/{order_id}
    {
      "order_id": "O202610130001",
      "logistics_status": "not_signed" | "in_transit" | "signed",
      "traces": ["已揽收", "到达派送点"]
    }
"""

from __future__ import annotations

from app.config import settings
from app.models import LogisticsInfo, LogisticsStatus
from app.tools.http_client import coerce_datetime, coerce_enum, get_json

_MOCK: dict[str, dict] = {}


def seed_logistics(order_id: str, **facts) -> None:
    """测试/演示用：注入一条物流事实。"""
    _MOCK[order_id] = {**_MOCK.get(order_id, {}), **facts}


async def fetch_logistics(order_id: str) -> LogisticsInfo | None:
    """拉取物流信息。取不到返回 None。"""
    if settings.logistics_api_base:
        data = await get_json(settings.logistics_api_base, f"/logistics/{order_id}")
        if data is None:
            return None
        return LogisticsInfo(
            order_id=order_id,
            logistics_status=coerce_enum(
                LogisticsStatus, data.get("logistics_status"), LogisticsStatus.IN_TRANSIT
            ),
            signed_at=coerce_datetime(data.get("signed_at")),
            traces=[str(t) for t in (data.get("traces") or [])],
        )

    row = _MOCK.get(order_id)
    if row is None:
        return None
    return LogisticsInfo(
        order_id=order_id,
        logistics_status=coerce_enum(
            LogisticsStatus, row.get("logistics_status"), LogisticsStatus.IN_TRANSIT
        ),
        signed_at=coerce_datetime(row.get("signed_at")),
        traces=[str(t) for t in (row.get("traces") or [])],
    )
