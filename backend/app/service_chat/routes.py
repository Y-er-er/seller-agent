"""普通咨询 API。公开买家入口只接收消息，不暴露订单数据。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.db import service_repo as repo
from app.config import settings
from app.seller.deps import SellerSession, require_seller, require_session

buyer_router = APIRouter(prefix="/api/service", tags=["买家咨询 · 本地演示"])
seller_router = APIRouter(prefix="/api/seller/service", tags=["商铺经营Agent · 客服"])


class BuyerStart(BaseModel):
    seller_id: str = Field(default="default", min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("消息不能为空")
        return cleaned


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    quick_reply_id: int | None = Field(default=None, ge=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("消息不能为空")
        return cleaned


class Reply(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("title", "content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("快捷回复标题和内容不能为空")
        return cleaned


def _fail(exc: repo.ServiceRepoError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


def _require_demo() -> None:
    """公开买家入口目前只有本地演示身份机制；真实数据模式必须关闭。"""
    if settings.resolved_data_source != "local":
        raise HTTPException(status_code=503, detail="演示咨询入口仅在本地数据模式开放")


@buyer_router.post("/conversations", dependencies=[Depends(_require_demo)])
async def buyer_start(payload: BuyerStart) -> dict[str, Any]:
    try:
        return await repo.create(payload.seller_id, payload.message.strip())
    except repo.ServiceRepoError as exc:
        _fail(exc)


@buyer_router.get("/conversations/{conversation_id}", dependencies=[Depends(_require_demo)])
async def buyer_get(conversation_id: str, x_buyer_token: str = Header(default="", alias="X-Buyer-Token")) -> dict[str, Any]:
    try:
        return await repo.get(conversation_id, token=x_buyer_token)
    except repo.ServiceRepoError as exc:
        _fail(exc)


@buyer_router.post("/conversations/{conversation_id}/messages", dependencies=[Depends(_require_demo)])
async def buyer_message(conversation_id: str, payload: Message, x_buyer_token: str = Header(default="", alias="X-Buyer-Token")) -> dict[str, Any]:
    try:
        return await repo.append(conversation_id, "buyer", payload.text, token=x_buyer_token)
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.get("/conversations")
async def seller_list(status: str = Query(default="", pattern="^(|open|closed)$"), seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return {"items": await repo.list_conversations(seller_id, status=status)}
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.get("/conversations/{conversation_id}")
async def seller_get(conversation_id: str, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await repo.get(conversation_id, seller_id=seller_id)
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.post("/conversations/{conversation_id}/takeover")
async def takeover(conversation_id: str, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await repo.set_state(conversation_id, seller_id, assignee="manual")
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.post("/conversations/{conversation_id}/close")
async def close(conversation_id: str, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await repo.set_state(conversation_id, seller_id, status="closed")
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.post("/conversations/{conversation_id}/messages")
async def seller_message(conversation_id: str, payload: Message, session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    try:
        return await repo.append(conversation_id, "human", payload.text, seller_id=session.seller_id,
                                 author=session.username, reply_id=payload.quick_reply_id)
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.get("/quick-replies")
async def list_replies(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return {"items": await repo.list_replies(seller_id)}
    except repo.ServiceRepoError as exc:
        _fail(exc)


@seller_router.post("/quick-replies")
async def create_reply(payload: Reply, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await repo.create_reply(seller_id, payload.title.strip(), payload.content.strip())
    except repo.ServiceRepoError as exc:
        _fail(exc)
