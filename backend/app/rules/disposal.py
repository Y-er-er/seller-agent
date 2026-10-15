"""处置结果解析 —— 处置 = f(诉求, 结论)。

**这是"处置层"，不是"判定层"**（方案 v4 §三）：
- 判定结论由规则引擎给出（`auto_pass` / `auto_reject` / `request_evidence` / `to_human`）；
- 处置结果由「诉求分类 claim_type」与「结论」**确定性映射**得出，**模型不参与**；
- 目的：让同一个 `auto_pass` 能按诉求方向落地为"退款"或"补发"或"换货"。

纯函数、无 IO、无模型，可离线单测。
"""

from __future__ import annotations

from typing import Any

# 处置结果枚举（含预留项；第一步只落地 refund / reship / reject / manual / pending_evidence）
DISPOSAL_RESULTS: tuple[str, ...] = (
    "refund",            # 退款（仅退款 / 退货退款）
    "reship",            # 补发 / 重发
    "reject",            # 拒绝
    "manual",            # 转人工
    "pending_evidence",  # 需补证
    "exchange",          # 换货（P3）
    "partial_refund",    # 部分退款 / 补偿（P3）
)

# auto_pass 时，诉求 → 处置（未列举的诉求默认退款）
_PASS_BY_CLAIM_TYPE: dict[str, str] = {
    "refund": "refund",
    "return_refund": "refund",
    "reship": "reship",
    "exchange": "exchange",
    "partial_refund": "partial_refund",
}


def _enum_value(value: Any) -> Any:
    """枚举取 .value，便于与字符串比较。"""
    return getattr(value, "value", value)


def resolve_disposal(claim_type: Any, conclusion: str) -> str:
    """由「诉求 + 结论」确定性映射出处置结果。

    - `claim_type`：诉求分类（str 或 ClaimType 枚举）；
    - `conclusion`：规则引擎结论（auto_pass / auto_reject / request_evidence / to_human）。
    """
    ct = _enum_value(claim_type)

    if conclusion == "auto_pass":
        return _PASS_BY_CLAIM_TYPE.get(ct, "refund")
    if conclusion == "auto_reject":
        return "reject"
    if conclusion == "request_evidence":
        return "pending_evidence"
    # to_human / 未知结论 → 一律人工兜底（不猜测）
    return "manual"
