"""卖家登录的业务逻辑：账号自举、登录、退出、会话解析。

分工（与商品域一致）：**routes 只管 HTTP，规则都在这里**，repo 只管 SQL。

三个刻意的设计：

1. **登录失败一律同一句话**（"账号或密码不正确"）。区分"没这个账号"和"密码错"
   等于给攻击者一个免费的账号枚举接口。
2. **失败计数在进程内**（同账号连续 N 次锁定 M 分钟）。单进程部署够用；
   多副本时各算各的 —— 这是**已知边界**，不假装是分布式限流。
3. **登录成功即分配新的助手线程号**。所以"每次登录默认开启新对话"不是前端约定，
   而是会话本身的性质：换个会话就换个 thread_id，历史消息天然不会串进来。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings
from app.db import seller_repo as repo
from app.seller.passwords import (
    hash_password,
    new_session_token,
    new_thread_id,
    verify_password,
)

logger = logging.getLogger(__name__)

#: 与 `HTTPException` 解耦：routes 只负责把 status/code 翻成响应
class AuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 401, code: str = "auth_failed") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


#: 登录失败计数：`{username: [失败时刻, ...]}`（monotonic 秒）
_FAILS: dict[str, list[float]] = {}


def _normalize(username: str) -> str:
    return (username or "").strip().lower()


def _recent_fails(username: str, window_seconds: float) -> int:
    now = time.monotonic()
    stamps = [t for t in _FAILS.get(username, []) if now - t < window_seconds]
    _FAILS[username] = stamps
    return len(stamps)


def _record_fail(username: str) -> None:
    _FAILS.setdefault(username, []).append(time.monotonic())


def _clear_fails(username: str) -> None:
    _FAILS.pop(username, None)


async def bootstrap() -> dict[str, Any] | None:
    """建表 + 首次启动创建演示账号。

    只在**账号表为空**时创建；已有账号时绝不动密码 —— 否则每次重启都会把
    卖家改过的密码重置回默认值（那是安全事故，不是便捷）。
    """
    await repo.ensure_tables()
    if await repo.count_accounts() > 0:
        return None
    username = settings.seller_bootstrap_username.strip()
    password = settings.seller_bootstrap_password
    if not username or not password:
        logger.warning("未配置 SELLER_BOOTSTRAP_USERNAME/PASSWORD，且账号表为空 —— 无法登录")
        return None
    created = await create_account(
        username=username,
        password=password,
        seller_id=settings.seller_bootstrap_seller_id,
        display_name=settings.seller_bootstrap_name or username,
    )
    logger.info(
        "已创建初始账号 `%s`（绑定 seller_id=%s）。请尽快在 .env 修改默认密码。",
        username, settings.seller_bootstrap_seller_id,
    )
    return created


async def create_account(
    *, username: str, password: str, seller_id: str, display_name: str = ""
) -> dict[str, Any]:
    """新建账号（供自举脚本/运维使用；**不对外开放注册接口**）。

    为什么不开放注册：本项目的数据是按 `seller_id` 隔离的，自助注册出来的新账号
    看到的是**一间空店**（没有商品、没有知识库），对卖家是误导而不是能力。
    需要多账号时用 `scripts/create_seller_account.py` 明确指定归属。
    """
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("账号和密码都不能为空", status_code=400, code="bad_input")
    if await repo.account_exists(username):
        raise AuthError(f"账号已存在：{username}", status_code=409, code="username_taken")
    account_id = await repo.create_account(
        seller_id, username, hash_password(password), display_name or username
    )
    return {"id": account_id, "username": username, "seller_id": seller_id,
            "display_name": display_name or username}


async def login(username: str, password: str) -> dict[str, Any]:
    """校验密码并建立会话。返回 `{token, expires_at, seller, thread_id}`。"""
    key = _normalize(username)
    window = settings.seller_login_lock_minutes * 60
    if _recent_fails(key, window) >= settings.seller_login_max_fails:
        minutes = int(settings.seller_login_lock_minutes)
        raise AuthError(
            f"登录失败次数过多，请 {minutes} 分钟后再试", status_code=429, code="too_many_attempts"
        )

    account = await repo.get_account((username or "").strip())
    if not account or not verify_password(password, account["password_hash"]):
        _record_fail(key)
        raise AuthError("账号或密码不正确", status_code=401, code="bad_credentials")
    if account["status"] != "active":
        raise AuthError("该账号已停用，请联系管理员", status_code=403, code="account_disabled")

    _clear_fails(key)
    token = new_session_token()
    # **每次登录一个新线程** —— 这就是"默认开启新对话"的落点
    thread_id = new_thread_id()
    await repo.create_session(
        token, account["id"], account["seller_id"], thread_id, settings.seller_session_hours
    )
    await repo.touch_login(account["id"])
    logger.info("卖家登录成功：%s（seller_id=%s, thread=%s）",
                account["username"], account["seller_id"], thread_id)
    return {
        "token": token,
        "expires_in_hours": settings.seller_session_hours,
        "thread_id": thread_id,
        "seller": {
            "username": account["username"],
            "seller_id": account["seller_id"],
            "display_name": account["display_name"] or account["username"],
        },
    }


async def logout(token: str) -> bool:
    """退出登录：**立即失效**（revoked_at），不是等它过期。"""
    if not token:
        return False
    return await repo.revoke_session(token)


async def resolve(token: str) -> dict[str, Any] | None:
    """把 Token 解析成会话（含 `seller_id` 与 `thread_id`）；无效返回 None。"""
    if not token:
        return None
    return await repo.get_session(token)
