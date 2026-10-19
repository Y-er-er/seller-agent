"""AI 助手的对话循环。

形态沿用 `app/chat/agent.py`（guard → model_step → tool_step → guard），但更精简：
助手没有成本闸门、没有转人工，只有两道边界 —— **步数上限**与**工具预算**。

**谁在决策**：模型自主决定调哪个工具、传什么参数、调几次、何时收尾。
代码只做三件事：

1. **破坏性工具转成待确认**（交给 `actions.create_pending`），**不执行**；
2. **未知工具直接拒绝**（即使模型幻觉出一个工具名，也执行不了）；
3. 步数与预算收口（防止无限循环）。

⚠️ 每轮都会产生**计费**调用；助手一轮可能触发多次工具调用 → 多次计费。
"""

from __future__ import annotations

import logging
from typing import Any

from app.assistant import actions, registry, tools_aftersale, tools_goods
from app.assistant.registry import AssistantContext
from app.chat import cost_gate
from app.chat.llm_adapter import get_chat_model
from app.db import assistant_repo as repo

logger = logging.getLogger(__name__)

#: 单轮最多几次模型往返（模型⇄工具）
MAX_STEPS = 6
#: 单轮最多执行几个工具
DEFAULT_TOOL_BUDGET = 8
#: 送进上下文的历史消息条数（只取卖家/助手文本，不含工具过程）
HISTORY_LIMIT = 40

VIEW_LABELS = {
    "overview": "概览",
    "gallery": "商品陈列",
    "admin": "商品管理",
    "audit": "操作日志",
    "aftersale": "售后",
}


def build_system_prompt(ctx: AssistantContext) -> str:
    """助手系统提示。含**界面上下文注入** —— 这是"懂上下文"与"只会聊天"的分界。"""
    lines = [
        "你是卖家经营控制台里的 AI 助手，帮卖家打理「商品」「陈列」「售后」三块事务。",
        "",
        "工作方式：",
        "1. 先弄清意图再动手；信息不足时先问清楚，不要凭猜测改数据。",
        "2. ⚠️ **凡涉及商品或工单「当前状态」的问题（有哪些商品、什么价格、什么状态、多少库存、"
        "有哪些工单），都必须先调用工具实时查询** —— 卖家随时可能改价、上架、删商品，"
        "对话历史里的数据**随时可能已过期**，不得凭历史或记忆回答。"
        "即使同一个问题刚刚问过，也要重新查。",
        "3. 一次可以调用多个工具；工具结果要**如实转达**，不要编造。"
        "⚠️ **卖家一次要求多件事时（如「生成文案并上架」「配张图然后上架」），要把它们做完**，"
        "不要只做一半就停下来汇报。",
        "4. 工具返回失败时，把真实原因告诉卖家，不要含糊其辞或换个说法掩饰。",
        "5. 遇到**不可撤销**（删除、批量改动、工单归属变更）或**会产生费用**（生成宣传图 / 文案 / 定价建议）"
        "的操作，系统会转为「待卖家确认」；此时你只需说明**已请他确认**，绝对不要说「已经完成」。",
        "6. 回复简洁口语化，面向卖家；不要暴露内部工具名、参数格式或技术细节。",
        "7. 商品编号形如 G-0001。卖家说「这件商品」「它」时，指的是当前界面上选中的那件。",
        "8. **`generate_copy` 只产出文案候选、不写库**：若卖家本就是要「更新 / 写入文案」，"
        "生成后请**立即调用 `update_goods` 把这段文案写进去**，不要停在候选让他再说第二遍。"
        "只有卖家只是「先看看」时才停下。",
    ]
    if ctx.view or ctx.selected_goods_no:
        lines += ["", "当前界面上下文（供理解「这件 / 它」的指代）："]
        if ctx.view:
            lines.append(f"- 卖家正在浏览：{VIEW_LABELS.get(ctx.view, ctx.view)}")
        if ctx.selected_goods_no:
            lines.append(f"- 当前选中商品：{ctx.selected_goods_no}")
    return "\n".join(lines)


