"""商家售后指挥台服务 —— 收件箱 / 指派 / 介入 / 确认 / 驳回 / **人工发言** /
**转人工后的三个出口（接管 / 退回智能体 / 人工结案）**。

工单状态机（**与对话层 `conversation.status` 正交**，是"谁在处理、走到哪一步"的指挥维度）：

```
queued → agent_running → awaiting_confirm → resolved
            │   介入/驳回           │
            └──────────→ escalated ◄┘
                           │
              接管 / 退回智能体 / 人工结案
```

⚠️ 最后三个动作是本版新增的**出口**：此前 `escalated` 是"只进不出"的状态——
前端在该分支只渲染一句提示、后端也没有任何动作接受它（`confirm` 要求 `awaiting_confirm`、
`dispatch` 只收 `queued/agent_running`），于是工单一旦转人工就**再也无法处理**。
这不仅影响体验，也违反政策 C5 对"撤销 / 召回"的要求。

**人始终在环**：指派 / 介入 / 确认权在管理员；智能体只在被指派后自主。

**接管的真实语义**：`assignee=manual` 不只是给人看的标签——
`orchestrator.handle_buyer_message()` 会据此**停止智能体自动应答**（接管守卫）。
此前接管只改标签、对话通路无感知，属"名义接管"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.chat import orchestrator
from app.chat import session as chat_session
from app.chat.state import ASSIGNEE_AGENT, ASSIGNEE_MANUAL, ESCALATE_BUYER_REQUEST
from app.db import chat_repo as repo

logger = logging.getLogger(__name__)

# ---- 工单状态 ----
WF_QUEUED = "queued"
WF_AGENT_RUNNING = "agent_running"
WF_AWAITING_CONFIRM = "awaiting_confirm"
WF_RESOLVED = "resolved"
WF_ESCALATED = "escalated"

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
    """管理员介入（接管），暂停智能体自主。

    ⚠️ 本动作**真的有阻断力**：`assignee=manual` 会被对话入口的接管守卫读取，
    之后买家再发消息不会触发智能体（见 `orchestrator.handle_buyer_message`）。
    """
    await _require_case(case_id)
    await repo.update_case(case_id, workflow_status=WF_ESCALATED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(case_id, "intervened", "管理员介入接管")
    return {"case_id": case_id, "workflow_status": WF_ESCALATED, "assignee": ASSIGNEE_MANUAL}


async def takeover(case_id: str) -> dict[str, Any]:
    """明确接管：把「已转人工」的工单归属到人工。

    与 `intervene` 的区别：`intervene` 是"**从智能体手里抢过来**"（处理者发生转换），
    本动作是"**确认由我负责**"——主要用于修正历史数据里"已转人工、处理者却仍是智能体
    或为空"的工单，让状态名实相符。

    幂等：重复调用不会产生副作用（只会多记一条护栏事件）。
    """
    case = await _require_case(case_id)
    if case.get("workflow_status") == WF_RESOLVED:
        raise ConsoleError("illegal_state", "工单已完结，无需接管", 409)

    await repo.update_case(case_id, workflow_status=WF_ESCALATED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(case_id, "taken_over", "管理员接管工单")
    return {"case_id": case_id, "workflow_status": WF_ESCALATED, "assignee": ASSIGNEE_MANUAL}


async def resume_agent(case_id: str) -> dict[str, Any]:
    """把「已转人工」的工单**退回智能体**继续处理。

    典型场景：因**瞬时故障**（`tool_failed` / `model_unavailable`）转人工——
    故障排除后不必重开工单，直接退回让智能体接着办，买家也不必重新描述问题。

    ⚠️ **买家主动要求人工（`buyer_request`）时拒绝退回**：那是买家的明确意愿，
    退回等于把买家推回给机器人。**买家意愿优先于处理效率**，此时只能接管或结案。

    退回会连带撤销会话终态（见 `session.resume_from_escalation`），
    使对话通路重新可用——否则工单虽退回，会话仍是终态，买家一发消息就被挡回。
    """
    case = await _require_case(case_id)
    if case.get("workflow_status") != WF_ESCALATED:
        raise ConsoleError(
            "illegal_state",
            f"仅「已转人工」的工单可退回智能体，当前：{case.get('workflow_status')}",
            409,
        )
    if str(case.get("escalated_reason") or "") == ESCALATE_BUYER_REQUEST:
        raise ConsoleError(
            "buyer_wants_human", "买家已明确要求人工，不能退回智能体（请直接接管）", 409
        )

    resumed = await chat_session.resume_from_escalation(case_id)
    if not resumed.get("ok"):
        detail = (
            "会话已结束（CLOSED），不能退回智能体"
            if resumed.get("reason") == "closed"
            else f"会话当前状态不可退回：{resumed.get('status')}"
        )
        raise ConsoleError("illegal_state", detail, 409)

    await repo.update_case(case_id, workflow_status=WF_AGENT_RUNNING, assignee=ASSIGNEE_AGENT)
    await repo.record_guardrail(case_id, "resumed_to_agent", "管理员退回智能体继续处理")
    return {"case_id": case_id, "workflow_status": WF_AGENT_RUNNING, "assignee": ASSIGNEE_AGENT}


async def close_case(case_id: str, note: str = "") -> dict[str, Any]:
    """人工结案：管理员处理完毕，把工单置为已完结。

    只改**指挥维度**（`workflow_status` / `assignee`），不动会话状态——
    会话状态属于"买家与智能体的对话生命周期"，由 `session` 层负责。
    结案后买家若仍有问题，正常做法是重新发起咨询（`case_id = order_id` 幂等）。
    """
    case = await _require_case(case_id)
    if case.get("workflow_status") == WF_RESOLVED:
        raise ConsoleError("illegal_state", "工单已完结", 409)

    await repo.update_case(case_id, workflow_status=WF_RESOLVED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(case_id, "closed_by_admin", note or "管理员人工结案")
    return {"case_id": case_id, "workflow_status": WF_RESOLVED, "assignee": ASSIGNEE_MANUAL}


async def post_human_message(case_id: str, text: str, author: str = "") -> dict[str, Any]:
    """管理员以**人工身份**直接回复买家。

    三条语义（都是刻意的）：

    1. **首次发言即接管**——人一旦开口，就说明人已进场，此时智能体必须停手，
       否则会出现"人机各说一句"的打架局面。故本动作会同时把 `assignee` 置为 `manual`；
    2. 消息以角色 `human` 落库，**不经任何模型改写**（人的意志不得被模型复述）；
    3. 记一条 guardrail 事件，卷宗时间线可追溯"谁在什么时候以人工身份说过话"。
    """
    await _require_case(case_id)
    body = (text or "").strip()
    if not body:
        raise ConsoleError("empty_message", "消息内容不能为空", 400)

    await repo.update_case(case_id, workflow_status=WF_ESCALATED, assignee=ASSIGNEE_MANUAL)
    await repo.record_guardrail(
        case_id, "human_replied", f"{(author or '管理员')} 以人工身份回复买家"
    )

    posted = await chat_session.append_human_message(case_id, body)
    if not posted.get("ok"):
        # 落库失败**不得当成功**——否则界面上看到"已发送"，买家其实没收到
        raise ConsoleError("persist_failed", "人工消息写入失败，请重试", 503)

    case = await repo.get_case(case_id)
    return {
        "case_id": case_id,
        "seq": posted.get("seq"),
        "workflow_status": (case or {}).get("workflow_status"),
        "assignee": (case or {}).get("assignee"),
        "messages": await repo.list_messages(case_id),
    }


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
