"""节点 6：处置执行（dispose）。

按结论产出并（在获授权时）执行处置动作。三条分支：

| 分支 | 条件 | 行为 |
| - | --- | --- |
| 影子模式 | `SHADOW_MODE=true` | 只记录"本应执行的动作"，**不落地** |
| 拒绝执行 | 未开 `DISPOSAL_ENABLED` 或未配置执行接口 | **不产生外部调用**，标记 `needs_human` → 升级人工 |
| 真实执行 | 双开关开启且已配置接口 | 带**幂等键**调用；成功落地；**失败同样升级人工** |

⚠️ 与取数层的安全差异：取数失败可以降级（规则判"数据不足 → 转人工"是安全的）；
**资金动作失败绝不可当成功**——否则会出现"以为退了、其实没退"。
因此本节点在任何不可执行的情形下都不会伪造成功，而是转为人工跟进。

资金动作护栏：
- **幂等**：state 中已有 `action` 即直接返回（防 interrupt/resume 重入重复处置）；
- **幂等键**：真实调用携带 `Idempotency-Key = 受理号`，防网络重试导致重复退款；
- **白名单**：仅 `_ACTION_BY_CONCLUSION` 列出的动作可执行，未知结论一律 noop。
"""

from __future__ import annotations

from app.config import settings
from app.graph.state import RefundState
from app.models import ActionRecord, AuditEvent
from app.tools.aftersale import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    aexecute_action,
)

# 结论 → 处置动作（白名单）。未列举的结论一律 noop，避免"未知结论"被误当成可执行动作。
_ACTION_BY_CONCLUSION: dict[str, str] = {
    "auto_pass": "approve_refund",
    "auto_reject": "reject_refund",
    "request_evidence": "notify_evidence",
}


def _record(
    *,
    action: str,
    executed: bool,
    shadow: bool,
    detail: dict,
    message: str,
) -> dict:
    """构造节点返回：动作记录 + 一条审计事件。"""
    record = ActionRecord(action=action, executed=executed, shadow=shadow, detail=detail)
    return {
        "action": record,
        "audit_events": [AuditEvent(stage="dispose", message=message, payload=record.model_dump())],
    }


async def dispose_node(state: RefundState) -> dict:
    """执行处置。"""
    # ---- 幂等守卫：已处置过则不再处置（等价 ToolCallLimitMiddleware(thread_limit=1)）----
    if state.get("action") is not None:
        return {}

    conclusion = state.get("conclusion")
    action = _ACTION_BY_CONCLUSION.get(conclusion or "", "noop")

    request = state.get("request")
    request_id = state.get("trace_id") or (request.order_id if request else "") or "UNKNOWN"
    order_id = request.order_id if request else ""
    amount = float(request.claim_amount) if request else 0.0

    base_detail = {
        "conclusion": conclusion,
        "hit_rules": list(state.get("hits", [])),
        "rule_version": state.get("rule_version"),
    }

    # ---- 分支 1：影子模式（默认）----
    if settings.shadow_mode:
        return _record(
            action=action,
            executed=False,
            shadow=True,
            detail={**base_detail, "mode": "shadow"},
            message=f"影子模式：本应执行 {action}（未落地）",
        )

    # ---- 分支 2：没有可执行动作（结论未列入白名单）----
    if action == "noop":
        return _record(
            action=action,
            executed=False,
            shadow=False,
            detail={**base_detail, "mode": "noop"},
            message="无对应处置动作（noop）",
        )

    # ---- 分支 3：真实执行 ----
    result = await aexecute_action(
        action=action, request_id=request_id, order_id=order_id, amount=amount
    )

    detail = {
        **base_detail,
        "mode": "real",
        "status": result.status,
        "idempotency_key": request_id,
        **result.detail,
    }

    if result.status == STATUS_EXECUTED:
        return _record(
            action=action,
            executed=True,
            shadow=False,
            detail=detail,
            message=f"已执行 {action}（{result.detail.get('disposal_id') or '无单号'}）",
        )

    # 未获授权 / 调用失败 —— 一律不伪造成功，标记升级人工
    level = "未获授权" if result.status == STATUS_NOT_CONFIGURED else "执行失败"
    detail["needs_human"] = True
    detail["error"] = result.error
    return _record(
        action=action,
        executed=False,
        shadow=False,
        detail=detail,
        message=f"处置{level}：{result.error}（转人工跟进）",
    )
