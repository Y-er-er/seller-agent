"""agentic 循环子图 —— 模型 ⇄ 工具，手写 LangGraph（P1 实施设计 D1）。

```
START → guard → model_step ─┬─(有 tool_calls)→ tool_step → guard
                            └─(无 tool_calls)→ END
```

**谁在决策（不可动摇）**：
- **模型自主**决定：调用哪个工具、传什么参数、调用顺序、调几次、是否追问、何时收尾。
  循环只把工具结果**如实回灌**，不替模型挑工具、不替模型写结论。
- 代码只做**安全边界**：① 每次模型调用前过 `guard`（成本闸门预检）；② 单次往返的
  工具调用总量 `≤ MAX_TOOLS_PER_ROUND`；③ 转人工后立即停止。

**为什么手写而不是 `create_agent`**：需要"每次模型调用前必过预检、每次工具调用后必留审计、
且有硬性终止条件"这三件事**不可绕过**——手写节点才能保证（见 D1 决策）。

**不配 checkpointer**：会话状态的持久化在业务表 `conversation_case`，本子图无状态、可重放。
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.chat import cost_gate, tools
from app.chat.tool_spec import TOOL_SPECS
from app.config import settings

logger = logging.getLogger(__name__)

# 硬性循环上限：即便模型一直要调工具，也不会无限循环
MAX_MODEL_STEPS = 8


class AgentState(TypedDict, total=False):
    """循环状态。

    ⚠️ 本子图**不配 checkpointer**，故 state 中可以携带非序列化对象（模型、上下文）。
    """

    model: Any                 # ChatModel（真机或桩）
    ctx: Any                   # tools.ToolContext
    messages: list[dict[str, Any]]
    tool_budget: int           # 本次往返允许的工具调用总量
    tools_used: int
    model_steps: int
    audits: list[dict[str, Any]]
    draft: str                 # 模型最近一次文本
    decision: dict[str, Any] | None
    escalated: bool
    stop_reason: str           # "" / gate_paused / model_unavailable / budget / escalated / model_error / done
    pending_calls: list[dict[str, Any]]
    persist_usage: bool


def build_system_prompt(order_id: str) -> str:
    """对话系统提示。**精简**（长提示词是成本大头），只保留必要约束。"""
    return (
        f"你是电商平台的售后助手，正在处理订单 {order_id} 的「仅退款」咨询。\n"
        "职责：理解买家诉求、按需调用工具核实事实、必要时追问，最后把结论如实转达。\n"
        "规则：\n"
        "1. 先核实事实再下结论，可用 query_order / query_logistics / query_case_progress。\n"
        "2. 提交判定前必须收齐三项：诉求分类、问题对象、期望方案；缺任一项先追问，不要调用 submit_for_decision。\n"
        "3. 判定结论由系统给出，你**不得改写结论**，只需如实转达并说明依据。\n"
        "4. 金额与物流状态以系统数据为准；你不能修改规则、也不能直接退款。\n"
        "5. 买家要求转人工、或你无法处理时，调用 escalate_to_human。\n"
        "6. 回复简洁口语化，面向普通买家，不暴露内部规则编号或技术细节。"
    )


# ============================================================
# 节点
# ============================================================


async def _guard(state: AgentState) -> dict[str, Any]:
    """模型调用前的安全边界：成本闸门 / 模型可用性 / 转人工后停止。

    工具预算**不在这里判**——预算只限制「工具执行」（在 `_model_step` 内判定），
    以便模型在预算耗尽后仍能生成一次收尾回复。
    """
    if state.get("escalated"):
        return {"stop_reason": "escalated"}

    # 成本闸门：**每次模型调用前**预检（P1 设计 §六 6.3）
    gate = await cost_gate.precheck(ongoing=True)
    if gate.paused:
        logger.warning("成本闸门暂停，停止对话循环：%s", gate.reason)
        return {"stop_reason": "gate_paused"}

    if state.get("model") is None:
        return {"stop_reason": "model_unavailable"}

    if state.get("model_steps", 0) >= MAX_MODEL_STEPS:
        return {"stop_reason": "budget"}

    return {}


def _route_after_guard(state: AgentState) -> str:
    return END if state.get("stop_reason") else "model_step"


async def _model_step(state: AgentState) -> dict[str, Any]:
    """调用模型一轮；**调用后立即记账**（前置预检在 guard）。"""
    model = state["model"]
    ctx = state["ctx"]
    messages = list(state.get("messages") or [])

    try:
        turn = await model.step(messages, TOOL_SPECS)
    except Exception as exc:  # noqa: BLE001 —— 模型异常由循环降级，不炸整个会话
        logger.warning("模型调用失败：%s: %s", type(exc).__name__, exc)
        return {"stop_reason": "model_error", "model_steps": state.get("model_steps", 0) + 1}

    # 记账（失败由 cost_gate 内部降级为内存兜底，不阻断对话）
    await cost_gate.record_usage(
        turn.input_tokens,
        turn.output_tokens,
        case_id=ctx.case_id,
        seq=ctx.last_seq,
        persist=state.get("persist_usage", True),
    )

    assistant_msg: dict[str, Any] = {"role": "assistant", "content": turn.content}
    pending: list[dict[str, Any]] = []
    if turn.has_tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in turn.tool_calls
        ]
        pending = assistant_msg["tool_calls"]

    out: dict[str, Any] = {
        "messages": messages + [assistant_msg],
        "draft": turn.content,
        "pending_calls": pending,
        "model_steps": state.get("model_steps", 0) + 1,
    }
    # 工具预算已尽：**不再执行工具**（安全边界）；若模型仍要调工具，
    # 则本轮文本即为收尾，并标记原因。注意：预算只限制"工具执行"，
    # **不剥夺模型生成最终回复的机会**——否则用户会收到空回复。
    if pending and state.get("tools_used", 0) >= state.get("tool_budget", 0):
        out["stop_reason"] = "budget"
    return out


def _route_after_model(state: AgentState) -> str:
    if state.get("stop_reason"):
        return END
    return "tool_step" if state.get("pending_calls") else END


async def _tool_step(state: AgentState) -> dict[str, Any]:
    """执行模型**自主选择**的工具，并把结果如实回灌。"""
    ctx = state["ctx"]
    messages = list(state.get("messages") or [])
    audits = list(state.get("audits") or [])
    used = state.get("tools_used", 0)
    decision = state.get("decision")
    escalated = bool(state.get("escalated"))
    budget = state.get("tool_budget", 0)

    for call in state.get("pending_calls") or []:
        if used >= budget:
            break  # 超预算→不再执行剩余调用（边界由 guard 收口）
        result = await tools.dispatch(call.get("name", ""), call.get("arguments") or {}, ctx)
        used += 1
        audits.append(result.to_audit())
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": call.get("name", ""),
                "content": result.content,
            }
        )
        if result.decided:
            decision = {k: v for k, v in result.data.items() if k != "summary"}
        if result.escalated:
            escalated = True

    return {
        "messages": messages,
        "audits": audits,
        "tools_used": used,
        "decision": decision,
        "escalated": escalated,
        "pending_calls": [],
    }


# ============================================================
# 编译（模块级单例）
# ============================================================

_builder = StateGraph(AgentState)
_builder.add_node("guard", _guard)
_builder.add_node("model_step", _model_step)
_builder.add_node("tool_step", _tool_step)
_builder.set_entry_point("guard")
_builder.add_conditional_edges("guard", _route_after_guard, {"model_step": "model_step", END: END})
_builder.add_conditional_edges("model_step", _route_after_model, {"tool_step": "tool_step", END: END})
_builder.add_edge("tool_step", "guard")

#: 对话循环子图（进程内只编译一次）
AGENT_GRAPH = _builder.compile()


async def run_agent(
    *,
    model: Any,
    ctx: tools.ToolContext,
    history: list[dict[str, Any]],
    tool_budget: int | None = None,
    persist_usage: bool = True,
) -> dict[str, Any]:
    """跑一次对话往返。返回终态 state（`draft` / `decision` / `escalated` / `audits` / `stop_reason`）。

    `history` 是**内部消息格式**（system + 历史买家/智能体消息），由调用方装配。
    """
    budget = settings.max_tools_per_round if tool_budget is None else tool_budget
    init: AgentState = {
        "model": model,
        "ctx": ctx,
        "messages": list(history),
        "tool_budget": budget,
        "tools_used": 0,
        "model_steps": 0,
        "audits": [],
        "draft": "",
        "decision": None,
        "escalated": False,
        "stop_reason": "",
        "pending_calls": [],
        "persist_usage": persist_usage,
    }
    return await AGENT_GRAPH.ainvoke(init)
