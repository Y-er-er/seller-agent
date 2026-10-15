"""HITL 节点：转人工复核（human_review）。

这是本方案相对原计划书**最大的工程增益**：原方案需自研"转人工队列 + 状态恢复"，
LangGraph 原生提供 `interrupt()` + `Command(resume=...)` + checkpointer 持久化。

两条进入本节点的来路（载荷中的 `reason` 字段区分）：
- `rule_to_human`：规则判定转人工（如命中 R1 金额闸门 / R7 质量红线）；
- `disposal_failed`：**处置未成功落地**（未获授权或调用失败）——资金动作悬空，需人工跟进。

可选决策（载荷 `options` 字段给出建议）：
- `maintain`：维持原判定（默认）；`override`：改判；
- **`retry`：重试处置** —— 仅在 `disposal_failed` 时才有意义。

⚠️ **关键陷阱**（代码库 `10_LangGraph入门/demo09` 注释明示，必须遵守）：
`Command(resume=...)` 恢复后，本节点会被**重新进入**。
若不做幂等守卫，会产生重复的中断与重复的副作用。

设计说明——**为什么重试放在本节点内，而不是把边连回 dispose**：
把 `human_review` 连回 `dispose` 会引入图环路，而 `dispose` 有幂等守卫
（`state["action"]` 已存在即直接返回），重试必须先清空 `action`，容易演化成难以推理的循环。
在本节点内重试则**天然由人工把关**：每次重试都是一次明确的人工决定，不存在自动无限重试的风险。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.graph.state import RefundState
from app.models import ActionRecord, AuditEvent
from app.tools.aftersale import STATUS_EXECUTED, aexecute_action


def _parse_decision(value: Any) -> tuple[str | None, str | None]:
    """兼容两种 resume 载荷：字典 `{decision, reviewer}` 或直接传字符串。"""
    if isinstance(value, dict):
        return value.get("decision"), value.get("reviewer")
    return value, None


def _payload(state: RefundState, *, action: ActionRecord | None, error: str | None, attempt: int) -> dict:
    """构造 interrupt 载荷（复核台据此渲染可选项）。"""
    disposal_failed = bool(action is not None and action.detail.get("needs_human"))
    if disposal_failed:
        reason = "disposal_failed"
        message = f"处置未落地：{error or action.detail.get('error', '未知原因')}，请核实后决定是否重试"
        options = ["retry", "maintain", "override"]
    else:
        reason = "rule_to_human"
        message = "命中转人工规则，请复核后维持或改判"
        options = ["maintain", "override"]

    return {
        "type": "human_review",
        "reason": reason,
        "attempt": attempt,
        "order_id": (state.get("request").order_id if state.get("request") else None),
        "conclusion": state.get("conclusion"),
        "hit_rules": list(state.get("hits", [])),
        "hit_details": state.get("hit_details", []),
        "risk_level": (state.get("risk").level if state.get("risk") else None),
        "action": action.action if action else None,
        "options": options,
        "message": message,
    }


def _audit_event(message: str, payload: dict) -> dict:
    return {"audit_events": [AuditEvent(stage="human_review", message=message, payload=payload)]}


async def human_review_node(state: RefundState) -> dict:
    """挂起等待人工复核决定；若选择重试，则在本节点内重新执行处置。"""
    # ---- 幂等守卫：已有人工决定则直接返回，避免 resume 后重复中断 ----
    if state.get("review_decision"):
        return {}

    action = state.get("action")
    request = state.get("request")
    request_id = state.get("trace_id") or (request.order_id if request else "") or "UNKNOWN"

    attempt = 0
    decision: Any = interrupt(_payload(state, action=action, error=None, attempt=attempt))
    resolved, reviewer = _parse_decision(decision)

    # ---- 人工选择重试：在本节点内重新执行处置 ----
    while resolved == "retry":
        if action is None:
            # 没有处置动作可重试（理论上不会出现：retry 只在 disposal_failed 时提供）
            resolved = "maintain"
            break

        attempt += 1
        result = await aexecute_action(
            action=action.action,
            request_id=request_id,
            order_id=request.order_id if request else "",
            amount=float(request.claim_amount) if request else 0.0,
        )

        # 用新结果**整体替换** action（而非追加），使审计反映最新一次尝试
        action = ActionRecord(
            action=action.action,
            executed=result.executed,
            shadow=False,
            detail={
                **action.detail,
                "status": result.status,
                "retry_attempt": attempt,
                "needs_human": result.needs_human,
                "error": result.error,
                **result.detail,
            },
        )

        if result.status == STATUS_EXECUTED:
            return {
                "review_decision": resolved,
                "reviewer": reviewer,
                "action": action,
                **_audit_event(
                    f"人工重试第 {attempt} 次成功：{action.action}",
                    {"decision": resolved, "reviewer": reviewer, "retry_attempt": attempt,
                     "executed": True},
                ),
            }

        # 重试仍失败 → 再次挂起，把**新的**错误呈现给人工（每次重试均由人决定，不会自动循环）
        decision = interrupt(_payload(state, action=action, error=result.error, attempt=attempt))
        resolved, reviewer = _parse_decision(decision)

    return {
        "review_decision": resolved,
        "reviewer": reviewer,
        # 重试过则回写最新 action；未重试则不更新该字段
        **({"action": action} if attempt else {}),
        **_audit_event(
            f"人工复核决定：{resolved}" + (f"（处置重试 {attempt} 次）" if attempt else ""),
            {"decision": resolved, "reviewer": reviewer, "retry_attempt": attempt},
        ),
    }
