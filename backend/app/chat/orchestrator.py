"""对话编排 —— 把「护栏 → agentic 循环 → 结论渲染 → 落库 → 状态迁移」串成**端到端入口**。

这是 P1.2 的最后一块拼图：`agent.py` 只负责"跑一轮循环"，`session.py` 只负责"落库与护栏"，
本模块负责**把两者接通**，对外暴露 `handle_buyer_message()`。

一条买家消息的完整路径：

```
append_buyer_message（护栏：终态 / 成本 / 字数 / 轮数 → 裁决并落库）
   └─ 未通过 → 直接返回（不进入循环）
装配 history（system + 最近 N 条，**上下文最小化**）
构造 ToolContext（case_id / order_id / 已登记材料 / 买家留言）
run_agent（模型自主：选工具 → 多轮往复 → 提交判定 / 追问 / 转人工）
渲染最终回复 ── 红线：**有结论时结论段一律由 reply 确定性渲染**，模型话术不得改写结论
落库 agent 消息（含本轮工具审计）+ 状态迁移
```

**红线**：结论的准确性高于话术的自然度。因此当本轮产出判定结论时，**最终回复完全由
`reply.render_decision()` 生成**（确定性），模型草稿只记入审计、不对外展示——
避免模型把"驳回"说成"通过"。
"""

from __future__ import annotations

import logging
from typing import Any

from app.chat import agent, llm_adapter, reply, session, state, tools
from app.config import settings
from app.db import chat_repo as repo
from app.rules.disposal import resolve_disposal

logger = logging.getLogger(__name__)

# 送入模型的**历史消息条数上限**（上下文最小化：只给最近 N 条，不整段回灌）
MAX_HISTORY_MESSAGES = 12

# 角色映射：业务表用 buyer/agent，模型接口用 user/assistant
_ROLE_TO_MODEL = {"buyer": "user", "agent": "assistant"}


def build_history(order_id: str, messages: list[dict[str, Any]], *, limit: int | None = None) -> list[dict[str, Any]]:
    """把会话消息装配成**模型可用的消息序列**（system + 最近 N 条）。

    注意：历史里**只保留文本**，不带 `tool_calls`——历史工具调用不重放
    （否则会要求配对的 tool 消息，真机接口会报错）。
    """
    cap = MAX_HISTORY_MESSAGES if limit is None else limit
    recent = messages[-cap:] if cap > 0 else []
    history: list[dict[str, Any]] = [{"role": "system", "content": agent.build_system_prompt(order_id)}]
    for msg in recent:
        role = _ROLE_TO_MODEL.get(str(msg.get("role") or ""))
        content = str(msg.get("content") or "").strip()
        if role and content:
            history.append({"role": role, "content": content})
    return history


