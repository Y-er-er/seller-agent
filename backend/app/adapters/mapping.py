"""外部原始数据 → 领域模型的映射（**唯一一处**）。

为什么单独成模块：此前 `order` / `logistics` / `profile` 各自把"HTTP 分支"与"mock 分支"的
构造逻辑各写了一遍 —— 改一处忘另一处，两条通道产出的对象就会悄悄不一致。
现收敛到本模块：**无论数据来自真实 API 还是本地夹具，都走同一套映射**。

字段语义与默认值以 `docs/契约_外部数据源_v1.md` 为准。
"""

from __future__ import annotations

from typing import Any

from app.adapters.coerce import coerce_datetime, coerce_enum, coerce_float, coerce_int
from app.models import BuyerProfile, LogisticsInfo, LogisticsStatus, OrderSnapshot

#: 信用分缺失时的兜底值（与契约 §三 一致；规则 R4 门槛为 650）
_DEFAULT_CREDIT_SCORE = 600


def to_order(order_id: str, data: dict[str, Any] | None) -> OrderSnapshot | None:
    """构造订单快照；`data` 为空返回 `None`（契约 §一）。"""
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
        goods_no=str(data.get("goods_no") or ""),
    )


def to_logistics(order_id: str, data: dict[str, Any] | None) -> LogisticsInfo | None:
    """构造物流信息；`data` 为空返回 `None`（契约 §二）。"""
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


def to_profile(order_id: str, data: dict[str, Any] | None) -> BuyerProfile | None:
    """构造买家画像；`data` 为空返回 `None`（契约 §三）。"""
    if data is None:
        return None
    return BuyerProfile(
        buyer_id=str(data.get("buyer_id") or ""),
        buyer_credit_score=coerce_int(data.get("buyer_credit_score"), _DEFAULT_CREDIT_SCORE),
        buyer_refund_rate_90d=coerce_float(data.get("buyer_refund_rate_90d")),
        refund_count_90d=coerce_int(data.get("refund_count_90d")),
    )
