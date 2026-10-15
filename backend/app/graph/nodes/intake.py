"""节点 1：申请受理（intake）。

职责：校验必填字段、建立受理号（同时作为 LangGraph 的 thread_id）。
判定：**确定性**，不调用模型。
"""

from __future__ import annotations

from app.graph.state import RefundState
from app.models import AuditEvent


def intake_node(state: RefundState) -> dict:
    """校验入参并把受理号写入 trace_id。

    校验失败时写入 intake_error，由路由函数把流程短路到 audit（不进入取数与判定）。
    """
    request = state.get("request")

    errors: list[str] = []
    if request is None:
        errors.append("缺少申请主体 request")
    else:
        if not request.order_id:
            errors.append("缺少订单号 order_id")
        if request.claim_amount <= 0:
            errors.append("申请退款金额必须大于 0")
        if request.claim_amount > request.pay_amount:
            errors.append("申请退款金额不得超过实付金额")

    if errors:
        return {
            "intake_error": "；".join(errors),
            "audit_events": [
                AuditEvent(stage="intake", message="受理校验未通过", payload={"errors": errors})
            ],
        }

    # 受理号 = 订单号（M0 简化）；它同时是 checkpointer 的 thread_id
    return {
        "intake_error": None,
        "trace_id": request.order_id,  # type: ignore[union-attr]
        "audit_events": [
            AuditEvent(
                stage="intake",
                message="受理通过",
                payload={"order_id": request.order_id, "claim_amount": request.claim_amount},  # type: ignore[union-attr]
            )
        ],
    }
