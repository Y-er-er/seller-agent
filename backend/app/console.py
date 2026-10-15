"""商家售后指挥台服务 —— 收件箱 / 指派 / 介入 / 确认 / 驳回。

工单状态机（**与对话层 `conversation.status` 正交**，是"谁在处理、走到哪一步"的指挥维度）：

```
queued → agent_running → awaiting_confirm → resolved
            │   介入/驳回           │
            └──────────→ escalated ◄┘
```

**人始终在环**：指派 / 介入 / 确认权在管理员；智能体只在被指派后自主。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.chat import orchestrator
from app.db import chat_repo as repo

logger = logging.getLogger(__name__)

# ---- 工单状态 ----
WF_QUEUED = "queued"
WF_AGENT_RUNNING = "agent_running"
WF_AWAITING_CONFIRM = "awaiting_confirm"
WF_RESOLVED = "resolved"
WF_ESCALATED = "escalated"

ASSIGNEE_AGENT = "agent"
ASSIGNEE_MANUAL = "manual"

# 允许"指派"的起始状态（待处理 / 处理中可重派）
_DISPATCHABLE = (WF_QUEUED, WF_AGENT_RUNNING)


class ConsoleError(Exception):
    """指挥台业务错误。API 层据 `status_code` 转 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def _require_case(case_id: str) -> dict[str, Any]:
    case = await repo.get_case(case_id)
    if case is None:
        raise ConsoleError("case_not_found", f"工单不存在：{case_id}", 404)
    return case


# ============================================================
# 读
# ============================================================


async def list_cases(workflow_status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """收件箱列表（`workflow_status=None` 返回全部）。"""
    return await repo.list_cases_by_workflow(workflow_status, limit)


async def get_detail(case_id: str) -> dict[str, Any]:
    """单笔详情：工单 + 完整对话历史 + 槽位 + 护栏记录。"""
    case = await _require_case(case_id)
    return {
        "case": case,
        "messages": await repo.list_messages(case_id),
        "slots": await repo.list_slots(case_id),
        "guardrails": await repo.list_guardrails(case_id),
    }


# ============================================================
# 指挥动作
# ============================================================


async def dispatch(case_id: str, model: Any = None) -> dict[str, Any]:
    """指派智能体：标记 agent_running + 跑一轮处理。"""
    case = await _require_case(case_id)
    if case.get("workflow_status") not in _DISPATCHABLE:
        raise ConsoleError(
            "illegal_state", f"当前工单状态不允许指派：{case.get('workflow_status')}", 409
        )

    await repo.update_case(case_id, workflow_status=WF_AGENT_RUNNING, assignee=ASSIGNEE_AGENT)
    # 跑一轮智能体处理；产出结论时 orchestrator 会把工单推进到 awaiting_confirm
    return await orchestrator.process_case(case_id, model=model)


async def intervene(case_id: str) -> dict[str, Any]:
    """管理员介入（接管），暂停智能体自主。"""
    await _require_case(case_id)
    await repo.update_case(case_id, workflow_status=WF_ESCALATED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(case_id, "intervened", "管理员介入接管")
    return {"case_id": case_id, "workflow_status": WF_ESCALATED}


async def confirm(case_id: str, reviewer: str = "") -> dict[str, Any]:
    """确认智能体的判定（退款 / 补发 / 拒绝落地执行；处置走影子 / 双闸门，绝不 mock 成功）。"""
    case = await _require_case(case_id)
    if case.get("workflow_status") != WF_AWAITING_CONFIRM:
        raise ConsoleError("illegal_state", f"当前状态不可确认：{case.get('workflow_status')}", 409)

    now = datetime.now()
    await repo.update_case(
        case_id,
        workflow_status=WF_RESOLVED,
        confirmed_by=(reviewer or "admin"),
        confirmed_at=now,
    )
    await repo.record_guardrail(case_id, "confirmed", f"管理员确认：{case.get('decision_result')}")
    return {"case_id": case_id, "workflow_status": WF_RESOLVED, "confirmed_at": now.isoformat()}


async def reject(case_id: str, reason: str = "") -> dict[str, Any]:
    """驳回智能体的判定 → 转人工。"""
    await _require_case(case_id)
    await repo.update_case(case_id, workflow_status=WF_ESCALATED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(case_id, "rejected", reason or "管理员驳回判定")
    return {"case_id": case_id, "workflow_status": WF_ESCALATED}