async def _registered_evidence(case_id: str) -> list[str]:
    """读取已登记的材料引用（供判定装配用）。"""
    try:
        rows = await repo.list_slots(case_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到材料不影响对话
        logger.warning("读取已登记材料失败（忽略）：%s", exc)
        return []
    for row in rows:
        if row.get("slot") == "evidence" and row.get("value"):
            return [item for item in str(row["value"]).split(",") if item]
    return []


def compose_reply(final: dict[str, Any]) -> tuple[str, str]:
    """把循环终态渲染为「(回复文本, 类型)」。类型用于状态迁移与前端提示。"""
    decision = final.get("decision")

    if final.get("escalated"):
        return reply.render_escalate(), "escalated"

    stop_reason = str(final.get("stop_reason") or "")
    if stop_reason == "gate_paused":
        return reply.render_unavailable(), "gate_paused"
    if stop_reason == "model_unavailable":
        return reply.render_unavailable(), "model_unavailable"
    if stop_reason == "model_error":
        return "抱歉，我这边刚刚出了点问题，请再说一次。", "model_error"

    if decision:
        # 🔴 结论段**一律确定性渲染**，模型草稿不作为回复内容（防结论被改写/曲解）
        return reply.render_decision(decision), "decision"

    if stop_reason == "budget":
        return reply.render_exhausted(), "budget"

    draft = str(final.get("draft") or "").strip()
    if draft:
        return draft, "chat"
    return reply.render_unavailable(), "model_unavailable"


async def _advance_to_decided(case_id: str) -> None:
    """已出结论：推进到 DECIDED（按状态机合法路径 CLARIFYING/COLLECTING → SUBMITTED → DECIDED）。"""
    case = await repo.get_case(case_id)
    if case is None:
        return
    status = case["status"]
    if status in (state.CLARIFYING, state.COLLECTING):
        await session.set_status(case_id, state.SUBMITTED)
        status = state.SUBMITTED
    if status == state.SUBMITTED:
        await session.set_status(case_id, state.DECIDED)


async def handle_buyer_message(
    case_id: str,
    text: str,
    *,
    model: Any = None,
    persist_usage: bool = True,
) -> dict[str, Any]:
    """处理一条买家消息，跑完一次完整往返。

    `model` 可注入（测试用脚本化桩）；为 `None` 时走 `llm_adapter.get_chat_model()`
    （双开关默认关闭 → 返回 None → 循环降级，**不会擅自计费**）。
    """
    # 1) 护栏 + 落库（终态 / 成本 / 字数 / 轮数）
    accepted = await session.append_buyer_message(case_id, text)
    if not accepted.get("accepted"):
        return {
            "accepted": False,
            "case_id": case_id,
            "code": accepted.get("code"),
            "reply": accepted.get("message"),
            "kind": "guardrail",
            "escalated": bool(accepted.get("escalated")),
        }

    # 2) 跑一轮（对已有的会话消息，不重复追加买家消息）
    result = await _run_round(case_id, model=model, persist_usage=persist_usage, buyer_note=text)
    result["gate"] = accepted.get("gate")
    return result


async def process_case(
    case_id: str,
    *,
    model: Any = None,
    persist_usage: bool = True,
) -> dict[str, Any]:
    """**指挥台指派**入口：对一笔已入队的会话，跑一轮智能体处理。

    与 `handle_buyer_message` 的区别：**不追加买家消息**（买家消息已在队列里），
    直接装配既有消息 → 跑 agentic 循环 → 落库 → 状态迁移 → 工单状态更新。
    """
    case = await repo.get_case(case_id)
    if case is None:
        return {"accepted": False, "case_id": case_id, "code": "case_not_found", "reply": "", "kind": "error"}
    return await _run_round(case_id, model=model, persist_usage=persist_usage, buyer_note="")


async def _run_round(
    case_id: str,
    *,
    model: Any,
    persist_usage: bool,
    buyer_note: str,
) -> dict[str, Any]:
    """跑一轮智能体处理（装配上下文 → agentic 循环 → 渲染 → 落库 → 状态/工单迁移）。"""
    case = await repo.get_case(case_id)
    if case is None:
        return {"accepted": False, "case_id": case_id, "code": "case_not_found", "reply": "", "kind": "error"}

    order_id = str(case.get("order_id") or case_id)

    # 装配上下文（**最小化**：只给最近 N 条 + 已登记材料）
    messages = await repo.list_messages(case_id, MAX_HISTORY_MESSAGES)
    history = build_history(order_id, messages)

    ctx = tools.ToolContext(
        case_id=case_id,
        order_id=order_id,
        claim_images=await _registered_evidence(case_id),
        requested_amount=None,          # MVP：未从消息中解析金额 → 判定按实付全额
        buyer_note=buyer_note,
        last_seq=int(case.get("last_seq") or 0),
        decision_runner=None,           # None → 走既有判定图
    )

    # 模型（默认走工厂；闸门关闭时返回 None，循环自行降级）
    if model is None:
        model = llm_adapter.get_chat_model()

    # 跑 agentic 循环（模型自主；代码只做安全边界）
    final = await agent.run_agent(
        model=model, ctx=ctx, history=history, persist_usage=persist_usage
    )

    # 渲染回复
    reply_text, kind = compose_reply(final)
    audits = list(final.get("audits") or [])

    # 落库（含本轮工具审计）
    await session.append_agent_message(case_id, reply_text, tool_calls=audits or None)

    # 状态迁移
    decision = final.get("decision")
    if kind == "escalated":
        await session.escalate(case_id, state.ESCALATE_BUYER_REQUEST)
    elif kind == "gate_paused":
        await session.escalate(case_id, state.ESCALATE_COST_PAUSED)
    elif kind == "model_unavailable":
        await session.escalate(case_id, state.ESCALATE_TOOL_FAILED)
    elif kind == "decision":
        if decision and decision.get("needs_human"):
            await session.escalate(case_id, state.ESCALATE_RULE_TO_HUMAN)
        else:
            await _advance_to_decided(case_id)

    # 工单状态（指挥台）：出结论 → 待确认；转人工 → escalated；其余保持 agent_running
    if kind == "decision" and decision and not decision.get("needs_human"):
        # 诉求方向：骨架阶段默认"退款"（模型开启后从槽位 SLOT_EXPECTATION 抽取再注入）
        intent = "refund"
        disposal = resolve_disposal(intent, str(decision.get("conclusion") or ""))
        amount = decision.get("refund_amount")
        await repo.update_case(
            case_id,
            workflow_status="awaiting_confirm",
            decision_result=disposal,
            decision_amount=(float(amount) if amount is not None else None),
        )
    elif kind in ("escalated", "model_unavailable", "gate_paused", "model_error"):
        await repo.update_case(case_id, workflow_status="escalated")

    latest = await repo.get_case(case_id)
    return {
        "accepted": True,
        "case_id": case_id,
        "reply": reply_text,
        "kind": kind,
        "escalated": kind == "escalated" or (decision or {}).get("needs_human", False),
        "decision": decision,
        "tool_audits": audits,
        "stop_reason": final.get("stop_reason") or "",
        "status": (latest or {}).get("status"),
        "workflow_status": (latest or {}).get("workflow_status"),
        "round": (latest or {}).get("round"),
        "max_rounds": settings.max_rounds,
        "gate": None,
    }
