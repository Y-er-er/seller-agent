"""节点 2：取数（fetch）—— 三个只读事实源，**并行 fan-out**。

三个节点写入三个互不相同的 state 字段，天然无写冲突，无需 reducer。
任一路取不到数据都不抛异常：置为 None，交由 R8"数据不足"规则转人工。
"""

from __future__ import annotations

from app.graph.state import RefundState
from app.models import AuditEvent
from app.tools import fetch_logistics, fetch_order, fetch_profile


def _order_id(state: RefundState) -> str:
    request = state.get("request")
    return request.order_id if request else ""


async def fetch_order_node(state: RefundState) -> dict:
    """取订单快照。"""
    order = await fetch_order(_order_id(state))
    return {
        "order": order,
        "audit_events": [
            AuditEvent(stage="fetch_order", message="订单取数" + ("成功" if order else "失败"))
        ],
    }


async def fetch_logistics_node(state: RefundState) -> dict:
    """取物流信息。"""
    logistics = await fetch_logistics(_order_id(state))
    return {
        "logistics": logistics,
        "audit_events": [
            AuditEvent(
                stage="fetch_logistics", message="物流取数" + ("成功" if logistics else "失败")
            )
        ],
    }


async def fetch_profile_node(state: RefundState) -> dict:
    """取买家画像。"""
    profile = await fetch_profile(_order_id(state))
    return {
        "profile": profile,
        "audit_events": [
            AuditEvent(stage="fetch_profile", message="画像取数" + ("成功" if profile else "失败"))
        ],
    }
