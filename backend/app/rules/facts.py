"""事实装配：把图状态投影为规则引擎所需的**扁平事实字典**。

为什么要单独一层：
- 规则表只认扁平字段（`claim_amount`、`logistics_status`…），不感知 Pydantic 模型结构；
- 事实契约集中在本文件，规则表与图状态解耦，改状态不必改规则。

契约（规则表可引用的全部字段）：
    order_id, item_category, pay_amount, claim_amount,
    refund_reason_code, logistics_status, signed_at,
    buyer_credit_score, buyer_refund_rate_90d, refund_count_90d,
    image_count, decodable, has_quality_claim,
    has_order, has_logistics, has_profile
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.models import RefundReason, RefundRequest
from app.graph.state import RefundState

# 事实契约：规则表**只允许**引用下列字段。
# 由 loader 在**加载期**校验——否则规则里写错字段名（如 claim_amounts）会
# 导致该条件永远为 False，且不报任何错，属于最难排查的一类缺陷。
FACT_FIELDS: frozenset[str] = frozenset(
    {
        "order_id",
        "item_category",
        "pay_amount",
        "claim_amount",
        "claim_intent",
        "refund_reason_code",
        "logistics_status",
        "signed_at",
        "days_since_signed",
        "buyer_credit_score",
        "buyer_refund_rate_90d",
        "refund_count_90d",
        "image_count",
        "decodable",
        "has_quality_claim",
        "has_order",
        "has_logistics",
        "has_profile",
    }
)


def _enum_value(value: Any) -> Any:
    """枚举取 .value，便于 YAML 中直接用字符串比较。"""
    return getattr(value, "value", value)


def build_facts(state: RefundState) -> dict[str, Any]:
    """从图状态装配事实字典。缺失的关联对象以 has_* = False 显式表达。"""
    request: RefundRequest | None = state.get("request")
    order = state.get("order")
    logistics = state.get("logistics")
    profile = state.get("profile")
    evidence = state.get("evidence")

    facts: dict[str, Any] = {
        # ---- 存在性（供 R8 之类"数据不足"规则使用）----
        "has_order": order is not None,
        "has_logistics": logistics is not None,
        "has_profile": profile is not None,
    }

    if request is not None:
        facts.update(
            order_id=request.order_id,
            item_category=request.item_category,
            pay_amount=request.pay_amount,
            claim_amount=request.claim_amount,
            claim_intent=_enum_value(request.claim_type),
            refund_reason_code=_enum_value(request.refund_reason_code),
        )

    if order is not None:
        facts["logistics_status"] = _enum_value(order.logistics_status)
        facts["signed_at"] = order.signed_at

    # 物流信息更新时，以物流侧状态为准（更贴近事实）
    if logistics is not None:
        facts["logistics_status"] = _enum_value(logistics.logistics_status)
        if logistics.signed_at is not None:
            facts["signed_at"] = logistics.signed_at

    if profile is not None:
        facts.update(
            buyer_credit_score=profile.buyer_credit_score,
            buyer_refund_rate_90d=profile.buyer_refund_rate_90d,
            refund_count_90d=profile.refund_count_90d,
        )

    if evidence is not None:
        facts.update(
            image_count=evidence.image_count,
            decodable=evidence.decodable,
            has_quality_claim=evidence.has_quality_claim,
        )
    else:
        # 未核验时按"无举证"处理，交由规则决定补证
        facts.update(image_count=0, decodable=True, has_quality_claim=False)

    # 派生字段：签收后时长（R11 超时效拒绝用）。signed_at 缺失/非法 → None（规则侧不命中）。
    # ⚠️ 键名 `days_since_signed` 而非 `signed_days`：槽位已有 `signed_days`（模型推断），
    # 两者同名会破坏「槽位不进规则事实字典」的物理隔离（test_slots T6 抓到过）。
    signed_at = facts.get("signed_at")
    if isinstance(signed_at, datetime):
        facts["days_since_signed"] = (date.today() - signed_at.date()).days
    else:
        facts["days_since_signed"] = None

    # 默认值兜底：保证规则表引用的字段恒存在，避免规则因缺字段而静默失效
    facts.setdefault("claim_intent", "refund")
    facts.setdefault("refund_reason_code", RefundReason.OTHER.value)
    facts.setdefault("buyer_credit_score", 600)
    facts.setdefault("buyer_refund_rate_90d", 0.0)
    facts.setdefault("refund_count_90d", 0)

    return facts
