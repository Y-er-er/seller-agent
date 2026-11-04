"""AI 经营助手的 HTTP 端点（前缀 `/api/seller/assistant`）。

⚠️ 计费提示：`/chat` 每次调用都会产生**真实模型调用**（一轮可能多次），
因为助手需要与模型多轮往返才能完成工具调用。这是"能动手的助手"的必要代价。
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.assistant import actions, loop, registry
from app.assistant.registry import AssistantContext, AssistantError
from app.db import assistant_repo as repo
from app.seller.deps import SellerSession, require_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seller/assistant", tags=["商铺经营Agent · AI 助手"])


# ============================================================
# 请求体
# ============================================================


class ContextPayload(BaseModel):
    """界面上下文：让助手知道卖家此刻在看什么、选中了什么。"""

    view: str = ""
    selected_goods_no: str = ""


class ChatPayload(BaseModel):
    message: str
    context: ContextPayload = Field(default_factory=ContextPayload)


class BatchPayload(BaseModel):
    """批量确认的请求体。`skip_ids` = 卖家在卡上单独划掉、这次不执行的步骤。"""

    skip_ids: list[int] = Field(default_factory=list)


def _ctx(session: SellerSession, context: ContextPayload | None = None) -> AssistantContext:
    """把登录态翻译成助手上下文。

    ⚠️ `thread_id` **来自会话**，不是常量：同一个账号换一次登录就换一条线程，
    因此"每次登录从新对话开始"由数据结构保证。写成常量会让不同账号、不同次登录
    共用同一段历史（曾用 `THREAD_ID = "default"`，就是这种串话的根源）。
    """
    ctx = context or ContextPayload()
    return AssistantContext(
        seller_id=session.seller_id,
        thread_id=session.thread_id,
        view=ctx.view or "",
        selected_goods_no=ctx.selected_goods_no or "",
    )


def _fail(exc: AssistantError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ============================================================
# 对话
# ============================================================


@router.post("/chat")
async def chat(payload: ChatPayload, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """给助手发一句话。

    返回 `reply`（助手回复）、`pending`（**需卖家确认的动作**）、`trace`（工具轨迹）。
    破坏性动作不会在本调用中执行 —— 只会生成待确认项。
    """
    ctx = _ctx(session, payload.context)
    try:
        return await loop.run_turn(ctx, payload.message)
    except Exception as exc:  # noqa: BLE001 —— 兜底：助手异常不该让整个界面 500
        logger.warning("助手对话失败：%s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail=f"助手暂时不可用：{type(exc).__name__}") from exc


@router.get("/messages")
async def messages(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """对话历史（按 seq 升序）。线程来自登录会话 —— 新登录就是新对话。"""
    items = await repo.list_messages(session.thread_id, limit=200)
    return {"items": [m for m in items if m["role"] != "tool"], "thread_id": session.thread_id}


@router.post("/reset")
async def reset(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """开始新对话：清空消息，并作废未决的待确认动作（避免留一个点不动的悬空卡片）。"""
    result = await repo.reset_thread(session.thread_id, session.seller_id)
    return {"ok": True, **result}


# ============================================================
# 巡检（纯聚合，**不调模型**）
# ============================================================


@router.get("/issues/rules")
async def assistant_issue_rules(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """**巡检规则自述**：检查哪些问题、判定标准、以及不检查什么。

    为什么要暴露它：巡检规则是代码写死的，用户看不到边界 ——
    漏报时无法判断"是规则没覆盖"还是"真的没问题"。明示规则，至少让漏报**可知**。
    """
    from app.assistant import scan as scanner

    return scanner.describe_rules()


@router.get("/issues")
async def assistant_issues(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """待办巡检：扫出需要卖家处理的事。

    **纯数据聚合、不调模型**（打开助手时可安全调用，零计费）。
    巡检只提建议，不会执行任何操作 —— 动手仍走工具 + 确认卡。
    """
    from app.assistant import scan as scanner

    try:
        return await scanner.scan(session.seller_id)
    except Exception as exc:  # noqa: BLE001 —— 巡检是旁路，失败不该让界面报错
        logger.warning("巡检失败（降级为空）：%s: %s", type(exc).__name__, exc)
        return {"count": 0, "issues": [], "by_kind": {}}


# ============================================================
# 能力与待确认动作
# ============================================================


@router.get("/capabilities")
async def capabilities(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """能力清单（界面「我能做什么」面板）。按域分组，并标出哪些需要确认。"""
    loop.ensure_tools_registered()
    return {"groups": registry.capabilities()}


@router.get("/actions")
async def list_actions(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """当前待卖家确认的动作（每项带 `batch_id`：界面按批次收成一张卡）。"""
    items = await actions.list_pending(_ctx(session))
    return {"items": items, "total": len(items)}


# ⚠️ 批次路由必须写在 `/actions/{action_id}/...` **之前**：
# 否则 `batch` 会被当成 action_id 去解析，直接 422。
@router.post("/actions/batch/{batch_id}/confirm")
async def confirm_batch(
    batch_id: str,
    payload: BatchPayload | None = None,
    session: SellerSession = Depends(require_session),
) -> dict[str, Any]:
    """**一次确认整批**：逐项执行同一次请求里的所有待确认动作。

    这是"一句话里有多件事"的正确交互 —— 卖家点一次，全部执行；
    不再需要逐张点（逐张点会只做一半、剩下的卡看起来像"又弹出来了"）。
    """
    try:
        return await actions.confirm_batch(
            _ctx(session), batch_id, skip_ids=(payload.skip_ids if payload else [])
        )
    except AssistantError as exc:
        _fail(exc)


@router.post("/actions/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """整批作废 —— 数据零改动。"""
    try:
        return await actions.cancel_batch(_ctx(session), batch_id)
    except AssistantError as exc:
        _fail(exc)


@router.get("/actions/batch/{batch_id}/progress")
async def batch_progress(batch_id: str, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """查一个批次的进度（按状态计数：待确认/执行中/成功/失败/取消）。"""
    return await actions.batch_progress(_ctx(session), batch_id)


@router.post("/actions/batch/{batch_id}/retry")
async def retry_batch(batch_id: str, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """把一批里**失败**的步骤重置回待确认，让卖家重新确认执行（重置后仍需点确认）。"""
    try:
        return await actions.retry_failed(_ctx(session), batch_id)
    except AssistantError as exc:
        _fail(exc)


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: int, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """确认并**真正执行**该动作（这是唯一会执行破坏性工具的入口）。"""
    try:
        return await actions.confirm(_ctx(session), action_id)
    except AssistantError as exc:
        _fail(exc)


@router.post("/actions/{action_id}/cancel")
async def cancel_action(action_id: int, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """取消该动作 —— 数据零改动。"""
    try:
        return await actions.cancel(_ctx(session), action_id)
    except AssistantError as exc:
        _fail(exc)
