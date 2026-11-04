"""卖家登录的 HTTP 端点（前缀 `/api/seller/auth`）。

三个端点：

- `POST /login` —— 账号密码换 Token（**唯一不需要登录态的接口**）；
- `POST /logout` —— 立即失效当前会话；
- `GET  /me` —— 前端启动时用它判断"要不要弹登录窗"。

Token 的传递方式（`X-Seller-Token` 请求头）与取舍见 `app/seller/deps.py` 文件头。
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.seller import auth
from app.seller.deps import SellerSession, require_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seller/auth", tags=["商铺经营Agent · 登录"])


class LoginPayload(BaseModel):
    """登录请求体。`password` 不落日志、不回显。"""

    username: str
    password: str


def _fail(exc: auth.AuthError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/login")
async def login(payload: LoginPayload) -> dict[str, Any]:
    """登录成功返回 Token 与**本次登录专属的助手线程号**（前端据此加载空白对话）。"""
    try:
        result = await auth.login(payload.username, payload.password)
    except auth.AuthError as exc:
        logger.info("登录失败：%s（%s）", payload.username, exc.code)
        _fail(exc)
    return result


@router.post("/logout")
async def logout(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """退出登录。**真失效**（写 `revoked_at`），不是"前端删掉 Token 就算"。"""
    ok = await auth.logout(session.token)
    return {"ok": ok}


@router.get("/me")
async def me(session: SellerSession = Depends(require_session)) -> dict[str, Any]:
    """当前登录态。无效时 `require_session` 直接 401 —— 前端据此弹登录窗。"""
    return {
        "seller": {
            "username": session.username,
            "seller_id": session.seller_id,
            "display_name": session.display,
        },
        "thread_id": session.thread_id,
    }
