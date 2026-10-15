"""节点 5：风险标记（risk）。确定性计算，不调用模型。"""

from __future__ import annotations

from app.graph.state import RefundState
from app.models import AuditEvent, RiskReport

# 等级排序（用于取较高者）
_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def risk_node(state: RefundState) -> dict:
    """依据 90 天退款率与仅退款笔数计算风险等级。"""
    profile = state.get("profile")

    rate = profile.buyer_refund_rate_90d if profile else 0.0
    count = profile.refund_count_90d if profile else 0

    flags: list[str] = []
    level = "low"

    if rate >= 0.30:
        flags.append("refund_rate_high")
        level = "high"
    elif rate >= 0.15:
        flags.append("refund_rate_watch")
        level = "medium"

    if count >= 6:
        flags.append("refund_count_high")
        level = "high"
    elif count >= 3:
        flags.append("refund_count_watch")
        if _LEVEL_ORDER[level] < _LEVEL_ORDER["medium"]:
            level = "medium"

    report = RiskReport(level=level, flags=flags)
    return {
        "risk": report,
        "audit_events": [
            AuditEvent(stage="risk", message=f"风险等级：{level}", payload={"flags": flags})
        ],
    }
