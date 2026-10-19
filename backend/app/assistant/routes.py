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
from app.seller.deps import require_seller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seller/assistant", tags=["卖家控制台 · AI 助手"])

#: 单卖家阶段固定线程（多卖家迭代时按卖家隔离）
THREAD_ID = "default"


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


def _ctx(seller_id: str, context: ContextPayload | None = None) -> AssistantContext:
    ctx = context or ContextPayload()
    return AssistantContext(
        seller_id=seller_id,
        thread_id=THREAD_ID,
        view=ctx.view or "",
        selected_goods_no=ctx.selected_goods_no or "",
    )


def _fail(exc: AssistantError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ============================================================
# 对话
# ============================================================


@router.post("/chat")
async def chat(payload: ChatPayload, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """给助手发一句话。

    返回 `reply`（助手回复）、`pending`（**需卖家确认的动作**）、`trace`（工具轨迹）。
    破坏性动作不会在本调用中执行 —— 只会生成待确认项。
    """
    ctx = _ctx(seller_id, payload.context)
    try:
        return await loop.run_turn(ctx, payload.message)
    except Exception as exc:  # noqa: BLE001 —— 兜底：助手异常不该让整个界面 500
        logger.warning("助手对话失败：%s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail=f"助手暂时不可用：{type(exc).__name__}") from exc


@router.get("/messages")
async def messages(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """对话历史（按 seq 升序）。"""
    items = await repo.list_messages(THREAD_ID, limit=200)
    return {"items": [m for m in items if m["role"] != "tool"], "thread_id": THREAD_ID}


@router.post("/reset")
async def reset(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """开始新对话：清空消息，并作废未决的待确认动作（避免留一个点不动的悬空卡片）。"""
    result = await repo.reset_thread(THREAD_ID, seller_id)
    return {"ok": True, **result}


# ============================================================
# 能力与待确认动作
# ============================================================


@router.get("/capabilities")
async def capabilities(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """能力清单（界面「我能做什么」面板）。按域分组，并标出哪些需要确认。"""
    loop.ensure_tools_registered()
    return {"groups": registry.capabilities()}


@router.get("/actions")
async def list_actions(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """当前待卖家确认的动作。"""
    items = await actions.list_pending(_ctx(seller_id))
    return {"items": items, "total": len(items)}


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """确认并**真正执行**该动作（这是唯一会执行破坏性工具的入口）。"""
    try:
        return await actions.confirm(_ctx(seller_id), action_id)
    except AssistantError as exc:
        _fail(exc)


@router.post("/actions/{action_id}/cancel")
async def cancel_action(action_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """取消该动作 —— 数据零改动。"""
    try:
        return await actions.cancel(_ctx(seller_id), action_id)
    except AssistantError as exc:
        _fail(exc)
