"""六个对话工具的实现 —— 模型**自主**选择调用的能力。

红线（P1 实施设计 §七）：
- **禁止暴露**给模型：退款 / 打款 / 改规则 / 改阈值 / 任意 SQL / 任意 URL 抓取。
  执行层用 `tool_spec.is_allowed_tool()` **二次校验**——不依赖模型守规矩。
- 每个工具都留审计：入参摘要、耗时、结果摘要、成败（由 agent 汇总写入消息的 `tool_calls`）。
- **失败不得谎报成功，也不得编造数据**：取不到就返回 `available=false`。
- `case_id` 由执行层通过 `ToolContext` 注入，模型表达不出"指定别的工单"。

统一返回 `ToolResult`：
- `content` —— **回灌给模型**的文本（模型据此决定下一步）；
- `data`    —— 给编排层 / 回复层用的结构化数据（如判定结论）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.chat import session as session_mod
from app.chat import slots as slots_mod
from app.chat import state
from app.chat.tool_spec import (
    TOOL_ESCALATE_TO_HUMAN,
    TOOL_QUERY_CASE_PROGRESS,
    TOOL_QUERY_LOGISTICS,
    TOOL_QUERY_ORDER,
    TOOL_REGISTER_EVIDENCE,
    TOOL_SUBMIT_FOR_DECISION,
    is_allowed_tool,
)
from app.db import chat_repo as repo
from app.rules.engine import DEFAULT_CONCLUSION
from app.tools.logistics import fetch_logistics
from app.tools.order import fetch_order

logger = logging.getLogger(__name__)

# 单次登记的举证材料数量上限（防模型塞进超长列表）
MAX_EVIDENCE_REFS = 20

# 转人工原因码 → 状态机原因（保持与 state.ESCALATE_* 语义一致）
_ESCALATE_MAP = {
    "buyer_request": state.ESCALATE_BUYER_REQUEST,
    "cannot_help": state.ESCALATE_TOOL_FAILED,
    "dispute": state.ESCALATE_RULE_TO_HUMAN,
}

#: 判定 runner：`(order_id, request) -> (summary, idempotent_replay)`。
#: 默认实现走既有判定图；测试可注入桩，从而**离线跑通"提交判定"环节**。
DecisionRunner = Callable[[str, Any], Awaitable[tuple[dict[str, Any], bool]]]


@dataclass
class ToolContext:
    """一次工具调用所需的会话上下文（由 agent 循环构造）。"""

    case_id: str
    order_id: str
    claim_images: list[str] = field(default_factory=list)
    requested_amount: float | None = None      # 买家主张金额（来自买家消息，非模型推断）
    buyer_note: str = ""
    last_seq: int = 0                          # 当前最新消息序号（槽位的 source_seq）
    decision_runner: DecisionRunner | None = None


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool
    name: str
    content: str                               # 回灌给模型的文本
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_ms: int = 0
    escalated: bool = False                    # 是否已转人工（循环据此收尾）
    decided: bool = False                      # 是否产出了判定结论

    def to_audit(self) -> dict[str, Any]:
        """裁剪为审计摘要（写入消息的 `tool_calls`，供卷宗追溯）。"""
        return {
            "tool": self.name,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "summary": self.data.get("summary") or "",
        }


# ============================================================
# 辅助
# ============================================================


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _ok(name: str, content: str, started: float, **kwargs: Any) -> ToolResult:
    return ToolResult(ok=True, name=name, content=content, elapsed_ms=_elapsed(started), **kwargs)


def _fail(name: str, message: str, started: float, *, error: str = "tool_failed") -> ToolResult:
    return ToolResult(
        ok=False,
        name=name,
        content=_dump({"ok": False, "error": error, "message": message}),
        error=error,
        elapsed_ms=_elapsed(started),
    )


def _iso(value: Any) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if hasattr(value, "isoformat") else None


# ============================================================
# 读工具（3 个）
# ============================================================


async def query_order(ctx: ToolContext) -> ToolResult:
    """查订单事实。取不到返回 `available=false` —— **不编造**。"""
    started = time.perf_counter()
    try:
        order = await fetch_order(ctx.order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_order 异常：%s: %s", type(exc).__name__, exc)
        return _fail(TOOL_QUERY_ORDER, "订单服务暂时不可用", started, error="order_unavailable")

    if order is None:
        return _ok(
            TOOL_QUERY_ORDER,
            _dump({"available": False, "message": "未获取到订单信息"}),
            started,
            data={"available": False, "summary": "订单未获取到"},
        )

    payload = {
        "available": True,
        "order_id": order.order_id,
        "item_category": order.item_category,
        "pay_amount": order.pay_amount,
        "logistics_status": getattr(order.logistics_status, "value", order.logistics_status),
        "signed_at": _iso(order.signed_at),
    }
    return _ok(
        TOOL_QUERY_ORDER,
        _dump(payload),
        started,
        data={**payload, "summary": f"实付 ¥{order.pay_amount}，物流 {payload['logistics_status']}"},
    )


async def query_logistics(ctx: ToolContext) -> ToolResult:
    """查物流轨迹。取不到返回 `available=false` —— **不编造**。"""
    started = time.perf_counter()
    try:
        info = await fetch_logistics(ctx.order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_logistics 异常：%s: %s", type(exc).__name__, exc)
        return _fail(TOOL_QUERY_LOGISTICS, "物流服务暂时不可用", started, error="logistics_unavailable")

    if info is None:
        return _ok(
            TOOL_QUERY_LOGISTICS,
            _dump({"available": False, "message": "未获取到物流信息"}),
            started,
            data={"available": False, "summary": "物流未获取到"},
        )

    status = getattr(info.logistics_status, "value", info.logistics_status)
    payload = {
        "available": True,
        "logistics_status": status,
        "signed_at": _iso(info.signed_at),
        "traces": list(info.traces)[:10],
    }
    return _ok(
        TOOL_QUERY_LOGISTICS,
        _dump(payload),
        started,
        data={**payload, "summary": f"物流状态 {status}"},
    )


async def query_case_progress(ctx: ToolContext) -> ToolResult:
    """查工单进度：阶段、已登记材料、剩余轮次/时限。只读。"""
    started = time.perf_counter()
    case = await repo.get_case(ctx.case_id)
    if case is None:
        return _fail(TOOL_QUERY_CASE_PROGRESS, "工单不存在", started, error="case_not_found")

    slot_rows = await repo.list_slots(ctx.case_id)
    evidence = ""
    for row in slot_rows:
        if row.get("slot") == slots_mod.SLOT_EVIDENCE:
            evidence = str(row.get("value") or "")
            break

    from app.config import settings

    payload = {
        "status": case.get("status"),
        "round": int(case.get("round") or 0),
        "max_rounds": settings.max_rounds,
        "evidence": evidence,
        "timeout_at": case.get("timeout_at"),
    }
    return _ok(
        TOOL_QUERY_CASE_PROGRESS,
        _dump(payload),
        started,
        data={**payload, "summary": f"阶段 {payload['status']}，已用 {payload['round']}/{payload['max_rounds']} 轮"},
    )


# ============================================================
# 写工具（1 个）
# ============================================================


async def register_evidence(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """登记举证材料。**幂等**：同一引用不重复登记；不做任何真伪/质量判断。"""
    started = time.perf_counter()
    raw = arguments.get("evidence_refs")
    refs = [str(item).strip() for item in raw if str(item).strip()] if isinstance(raw, list) else []
    refs = refs[:MAX_EVIDENCE_REFS]
    if not refs:
        return _fail(TOOL_REGISTER_EVIDENCE, "未提供任何材料引用", started, error="empty_evidence")

    existing: list[str] = []
    for row in await repo.list_slots(ctx.case_id):
        if row.get("slot") == slots_mod.SLOT_EVIDENCE and row.get("value"):
            existing = [item for item in str(row["value"]).split(",") if item]
            break

    merged = list(existing)
    for ref in refs:
        if ref not in merged:
            merged.append(ref)

    stored = ",".join(merged)[:500]
    saved = await repo.upsert_slot(
        ctx.case_id, slots_mod.SLOT_EVIDENCE, stored, confidence=1.0, source_seq=ctx.last_seq
    )
    if not saved:
        return _fail(TOOL_REGISTER_EVIDENCE, "材料登记失败，请重试", started, error="persist_failed")

    # 供后续 submit_for_decision 使用（权威数据 + 已登记材料）
    ctx.claim_images = merged
    new_count = len(merged) - len(existing)
    payload = {"ok": True, "registered": len(merged), "new": new_count, "refs": merged}
    return _ok(
        TOOL_REGISTER_EVIDENCE,
        _dump(payload),
        started,
        data={**payload, "summary": f"已登记材料 {len(merged)} 项"},
    )


# ============================================================
# 判定工具（1 个）
# ============================================================


async def _default_decision_runner(order_id: str, request: Any) -> tuple[dict[str, Any], bool]:
    """默认判定 runner：调用**既有判定图**（L3 原样复用），并做**幂等**保护。

    与 `/api/requests` 一致：`thread_id = 受理号`；已存在状态则直接返回既有结论，
    不重新判定（重复判定会累加命中规则、重复写审计、重复触发处置）。
    """
    from app.graph.builder import get_graph
    from app.graph.queries import get_thread_state, summarize_result, thread_config

    graph = await get_graph()
    existing = await get_thread_state(graph, order_id)
    if existing is not None:
        return existing, True
    result = await graph.ainvoke({"request": request}, config=thread_config(order_id))
    return summarize_result(result), False


async def submit_for_decision(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """提交规则引擎判定。

    **必填缺失 → 直接拒绝并要求追问**（不猜测、不提交）；权威字段（金额 / 物流状态）
    以取数层为准，槽位只提供"原因 / 诉求"类字段。
    """
    started = time.perf_counter()

    raw_slots = {
        key: arguments.get(key)
        for key in (
            slots_mod.SLOT_CLAIM_TYPE,
            slots_mod.SLOT_ISSUE_OBJECT,
            slots_mod.SLOT_EXPECTATION,
            slots_mod.SLOT_SYMPTOM,
            slots_mod.SLOT_AFFECTED_QTY,
            slots_mod.SLOT_NOTE,
        )
    }
    cleaned = slots_mod.clean_slots(raw_slots)
    missing = slots_mod.missing_required(cleaned)
    if missing:
        payload = {
            "ok": False,
            "error": "slots_missing",
            "missing": missing,
            "hint": "必填项缺失，请先向买家追问补齐，不要重复提交。",
        }
        return ToolResult(
            ok=False, name=TOOL_SUBMIT_FOR_DECISION, content=_dump(payload),
            error="slots_missing", elapsed_ms=_elapsed(started), data=payload,
        )

    order = await fetch_order(ctx.order_id)
    logistics = await fetch_logistics(ctx.order_id)
    assembly = slots_mod.assemble_request(
        order_id=ctx.order_id,
        slots=cleaned,
        order=order,
        logistics=logistics,
        claim_images=ctx.claim_images,
        buyer_note=ctx.buyer_note,
        requested_amount=ctx.requested_amount,
    )
    if not assembly.ok or assembly.request is None:
        payload = {"ok": False, "error": "assembly_failed", "missing": assembly.missing}
        return ToolResult(
            ok=False, name=TOOL_SUBMIT_FOR_DECISION, content=_dump(payload),
            error="assembly_failed", elapsed_ms=_elapsed(started), data=payload,
        )

    # 槽位落库（**必须带 source_seq**，卷宗要能点回原话）
    for key, value in cleaned.items():
        await repo.upsert_slot(ctx.case_id, key, value, source_seq=ctx.last_seq)

    runner = ctx.decision_runner or _default_decision_runner
    try:
        summary, replay = await runner(ctx.order_id, assembly.request)
    except Exception as exc:  # noqa: BLE001 —— 判定失败保留会话状态，可重试
        logger.warning("判定调用失败：%s: %s", type(exc).__name__, exc)
        return _fail(TOOL_SUBMIT_FOR_DECISION, "判定服务暂时不可用，可稍后重试", started, error="decision_failed")

    conclusion = summary.get("conclusion") or DEFAULT_CONCLUSION
    status = summary.get("status") or ""
    needs_human = conclusion == "to_human" or status == "pending_review"

    payload = {
        "ok": True,
        "conclusion": conclusion,
        "status": status,
        "hit_rules": list(summary.get("hit_rules") or []),
        "details": list(summary.get("hit_details") or []),
        "risk_level": summary.get("risk_level"),
        "idempotent_replay": replay,
        "needs_human": needs_human,
        "hint": "以上结论由系统判定，请如实转达给买家，**不要改写结论**。",
    }
    return _ok(
        TOOL_SUBMIT_FOR_DECISION,
        _dump(payload),
        started,
        data={**payload, "summary": f"结论 {conclusion}，命中 {len(payload['hit_rules'])} 条"},
        decided=True,
    )


# ============================================================
# 升级工具（1 个）
# ============================================================


async def escalate_to_human(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """转人工（终态）。**不可静默失败**——否则用户以为已转人工。"""
    started = time.perf_counter()
    code = str(arguments.get("reason_code") or "cannot_help")
    reason = _ESCALATE_MAP.get(code, state.ESCALATE_BUYER_REQUEST)

    try:
        done = await session_mod.escalate(ctx.case_id, reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("转人工失败：%s: %s", type(exc).__name__, exc)
        return _fail(TOOL_ESCALATE_TO_HUMAN, "转人工失败，请重试", started, error="escalate_failed")

    if not done:
        return _fail(TOOL_ESCALATE_TO_HUMAN, "转人工失败，请重试", started, error="escalate_failed")

    payload = {"ok": True, "escalated": True, "reason_code": code, "message": "已转人工，请等待客服接入"}
    return _ok(
        TOOL_ESCALATE_TO_HUMAN,
        _dump(payload),
        started,
        data={**payload, "summary": f"转人工（{code}）"},
        escalated=True,
    )


# ============================================================
# 分派
# ============================================================

_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]] = {
    TOOL_QUERY_ORDER: lambda ctx, args: query_order(ctx),
    TOOL_QUERY_LOGISTICS: lambda ctx, args: query_logistics(ctx),
    TOOL_QUERY_CASE_PROGRESS: lambda ctx, args: query_case_progress(ctx),
    TOOL_REGISTER_EVIDENCE: register_evidence,
    TOOL_SUBMIT_FOR_DECISION: submit_for_decision,
    TOOL_ESCALATE_TO_HUMAN: escalate_to_human,
}


async def dispatch(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """按工具名分派执行。**白名单二次校验**：模型幻觉出的工具名一律拒绝。"""
    started = time.perf_counter()
    if not is_allowed_tool(name):
        logger.warning("模型请求了未授权的工具：%r", name)
        return _fail(name, f"工具 {name} 不在允许清单内", started, error="tool_not_allowed")

    handler = _HANDLERS.get(name)
    if handler is None:
        return _fail(name, f"工具 {name} 未实现", started, error="tool_not_implemented")

    try:
        return await handler(ctx, arguments or {})
    except Exception as exc:  # noqa: BLE001 —— 工具异常不得炸掉整个循环
        logger.warning("工具 %s 执行异常：%s: %s", name, type(exc).__name__, exc)
        return _fail(name, "工具执行出错，请换一种方式或转人工", started, error=type(exc).__name__)
