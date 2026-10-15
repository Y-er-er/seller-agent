"""条件边路由函数。

固定优先级（方案 §4 要点 2）：转人工 > 补证 > 自动处置。
命中 `to_human` 直接旁路到 human_review，**不再进入 risk / dispose**。
"""

from __future__ import annotations

from app.graph.state import RefundState


def route_after_intake(state: RefundState) -> str:
    """受理校验失败 → 直接落审计；通过 → 进入并行取数。"""
    return "invalid" if state.get("intake_error") else "fetch"


def route_after_judge(state: RefundState) -> str:
    """转人工 → 挂起等复核；其余 → 走风险标记与处置。"""
    return "human_review" if state.get("conclusion") == "to_human" else "risk"


def route_after_dispose(state: RefundState) -> str:
    """处置后分流。

    **处置未成功落地时升级人工**（`needs_human`）：处置未获授权或调用失败，
    若直接走 audit 结束，资金动作就"悬空"了——判定说该退、但钱没退，且无人跟进。
    故此时挂起转人工，由人工确认后再决定是否重试。

    影子模式下不会设置该标记（那是预期行为，不是异常），照常落审计。
    """
    action = state.get("action")
    if action is not None and action.detail.get("needs_human"):
        return "human_review"
    return "audit"
