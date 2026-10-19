"""售后域的助手工具 —— **复用现有指挥台能力，一行逻辑都不重写**。

为什么强调复用：助手在售后域做的事，必须与"人在指挥台里点出来的事"**完全一致** ——
同一套校验、同一套留痕、同一套状态机。若这里另写一套，两处必然分叉，
最后变成"指挥台里退不了的单，助手能退"这类事故。

因此本模块只做两件事：把 `app.console` 的返回值转成模型可读的话；把异常转成 `ToolResult`。
"""

from __future__ import annotations

from typing import Any

from app import console
from app.assistant.registry import (
    DOMAIN_AFTERSALE,
    AssistantContext,
    ToolResult,
    ToolSpec,
    register,
)

_STATUS_LABELS = {
    "queued": "待处理", "agent_running": "智能体处理中", "awaiting_confirm": "待确认",
    "resolved": "已完结", "escalated": "已转人工",
}


def _case_line(c: dict[str, Any]) -> str:
    return (f"{c.get('case_id')} · {c.get('order_id') or ''} · "
            f"{_STATUS_LABELS.get(c.get('workflow_status'), c.get('workflow_status'))} · "
            f"处理者 {c.get('assignee') or '—'}")


def _fail(message: str) -> ToolResult:
    return ToolResult(content=f"操作未成功：{message}", ok=False)


def _console_fail(exc: console.ConsoleError) -> ToolResult:
    return _fail(exc.message)


# ============================================================
# 工具
# ============================================================


async def _list_cases(ctx: AssistantContext, status: str = "") -> ToolResult:
    items = await console.list_cases(status or None, limit=30)
    if not items:
        return ToolResult("当前没有符合条件的售后工单。", {"count": 0})
    body = "\n".join(_case_line(c) for c in items)
    return ToolResult(f"共 {len(items)} 条工单：\n{body}", {"count": len(items)})


async def _get_case(ctx: AssistantContext, case_id: str) -> ToolResult:
    try:
        detail = await console.get_detail(case_id)
    except console.ConsoleError as exc:
        return _console_fail(exc)
    case = detail.get("case") or {}
    msgs = detail.get("messages") or []
    tail = msgs[-4:]
    body = "\n".join(f"  {m.get('role')}: {str(m.get('content') or '')[:80]}" for m in tail)
    return ToolResult(
        f"工单 {case.get('case_id')}：{_STATUS_LABELS.get(case.get('workflow_status'), case.get('workflow_status'))}"
        f" · 处理者 {case.get('assignee') or '—'} · 转人工原因 {case.get('escalated_reason') or '—'}\n"
        f"最近对话（{len(msgs)} 条中的最后 {len(tail)} 条）：\n{body}",
        {"case_id": case.get("case_id"), "workflow_status": case.get("workflow_status")},
    )


async def _resume_case(ctx: AssistantContext, case_id: str) -> ToolResult:
    try:
        await console.resume_agent(case_id)
    except console.ConsoleError as exc:
        return _console_fail(exc)
    return ToolResult(f"工单 {case_id} 已退回智能体继续处理。", {"case_id": case_id})


async def _close_case(ctx: AssistantContext, case_id: str, note: str = "") -> ToolResult:
    try:
        await console.close_case(case_id, note or "由 AI 助手结案")
    except console.ConsoleError as exc:
        return _console_fail(exc)
    return ToolResult(f"工单 {case_id} 已人工结案。", {"case_id": case_id})


# ============================================================
# 影响预览
# ============================================================


async def _impact_case(ctx: AssistantContext, args: dict[str, Any]) -> str:
    case_id = str(args.get("case_id") or "")
    try:
        detail = await console.get_detail(case_id)
    except console.ConsoleError:
        return f"工单 {case_id} 不存在，确认后将直接失败。"
    case = detail.get("case") or {}
    return (f"工单 {case_id}：{_STATUS_LABELS.get(case.get('workflow_status'), case.get('workflow_status'))}"
            f" · 处理者 {case.get('assignee') or '—'}")


# ============================================================
# 注册
# ============================================================


_REGISTERED: set[str] = set()


def register_all() -> None:
    """注册售后域工具（幂等）。"""
    specs = [
        ToolSpec(
            name="list_aftersale_cases", domain=DOMAIN_AFTERSALE,
            description="查询售后工单列表（可按状态筛选：queued 待处理 / agent_running 处理中 / "
                        "awaiting_confirm 待确认 / escalated 已转人工 / resolved 已完结）。只读。",
            parameters={"type": "object", "properties": {
                "status": {"type": "string",
                           "enum": ["queued", "agent_running", "awaiting_confirm", "escalated", "resolved"],
                           "description": "按工单状态筛选（可选；不传为全部）"},
            }, "required": []},
            handler=_list_cases,
        ),
        ToolSpec(
            name="get_aftersale_case", domain=DOMAIN_AFTERSALE,
            description="查看单个售后工单的详情与最近对话。只读。",
            parameters={"type": "object", "properties": {
                "case_id": {"type": "string", "description": "工单号（= 订单号）"},
            }, "required": ["case_id"]},
            handler=_get_case,
        ),
        ToolSpec(
            name="resume_aftersale_case", domain=DOMAIN_AFTERSALE, destructive=True,
            summary_template="把工单 {case_id} 退回智能体",
            description=("把「已转人工」的售后工单退回智能体继续处理。"
                         "**会改变工单归属，需要卖家确认。**买家主动要求人工的工单会被拒绝。"),
            parameters={"type": "object", "properties": {
                "case_id": {"type": "string", "description": "工单号（必填）"},
            }, "required": ["case_id"]},
            handler=_resume_case, impact_builder=_impact_case,
        ),
        ToolSpec(
            name="close_aftersale_case", domain=DOMAIN_AFTERSALE, destructive=True,
            summary_template="把工单 {case_id} 结案",
            description="把售后工单人工结案。**结案后不再自动流转，需要卖家确认。**",
            parameters={"type": "object", "properties": {
                "case_id": {"type": "string", "description": "工单号（必填）"},
                "note": {"type": "string", "description": "结案备注（可选）"},
            }, "required": ["case_id"]},
            handler=_close_case, impact_builder=_impact_case,
        ),
    ]
    for spec in specs:
        if spec.name in _REGISTERED:
            continue
        register(spec)
        _REGISTERED.add(spec.name)