def _to_model_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把对话历史转成模型消息。

    **只取卖家与助手的文本**，跳过 `role=tool` 的行 —— 工具过程不进上下文：既省 token，
    也避免"tool 消息缺 `tool_call_id`"这类格式问题。

    ⚠️ **代价必须说清**（实测踩到）：跳过 tool 行之后，上下文里就只剩「助手说过的话」。
    当数据在两次提问之间被改动（卖家刚上架 / 改价），模型很容易**把自己上一轮的旧结论当成事实**，
    于是答出过期信息。防线有两条，缺一不可：

    1. system prompt 的硬规则：**涉及当前状态必须实时查工具**；
    2. 这里给"当时由工具结果写回的助手消息"**打上过期标记**，让模型看到明确信号。

    这是"省 token"与"答得对"之间的刻意权衡，不是遗漏。
    """
    out: list[dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if not content:
            continue
        if role == "seller":
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            # 带 `tool_name` 的历史消息 = 当时由工具查询结果写回的，明确标注"可能已过期"
            if msg.get("tool_name"):
                content = f"{content}\n（以上是当时查询的结果，可能已过期；需要时应重新查询）"
            out.append({"role": "assistant", "content": content})
    return out


def ensure_tools_registered() -> None:
    """注册全部工具（幂等）。新增域时在此处加一行即可。"""
    tools_goods.register_all()
    tools_aftersale.register_all()


async def run_turn(ctx: AssistantContext, message: str) -> dict[str, Any]:
    """跑一轮助手对话：落卖家消息 → 循环 → 落助手回复。返回本轮结果。"""
    ensure_tools_registered()

    text = (message or "").strip()
    if not text:
        return {"reply": "请告诉我要做什么。", "pending": [], "trace": [], "stop_reason": "empty"}

    await repo.append_message(ctx.thread_id, "seller", text, seller_id=ctx.seller_id)

    model = get_chat_model()
    if model is None:
        # 模型不可用：**明确告知**，不假装在思考（也不静默失败）
        reply = ("AI 助手当前不可用：模型未启用或未配置密钥。"
                 "请检查 .env 的 LLM_ENABLED 与 DASHSCOPE_API_KEY 后重启服务。")
        await repo.append_message(ctx.thread_id, "assistant", reply, seller_id=ctx.seller_id)
        return {"reply": reply, "pending": [], "trace": [], "stop_reason": "model_unavailable"}

    history = await repo.list_messages(ctx.thread_id, limit=HISTORY_LIMIT)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(ctx)}
    ] + _to_model_messages(history)
    specs = registry.model_specs()

    reply = ""
    stop_reason = "done"
    trace: list[dict[str, Any]] = []
    pending_cards: list[dict[str, Any]] = []
    tools_used = 0

    for _step in range(MAX_STEPS):
        # 成本闸门：助手与售后对话**受同一个闸门约束**（都是真实计费调用）
        gate = await cost_gate.precheck(ongoing=True)
        if gate.paused:
            reply = f"成本闸门已暂停模型调用：{gate.message}"
            stop_reason = "gate_paused"
            break

        try:
            turn = await model.step(messages, specs)
        except Exception as exc:  # noqa: BLE001 —— 模型异常由本层降级，不让整个请求炸掉
            logger.warning("助手模型调用失败：%s: %s", type(exc).__name__, exc)
            reply = "抱歉，我这边调用模型时出错了，请稍后再试。"
            stop_reason = "model_error"
            break

        # 记账：助手调用同样计入用量（case_id 为空 —— 它不属于任何售后工单）
        await cost_gate.record_usage(
            turn.input_tokens, turn.output_tokens, case_id=None, seq=None, persist=True
        )

        if turn.content:
            reply = turn.content

        if not turn.has_tool_calls:
            stop_reason = "done"
            break

        messages.append({
            "role": "assistant",
            "content": turn.content or "",
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls
            ],
        })

        for call in turn.tool_calls:
            if tools_used >= DEFAULT_TOOL_BUDGET:
                content = "已达到本轮工具调用上限，请先用已有信息回复卖家。"
                trace.append({"name": call.name, "args": call.arguments, "ok": False,
                              "summary": "超出本轮工具预算"})
            else:
                content = await _dispatch(ctx, call, trace, pending_cards)
                tools_used += 1
            messages.append({
                "role": "tool", "tool_call_id": call.id, "name": call.name, "content": content,
            })
    else:
        stop_reason = "max_steps"
        if not reply:
            reply = "这件事我还没处理完，需要你再确认一下具体要求。"

    if not reply:
        reply = "我没能理解你的意思，能再说得具体一点吗？"

    await repo.append_message(ctx.thread_id, "assistant", reply, seller_id=ctx.seller_id)
    return {
        "reply": reply, "pending": pending_cards, "trace": trace,
        "stop_reason": stop_reason, "tools_used": tools_used,
    }


async def _dispatch(
    ctx: AssistantContext,
    call: Any,
    trace: list[dict[str, Any]],
    pending_cards: list[dict[str, Any]],
) -> str:
    """执行一个工具调用，返回要回灌给模型的文本。

    三条分支：**未知工具 → 拒绝**；**需要确认的（不可逆 / 计费）→ 转待确认、不执行**；其余 → 直接执行。
    """
    spec = registry.get(call.name)
    args = call.arguments if isinstance(call.arguments, dict) else {}

    if spec is None:
        trace.append({"name": call.name, "args": args, "ok": False, "summary": "未知工具，已拒绝"})
        return f"不存在名为 {call.name} 的工具。请只使用已提供的工具。"

    if spec.needs_confirm:
        card = await actions.create_pending(ctx, spec, args)
        pending_cards.append(card)
        trace.append({"name": spec.name, "args": args, "ok": True,
                      "summary": f"待卖家确认：{card['summary']}"})
        # 理由分两类：不可逆 vs 会产生费用 —— 让模型转达的措辞准确
        reason = "会改变数据且不可撤销" if spec.destructive else "会产生 API 费用"
        return (f"该操作（{card['summary']}）{reason}，"
                f"系统已生成待确认项 #{card['id']}，**在卖家确认之前不会执行**。"
                f"请告知卖家等待其确认，不要说已经完成。")

    try:
        result = await spec.handler(ctx, **args)
        trace.append({"name": spec.name, "args": args, "ok": result.ok, "summary": result.content[:120]})
        return result.content
    except Exception as exc:  # noqa: BLE001 —— 工具异常要如实回灌，让模型能解释给卖家
        logger.warning("工具执行异常（%s）：%s: %s", spec.name, type(exc).__name__, exc)
        trace.append({"name": spec.name, "args": args, "ok": False, "summary": str(exc)[:120]})
        return f"工具执行出错：{type(exc).__name__}: {exc}"
