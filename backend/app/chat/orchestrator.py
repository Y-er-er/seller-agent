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

from app.chat import agent, llm_adapter, reply, session, slots, state, tools
from app.config import settings
from app.db import chat_repo as repo
from app.rules.disposal import resolve_disposal

logger = logging.getLogger(__name__)

# 送入模型的**历史消息条数上限**（只给最近 N 条，不整段回灌）。
# 取值依据：一次完整售后往复（买家 3—5 句 + 智能体 3—5 句 + 工具结果）通常 ≤20 条，
# 20 条足以覆盖完整上下文；不是按"省 token"设定的。
MAX_HISTORY_MESSAGES = 20

# 角色映射：业务表用 buyer / agent / human，模型接口用 user / assistant。
# ⚠️ `human`（人工客服）映射为 assistant —— 与智能体同属"我方"；
#    真正的区分靠内容前缀（见 HUMAN_PREFIX），避免模型把人的话当成自己说过的。
_ROLE_TO_MODEL = {"buyer": "user", "agent": "assistant", "human": "assistant"}

#: 人工消息进入模型上下文时的前缀标记（**只作用于送入模型的上下文，不改落库原文**）
HUMAN_PREFIX = "[人工客服] "


def build_history(order_id: str, messages: list[dict[str, Any]], *, limit: int | None = None) -> list[dict[str, Any]]:
    """把会话消息装配成**模型可用的消息序列**（system + 最近 N 条）。

    注意：历史里**只保留文本**，不带 `tool_calls`——历史工具调用不重放
    （否则会要求配对的 tool 消息，真机接口会报错）。
    """
    cap = MAX_HISTORY_MESSAGES if limit is None else limit
    recent = messages[-cap:] if cap > 0 else []
    history: list[dict[str, Any]] = [{"role": "system", "content": agent.build_system_prompt(order_id)}]
    for msg in recent:
        raw_role = str(msg.get("role") or "")
        role = _ROLE_TO_MODEL.get(raw_role)
        content = str(msg.get("content") or "").strip()
        if role and content:
            # 人工发言加前缀：让模型知道"这句是客服说的，不是我说的"
            if raw_role == state.ROLE_HUMAN:
                content = HUMAN_PREFIX + content
            history.append({"role": role, "content": content})
    return history


async def _slot_value(case_id: str, slot: str) -> str:
    """读取某个槽位的当前值（取不到返回空串，不抛异常）。"""
    try:
        rows = await repo.list_slots(case_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到槽位不影响对话，但会退化为默认方向
        logger.warning("读取槽位 %s 失败（忽略）：%s", slot, exc)
        return ""
    for row in rows:
        if row.get("slot") == slot and row.get("value"):
            return str(row["value"])
    return ""


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
    # 0) 先读工单：判断管理员是否已接管 —— 它决定下面走"人"还是走"智能体"
    case = await repo.get_case(case_id)
    taken_over = bool(case) and str(case.get("assignee") or "") == state.ASSIGNEE_MANUAL

    # 1) 护栏 + 落库（终态 / 成本 / 字数 / 轮数）
    #    ⚠️ 人工接管时"终态 / 成本 / 轮数"三条豁免：转人工会把会话置为终态，
    #       若照旧拒收，买家会被"会话已结束"挡在门外，人反而接不上话。
    accepted = await session.append_buyer_message(case_id, text, human_takeover=taken_over)
    if not accepted.get("accepted"):
        # 护栏触发转人工（轮数耗尽 / 字数二次超长 / 成本暂停）时，工单也要落到人工队列，
        # 否则指挥台仍显示旧状态，管理员看不到这是一笔等待人工的工单。
        if accepted.get("escalated"):
            await repo.update_case(case_id, workflow_status="escalated")
        return {
            "accepted": False,
            "case_id": case_id,
            "code": accepted.get("code"),
            "reply": accepted.get("message"),
            "kind": "guardrail",
            "escalated": bool(accepted.get("escalated")),
        }

    # 2) **接管守卫**：管理员已接管（assignee=manual）→ 智能体**不再自动应答**。
    #
    #    消息上一步已落库（人工必须看得到买家说了什么），此处直接收口：
    #    会话此刻归人工，智能体若再写一条回复进去，会与人说的话打架。
    #    因此只返回一条**界面提示**（`notice`，不落库），不回传 reply。
    if taken_over:
        await repo.record_guardrail(case_id, "human_takeover", "管理员已接管，智能体不自动应答")
        latest = await repo.get_case(case_id) or case or {}
        return {
            "accepted": True,
            "case_id": case_id,
            "reply": "",
            "notice": reply.render_human_takeover(),
            "kind": "human_takeover",
            "escalated": False,
            "decision": None,
            "tool_audits": [],
            "stop_reason": "human_takeover",
            "status": latest.get("status"),
            "workflow_status": latest.get("workflow_status"),
            "round": latest.get("round"),
            "max_rounds": settings.max_rounds,
            "gate": accepted.get("gate"),
        }

    # 3) 跑一轮（对已有的会话消息，不重复追加买家消息）
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
        # 诉求方向：由槽位 `claim_direction` 决定处置方向（退款 / 补发 / 换货）。
        # ⚠️ 此前这里硬编码 `intent = "refund"`，导致补发永不可达（R9/R10 永不命中）。
        direction = await _slot_value(case_id, slots.SLOT_CLAIM_DIRECTION)
        claim_type = slots.direction_to_claim_type(direction)
        disposal = resolve_disposal(claim_type, str(decision.get("conclusion") or ""))
        amount = decision.get("refund_amount")
        await repo.update_case(
            case_id,
            workflow_status="awaiting_confirm",
            decision_result=disposal,
            decision_amount=(float(amount) if amount is not None else None),
        )
    elif kind in ("escalated", "model_unavailable", "gate_paused"):
        await repo.update_case(case_id, workflow_status="escalated")
    elif kind == "model_error":
        # 模型**瞬时失败**：回复本身就是「请再说一次」，**不转人工**。
        # 若打成"已转人工"，指挥台会出现「已转人工 / 处理者=— / 买家仍可输入」的自相矛盾状态，
        # 而且并没有任何人被指派（实测 DEMO002）。这里保持"处理中"，买家重试即可；
        # 失败另落一条护栏事件留痕，便于排查模型侧问题。
        await repo.record_guardrail(case_id, "model_error", "模型调用失败，本轮未完成，等待买家重试")
        await repo.update_case(case_id, workflow_status="agent_running")

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
