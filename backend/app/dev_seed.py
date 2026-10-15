"""开发演示用的"取数种子"。

⚠️ **边界声明（重要）**：
- 本模块**不是业务能力**，只在**本地 mock 模式**（未配置真实业务 API）下提供数据，
  用途是让复核工作台与验收流程能立刻看到各类判定结果；
- 已配置真实业务 API（`ORDER_API_BASE` 等）时，端点会拒绝调用（409），
  避免演示数据混入真实链路；
- 生产部署可直接删除本模块并移除 `main.py` 中对应的 `/api/dev/seed` 端点。

场景与规则的对应关系（用于演示覆盖各条分支）：
    small_not_signed    小额 + 未签收 + 未收到货  → auto_pass        (R2)
    signed_not_received 已签收 + 称未收到货      → request_evidence (R3)
    good_credit_small   ¥30 + 高信用 + 低退款率  → auto_pass        (R4)
    frequent_refunder   90 天内高频退款          → to_human         (R5)
    big_amount_quality  大额 + 质量主张          → to_human         (R1+R7)
    wrong_item_no_image 错发/漏发且无举证图       → request_evidence (R6)
    no_data             不注入任何数据            → to_human        (R8，兜底)
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.models import LogisticsStatus, RefundReason
from app.tools import seed_logistics, seed_order, seed_profile

# 每个场景：物流状态 + 买家画像 + 建议的申请原因码与金额（前端用于预填表单）
SCENARIOS: dict[str, dict[str, Any]] = {
    "small_not_signed": {
        "label": "小额未签收（自动通过 R2）",
        "logistics": LogisticsStatus.NOT_SIGNED,
        "credit": 600,
        "refund_rate": 0.05,
        "refund_count": 1,
        "suggest_reason": RefundReason.NOT_RECEIVED,
        "suggest_amount": 39.0,
    },
    "signed_not_received": {
        "label": "已签收称未收到（要求补证 R3）",
        "logistics": LogisticsStatus.SIGNED,
        "credit": 600,
        "refund_rate": 0.05,
        "refund_count": 1,
        "suggest_reason": RefundReason.NOT_RECEIVED,
        "suggest_amount": 199.0,
    },
    "good_credit_small": {
        "label": "小额 + 高信用（自动通过 R4）",
        "logistics": LogisticsStatus.IN_TRANSIT,
        "credit": 700,
        "refund_rate": 0.10,
        "refund_count": 1,
        "suggest_reason": RefundReason.NO_REASON,
        "suggest_amount": 30.0,
    },
    "frequent_refunder": {
        "label": "90 天高频退款（转人工 R5）",
        "logistics": LogisticsStatus.SIGNED,
        "credit": 650,
        "refund_rate": 0.20,
        "refund_count": 6,
        "suggest_reason": RefundReason.NO_REASON,
        "suggest_amount": 120.0,
    },
    "big_amount_quality": {
        "label": "大额质量主张（转人工 R1+R7）",
        "logistics": LogisticsStatus.SIGNED,
        "credit": 700,
        "refund_rate": 0.05,
        "refund_count": 0,
        "suggest_reason": RefundReason.QUALITY,
        "suggest_amount": 1800.0,
    },
    "wrong_item_no_image": {
        "label": "错发漏发且无举证图（要求补证 R6）",
        "logistics": LogisticsStatus.SIGNED,
        "credit": 600,
        "refund_rate": 0.05,
        "refund_count": 1,
        "suggest_reason": RefundReason.WRONG_ITEM,
        "suggest_amount": 260.0,
    },
    "no_data": {
        "label": "无任何取数（数据不足兜底 R8）",
        "logistics": None,  # None 表示不注入 —— 取数返回 None，触发兜底
        "credit": None,
        "refund_rate": None,
        "refund_count": None,
        "suggest_reason": RefundReason.OTHER,
        "suggest_amount": 88.0,
    },
}


def scenario_options() -> list[dict[str, Any]]:
    """给前端用的场景清单（不含内部字段）。"""
    return [
        {
            "key": key,
            "label": spec["label"],
            "suggest_reason": spec["suggest_reason"].value,
            "suggest_amount": spec["suggest_amount"],
        }
        for key, spec in SCENARIOS.items()
    ]


def real_api_configured() -> bool:
    """是否已接入真实业务 API —— 此时禁止注入演示数据。"""
    return bool(settings.order_api_base or settings.logistics_api_base)


def suggest_amount(scenario: str) -> float:
    """取场景的建议金额（未显式传金额时使用）。"""
    return SCENARIOS[scenario]["suggest_amount"]


def apply_scenario(order_id: str, scenario: str, pay_amount: float) -> None:
    """按场景注入取数数据（写入内存 mock）。

    未注入任何数据时，`fetch_*` 返回 None，规则引擎判为"数据不足 → 转人工"，
    这正是 `no_data` 场景要演示的兜底路径。
    """
    spec = SCENARIOS[scenario]
    if spec["logistics"] is None:
        return

    seed_order(order_id, pay_amount=pay_amount, logistics_status=spec["logistics"])
    seed_logistics(order_id, logistics_status=spec["logistics"])
    seed_profile(
        order_id,
        buyer_credit_score=spec["credit"],
        buyer_refund_rate_90d=spec["refund_rate"],
        refund_count_90d=spec["refund_count"],
    )
