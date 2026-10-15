"""口径问答：把"为什么这么判"用业务语言讲清楚。

设计原则（方案 §12 RAG 边界）：
1. **事实来自规则，不来自模型**——判定依据全部由 `hit_details`（规则 + 字段 + 运算符 + 阈值 + 实际值）
   确定性生成；模型至多负责**措辞润色**，不得改写事实。
2. **默认零成本**——确定性解释 + 关键词口径检索即可完成全部问答；
   仅当 `QA_LLM_POLISH=true` 且 `LLM_ENABLED=true` 时才调用模型润色（计费，纪律 10）。
3. **不参与判定**——本模块是纯只读侧路，不写状态、不触发节点、不改变任何结论。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.graph.builder import get_graph
from app.graph.queries import STATUS_PENDING_REVIEW, get_thread_state
from app.llm.client import get_text_llm, text_available
from app.qa.knowledge import KnowledgeEntry, get_knowledge_base, search
from app.qa.labels import describe_conclusion, describe_leaf, format_value

logger = logging.getLogger(__name__)

# 边界声明：随每次回答返回，避免被当成"判定依据咨询"而误解系统能力
BOUNDARY = "本问答入口仅用于解释与释疑，不参与资格判定，也不会改变任何判定结论。"

# 润色调用的超时（秒）。超时即回落到确定性回答——润色是锦上添花，不能拖慢接口。
_POLISH_TIMEOUT = 15.0

# 复核挂起原因码 → 业务说明
_HOLD_REASONS: dict[str, str] = {
    "rule_to_human": "命中转人工规则，等待人工复核",
    "disposal_failed": "处置未能落地，升级人工处理（可重试处置）",
}

# 复核选项码 → 业务名称（与前端 DECISION_META 保持一致）
_OPTION_LABELS: dict[str, str] = {
    "maintain": "维持",
    "override": "改判",
    "retry": "重试处置",
}


def _describe_option(option: Any) -> str:
    """把复核选项描述成业务话术。

    载荷里的选项可能是**字符串码**（`"maintain"`）也可能是含 label 的字典，
    两种形态都要兼容——否则一处形态变化就会让整段解释降级。
    """
    if isinstance(option, dict):
        decision = str(option.get("decision") or "")
        return str(option.get("label") or _OPTION_LABELS.get(decision, decision))
    return _OPTION_LABELS.get(str(option), str(option))


# ============================================================
# 事实解释（确定性，零模型调用）
# ============================================================
async def explain_order(order_id: str, *, graph: Any | None = None) -> dict[str, Any] | None:
    """对某个受理号生成"判定依据"解释。查不到返回 None。

    `graph` 可注入（测试用临时图）；不传则用进程内单例图（生产路径）。
    允许注入是必要的：否则本函数只能查生产检查点，**无法单元测试**。
    """
    if graph is None:
        graph = await get_graph()
    state = await get_thread_state(graph, order_id)
    if state is None:
        return None

    hit_details = state.get("hit_details") or []
    rule_explanations: list[str] = []
    for detail in hit_details:
        leaves = "；".join(describe_leaf(leaf) for leaf in (detail.get("leaves") or []))
        rule_id = detail.get("rule_id", "")
        description = detail.get("description", "")
        rule_explanations.append(f"{rule_id}（{description}）：{leaves}" if leaves else f"{rule_id}（{description}）")

    conclusion_text = describe_conclusion(state.get("conclusion"))
    lines: list[str] = [f"受理号 {order_id} 的判定结论是：**{conclusion_text}**。"]

    facts_line = (
        f"申请退款金额 ¥{float(state.get('claim_amount') or 0):,.2f}，"
        f"退款原因：{format_value('refund_reason_code', state.get('refund_reason_code'))}，"
        f"有效举证图片 {state.get('image_count')} 张。"
    )
    lines.append(facts_line)

    if rule_explanations:
        lines.append("判定依据（逐条规则）：")
        lines.extend(f"- {item}" for item in rule_explanations)
    else:
        lines.append("未命中任何规则，按「数据不足不猜测」原则兜底转人工。")

    # 当前环节状态
    if state.get("status") == STATUS_PENDING_REVIEW:
        interrupt = (state.get("interrupt") or {}).get("value") or {}
        reason = interrupt.get("reason")
        lines.append(f"当前状态：**待人工复核**（{_HOLD_REASONS.get(reason, reason or '等待人工处理')}）。")
        options = interrupt.get("options") or []
        if options:
            lines.append(f"可执行操作：{'、'.join(_describe_option(opt) for opt in options)}。")
    else:
        lines.append("当前状态：流程已完成。")

    # 处置与复核
    action = state.get("action")
    if action:
        if state.get("action_executed"):
            lines.append(f"处置：已执行 {action}。")
        else:
            lines.append(
                f"处置：{action} **未落地**（影子模式或未授权），仅记录待执行动作。"
            )
    if state.get("review_decision"):
        decision_text = {"maintain": "维持", "override": "改判", "retry": "重试处置"}.get(
            str(state["review_decision"]), str(state["review_decision"])
        )
        reviewer = state.get("reviewer") or "未署名"
        lines.append(f"人工复核：{reviewer} 选择「{decision_text}」。")

    return {
        "order_id": order_id,
        "conclusion": state.get("conclusion"),
        "conclusion_text": conclusion_text,
        "hit_rules": list(state.get("hit_rules") or []),
        "rule_explanations": rule_explanations,
        "status": state.get("status"),
        "narrative": "\n".join(lines),
    }


# ============================================================
# 回答组装
# ============================================================
def _render_entry(entry: KnowledgeEntry) -> str:
    """渲染一条口径（标题 + 正文）。"""
    return f"【{entry.title}】\n{entry.answer}"


def _related_by_rules(hit_rules: list[str], exclude: set[str], limit: int) -> list[KnowledgeEntry]:
    """按命中规则反查口径条目：让"为什么这笔转人工"在无关键词时也能命中正确口径。"""
    if not hit_rules or limit <= 0:
        return []
    wanted = set(hit_rules)
    result: list[KnowledgeEntry] = []
    for entry in get_knowledge_base().entries:
        if entry.id in exclude:
            continue
        if wanted.intersection(entry.related_rules):
            result.append(entry)
            if len(result) >= limit:
                break
    return result


def _fill_keyword_matches(
    question: str, hit_rules: set[str], chosen: set[str], limit: int
) -> list[KnowledgeEntry]:
    """关键词检索补齐剩余名额。

    ⚠️ 精度规则：**绑定了规则、但那些规则并未命中**的口径条目会被排除 ——
    否则问"这笔为什么转人工"会召回一堆本案用不到的规则说明（关键词"转人工"命中的噪声）。
    未绑定规则（`related_rules` 为空）的通用口径仍可入选，它们对任何问题都有解释价值。
    """
    result: list[KnowledgeEntry] = []
    for entry, _score in search(question, limit=limit * 3):
        if len(result) >= limit:
            break
        if entry.id in chosen:
            continue
        if entry.related_rules and not (set(entry.related_rules) & hit_rules):
            continue  # 绑定了未命中的规则 → 与本案无关，跳过
        result.append(entry)
        chosen.add(entry.id)
    return result


def _fallback_hint() -> str:
    """无任何匹配时，给出"可以问什么"的引导（而不是沉默）。"""
    titles = [entry.title for entry in get_knowledge_base().entries]
    preview = "\n".join(f"- {t}" for t in titles[:8])
    return (
        "没有匹配到相关口径。可以换个问法，或参考以下主题：\n"
        f"{preview}\n"
        "也可以在提问时附带受理号，我会直接解释这笔申请的判定依据。"
    )


async def _polish(question: str, deterministic: str) -> str | None:
    """可选的模型润色（计费）。未开启闸门、或调用失败时返回 None。"""
    if not (settings.qa_llm_polish and text_available()):
        return None

    llm = get_text_llm()
    if llm is None:
        return None

    prompt = (
        "你是售后客服话术整理助手。下面是一段**已经确定**的系统判定说明与口径，"
        "请在不改变任何事实、数字、规则编号与结论的前提下，把措辞整理得更通顺、更易读。"
        "不得新增判断、不得给出通过/驳回建议、不得省略限制条件。\n\n"
        f"用户问题：{question}\n\n"
        f"待整理内容：\n{deterministic}"
    )
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(llm.invoke, prompt), timeout=_POLISH_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — 润色是旁路，失败即保留原文
        logger.warning("口径润色失败（已回落确定性回答）：%s: %s", type(exc).__name__, exc)
        return None

    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


async def answer_question(
    question: str, *, order_id: str | None = None, graph: Any | None = None
) -> dict[str, Any]:
    """回答一个口径问题；可选带上受理号以附带该笔申请的事实解释。

    返回结构固定包含 `boundary` 字段——把"不参与判定"这条边界随回答一并交付，
    避免使用者误解本入口的职责。
    """
    question = (question or "").strip()

    # 1) 事实解释（确定性；查不到受理号不报错，只提示）
    facts: dict[str, Any] | None = None
    notice = ""
    if order_id:
        try:
            facts = await explain_order(order_id, graph=graph)
        except Exception as exc:  # noqa: BLE001 — 查询失败不应让问答整体不可用
            logger.warning("解释受理号失败（已降级）：%s: %s", type(exc).__name__, exc)
            notice = f"（受理号 {order_id} 查询失败，仅回答通用口径。）"
        if facts is None and not notice:
            notice = f"（未找到受理号 {order_id} 的记录，仅回答通用口径。）"

    # 2) 口径召回：
    #    有受理号时**优先按实际命中规则**召回（这才是这笔申请的解释依据），
    #    剩余名额再用关键词检索补齐；无受理号时纯关键词检索。
    limit = settings.qa_max_sources
    if facts is not None:
        hit_rule_set = set(facts.get("hit_rules") or [])
        matches = _related_by_rules(list(hit_rule_set), exclude=set(), limit=limit)
        chosen = {entry.id for entry in matches}
        if len(matches) < limit:
            matches.extend(
                _fill_keyword_matches(question, hit_rule_set, chosen, limit - len(matches))
            )
        # 兜底：若规则相关与通用口径均为空，退回纯关键词检索，避免"什么都没有"
        if not matches:
            matches = [entry for entry, _score in search(question, limit=limit)]
    else:
        matches = [entry for entry, _score in search(question, limit=limit)]

    # 3) 组装（确定性文本）
    sections: list[str] = []
    if facts is not None:
        sections.append(facts["narrative"])
    if matches:
        sections.append("相关口径：\n" + "\n\n".join(_render_entry(entry) for entry in matches))
    if not sections:
        sections.append(_fallback_hint())
    deterministic_answer = "\n\n".join(sections) + notice

    # 4) 可选润色（计费；失败即保留确定性文本）
    polished = await _polish(question, deterministic_answer)
    answer = polished or deterministic_answer

    return {
        "question": question,
        "answer": answer,
        "order_id": order_id,
        "facts": facts,
        "sources": [
            {"id": entry.id, "title": entry.title, "related_rules": list(entry.related_rules)}
            for entry in matches
        ],
        "related_rules": sorted({r for entry in matches for r in entry.related_rules}),
        # 是否真的用了模型：便于核对"计费调用是否发生"
        "model_used": polished is not None,
        "boundary": BOUNDARY,
    }
