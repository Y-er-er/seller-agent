"""节点 7：合规审计（audit）。

所有分支的**唯一汇聚点**，保证"无论走哪条路径都留痕"（方案 §4 要点 3）。

两条留痕并行：
- **技术留痕**：由 LangGraph checkpointer 自动完成（每个 superstep 一份状态快照）；
- **业务留痕**：本节点写入 MySQL 的 `decision_audit` 表（对外审计、客服口径、一致率统计）。

注意：落库是**旁路**。`save_decision()` 内部已吞掉所有异常并返回布尔值，
因此数据库不可用**不会**影响判定结论与图的正常结束。
"""

from __future__ import annotations

from app.db import save_decision
from app.graph.state import RefundState
from app.models import AuditEvent


def _build_record(state: RefundState) -> dict:
    """把图状态整理为 `decision_audit` 的一行记录。"""
    request = state.get("request")
    action = state.get("action")
    risk = state.get("risk")

    # 受理号优先取 trace_id（intake 写入）；异常路径退化为订单号，最后兜底占位
    request_id = state.get("trace_id") or (request.order_id if request else "") or "UNKNOWN"

    return {
        "request_id": request_id,
        "order_id": (request.order_id if request else ""),
        "conclusion": state.get("conclusion") or "",
        "hit_rules": list(state.get("hits", [])),
        # 命中明细（字段 / 运算符 / 阈值 / 实际值），使结论可逐项回溯
        "field_values": state.get("hit_details", []),
        "rule_version": state.get("rule_version") or "",
        "risk_level": risk.level if risk else None,
        "action": action.action if action else None,
        # 以下三个字段描述的是**本次处置动作**的状态，而非全局开关：
        # to_human / request_evidence 路径不经过 dispose，没有资金动作，
        # 故 action=None、executed=False、shadow=False（"没有动作"比"动作处于影子模式"更准确）。
        # 真正判断"这笔钱有没有动"应看 executed。
        "executed": bool(action.executed) if action else False,
        "shadow": bool(action.shadow) if action else False,
        "review_decision": state.get("review_decision"),
        "reviewer": state.get("reviewer"),
    }


async def audit_node(state: RefundState) -> dict:
    """汇总关键结论，写入审计表，并追加一条内存审计事件。"""
    record = _build_record(state)

    # 落库失败会返回 False（内部已降级），不影响后续流程
    persisted = await save_decision(record)

    event_payload = {
        **record,
        "hit_rules": record["hit_rules"],  # 保留列表形态，便于内存侧阅读
        "audit_persisted": persisted,      # 是否成功落 MySQL
        "intake_error": state.get("intake_error"),
    }

    return {
        "audit_events": [
            AuditEvent(
                stage="audit",
                message="链路留痕完成" + ("（已落库）" if persisted else "（未落库，已降级）"),
                payload=event_payload,
            )
        ]
    }
