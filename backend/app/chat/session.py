"""会话服务 —— 把状态机（`state.py`）与数据访问（`chat_repo`）串起来的**编排层**。

分工（刻意保持单一职责）：
- **所有判定规则**在 `state.py`（纯函数，可离线单测）；
- **所有 SQL** 在 `app/db/chat_repo.py`；
- 本模块只做：取数 → 调裁决 → 落库 → 返回结果。

`repo` 是**模块级引用**，便于测试注入内存假仓储（无需连真库）。

会话与工单是同一个东西：`case_id = order_id`，与判定图 `thread_id`、审计检索键**四处同值**。
这样重复发起同一订单的咨询是**幂等**的（返回既有会话，而不是新建一个工单）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.chat import cost_gate, state
from app.config import settings
from app.db import chat_repo as repo

logger = logging.getLogger(__name__)

GREETING = (
    "你好，我是售后助手，已为你关联订单 {order_id}。\n"
    "请直接描述遇到的问题（例如「包裹破损了」「一直没收到货」），"
    "我会先帮你核实订单与物流，再告诉你能怎么处理。"
)

# 触发过超长提示的护栏类型（用于判断"首次 / 再次"）
_GUARDRAIL_MSG_TOO_LONG = "msg_too_long"


class SessionError(Exception):
    """会话层业务错误。API 层据 `status_code` 转 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def _require_case(case_id: str) -> dict[str, Any]:
    case = await repo.get_case(case_id)
    if case is None:
        raise SessionError("case_not_found", f"会话不存在：{case_id}", 404)
    return case


# ============================================================
# 创建 / 读取
# ============================================================


async def create_session(order_id: str) -> dict[str, Any]:
    """创建（或幂等返回）一个会话。

    闸门语义：**新会话**在暂停时直接拒绝——不静默失败，而是把原因返回给调用方。
    """
    order_id = (order_id or "").strip()
    if not order_id:
        raise SessionError("bad_request", "缺少订单号", 400)

    gate = await cost_gate.precheck(ongoing=False)
    if gate.paused:
        raise SessionError("cost_paused", gate.message, 503)

    case_id = order_id  # 会话 = 工单；与判定图 thread_id 同值
    timeout_at = datetime.now() + timedelta(hours=settings.session_timeout_hours)
    created = await repo.create_case(case_id, order_id, timeout_at)

    if not created:
        # 已存在 → 幂等返回既有会话（重复发起同一订单的咨询不新建工单）
        existing = await repo.get_case(case_id)
        if existing is None:
            raise SessionError("create_failed", "会话创建失败，请稍后重试", 503)
        return {"case_id": case_id, "resumed": True, "session": existing, "gate": gate.to_dict()}

    greeting = GREETING.format(order_id=order_id)
    await repo.append_message(case_id, 1, "agent", greeting)
    await repo.update_case(case_id, status=state.CLARIFYING, last_seq=1)
    case = await repo.get_case(case_id)
    return {"case_id": case_id, "resumed": False, "session": case, "gate": gate.to_dict()}


async def load_session(case_id: str, *, message_limit: int | None = None) -> dict[str, Any]:
    """读取会话全貌（卷宗「对话历史」的数据源）。"""
    case = await _require_case(case_id)
    return {
        "session": case,
        "messages": await repo.list_messages(case_id, message_limit),
        "slots": await repo.list_slots(case_id),
        "guardrails": await repo.list_guardrails(case_id),
    }


# ============================================================
# 状态迁移
# ============================================================


async def set_status(case_id: str, target: str, *, reason: str | None = None) -> bool:
    """按状态机校验后迁移；非法迁移抛 `SessionError`（不静默放行）。"""
    case = await _require_case(case_id)
    error = state.transition_error(case["status"], target)
    if error:
        raise SessionError("illegal_transition", error, 409)
    return await repo.update_case(case_id, status=target, escalated_reason=reason)


async def escalate(case_id: str, reason: str) -> bool:
    """转人工（终态）。必须带原因码，且**同时落护栏事件**以便统计与复盘。"""
    case = await repo.get_case(case_id)
    if case is None:
        return False
    if state.is_terminal(case["status"]):
        return True  # 已是终态，幂等返回（避免重复挂起）

    await repo.record_guardrail(case_id, reason, "转人工")
    return await repo.update_case(case_id, status=state.ESCALATED, escalated_reason=reason)


# ============================================================
# 消息
# ============================================================


async def append_buyer_message(case_id: str, text: str) -> dict[str, Any]:
    """接收买家消息。返回裁决结果（不抛业务异常，除会话不存在）。

    裁决顺序（**顺序即优先级，不要调换**）：
    1. 迁移守卫：终态 / 成本暂停 / 轮数耗尽 → 按需转人工；
    2. 字数裁决：首次超长只提示（**不计轮次**），再犯转人工；
    3. 正常接收：写消息 → 轮次 +1 → （首条时）状态 CREATED → CLARIFYING。
    """
    case = await _require_case(case_id)
    gate = await cost_gate.precheck(ongoing=True)

    guard = state.check_can_accept_message(
        status=case["status"],
        round_used=int(case["round"]),
        gate_paused=gate.paused,
    )
    if not guard.allowed:
        if guard.escalate_reason:
            await escalate(case_id, guard.escalate_reason)
        return {
            "accepted": False,
            "code": guard.code,
            "message": guard.message,
            "escalated": bool(guard.escalate_reason),
            "gate": gate.to_dict(),
        }

    guardrails = await repo.list_guardrails(case_id)
    already_warned = any(item.get("type") == _GUARDRAIL_MSG_TOO_LONG for item in guardrails)
    length = state.evaluate_message_length(text, already_warned=already_warned)
    if not length.ok:
        attempt = 2 if already_warned else 1
        await repo.record_guardrail(case_id, _GUARDRAIL_MSG_TOO_LONG, f"第 {attempt} 次超长")
        if length.escalate:
            await escalate(case_id, state.ESCALATE_MSG_TOO_LONG)
        return {
            "accepted": False,
            "code": length.code,
            "message": length.message,
            "escalated": length.escalate,
            "gate": gate.to_dict(),
        }

    seq = int(case["last_seq"]) + 1
    await repo.append_message(case_id, seq, "buyer", text)

    # 首条买家消息即离开 CREATED（其余状态保持不变）
    target_status = state.CLARIFYING if case["status"] == state.CREATED else None
    await repo.update_case(case_id, status=target_status, round_delta=1, last_seq=seq)

    return {
        "accepted": True,
        "code": "",
        "message": "",
        "seq": seq,
        "round": int(case["round"]) + 1,
        "max_rounds": settings.max_rounds,
        "gate": gate.to_dict(),
    }


async def append_agent_message(
    case_id: str, text: str, *, tool_calls: Any = None, guardrail_event: str | None = None
) -> dict[str, Any]:
    """写入智能体回复。`tool_calls` 记本轮工具调用摘要（审计与卷宗需要）。"""
    case = await _require_case(case_id)
    seq = int(case["last_seq"]) + 1
    ok = await repo.append_message(
        case_id, seq, "agent", text, tool_calls=tool_calls, guardrail_event=guardrail_event
    )
    if ok:
        await repo.update_case(case_id, last_seq=seq)
    return {"ok": ok, "seq": seq}
