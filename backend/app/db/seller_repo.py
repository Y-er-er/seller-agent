"""卖家登录的数据访问（账号 + 会话）。

与其它 repo 同一套取向：同步 pymysql + `run_sync` 线程池；**读**失败上抛由调用方决定降级，
**写**（登录、建会话、退出）失败必须上抛 —— 登录失败绝不可当成功。

两张表见 `sql/seller_schema.sql`：

- `seller_account` —— 账号与密码哈希（PBKDF2）。一个账号绑定一个 `seller_id`，
  业务数据（商品 / 任务 / 知识库）早已按 `seller_id` 隔离，登录只负责把"谁"解析成 id。
- `seller_session` —— 登录会话。**携带本次登录专属的助手线程号**，
  所以"每次登录都从新对话开始"是结构决定的，不靠前端记忆。

表不在时自动建（幂等）：DDL 与 `sql/seller_schema.sql` 一致；
标准路径仍是跑 SQL，代码里这份只是兜底，避免"忘跑建表 = 整个控制台 401"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.config import PROJECT_ROOT
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

#: 会话是否有效由**数据库时间**判定（`NOW()`），不读应用进程的时钟 ——
#: 否则容器时区与库时区不一致时，会出现"刚登录就过期"或"退出后还能用"。
_VALID_SESSION_SQL = (
    "SELECT s.token, s.account_id, s.seller_id, s.thread_id, s.expires_at, "
    "       a.username, a.display_name "
    "FROM seller_session s JOIN seller_account a ON a.id = s.account_id "
    "WHERE s.token=%s AND s.revoked_at IS NULL AND s.expires_at > NOW() AND a.status='active'"
)

_TABLES_READY = False


def _ddl_statements() -> list[str]:
    """读取 `sql/seller_schema.sql` 并拆成单条语句。

    只用于"兜底建表"；注释行先剔除，避免把说明文字当 SQL 执行。
    """
    path = PROJECT_ROOT / "sql" / "seller_schema.sql"
    raw = path.read_text(encoding="utf-8")
    body = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def _ensure_tables() -> None:
    """确保两张表存在（幂等，进程内只做一次）。"""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with connect() as conn, conn.cursor() as cur:
        for stmt in _ddl_statements():
            cur.execute(stmt)
        conn.commit()
    _TABLES_READY = True


async def ensure_tables() -> None:
    await run_sync(_ensure_tables)


def _get_account(username: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, seller_id, username, password_hash, display_name, status, last_login_at "
            "FROM seller_account WHERE username=%s",
            (username,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


async def get_account(username: str) -> dict[str, Any] | None:
    return await run_sync(_get_account, username)


def _count_accounts() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM seller_account")
        return int(cur.fetchone()[0])


async def count_accounts() -> int:
    return await run_sync(_count_accounts)


def _create_account(
    seller_id: str, username: str, password_hash: str, display_name: str
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seller_account (seller_id, username, password_hash, display_name) "
            "VALUES (%s, %s, %s, %s)",
            (seller_id, username, password_hash, display_name),
        )
        new_id = int(cur.lastrowid)
        conn.commit()
        return new_id


async def create_account(
    seller_id: str, username: str, password_hash: str, display_name: str
) -> int:
    return await run_sync(_create_account, seller_id, username, password_hash, display_name)


def _touch_login(account_id: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE seller_account SET last_login_at=NOW() WHERE id=%s", (account_id,))
        conn.commit()


async def touch_login(account_id: int) -> None:
    await run_sync(_touch_login, account_id)


def _set_password(account_id: int, password_hash: str) -> bool:
    """改密码（供运维脚本使用）。**不碰 seller_id**，业务数据不受影响。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE seller_account SET password_hash=%s WHERE id=%s",
            (password_hash, account_id),
        )
        changed = cur.rowcount > 0
        conn.commit()
        return changed


async def set_password(account_id: int, password_hash: str) -> bool:
    return await run_sync(_set_password, account_id, password_hash)


def _create_session(
    token: str, account_id: int, seller_id: str, thread_id: str, hours: float
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO seller_session (token, account_id, seller_id, thread_id, expires_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL %s SECOND), NOW())",
            (token, account_id, seller_id, thread_id, int(hours * 3600)),
        )
        conn.commit()


async def create_session(
    token: str, account_id: int, seller_id: str, thread_id: str, hours: float
) -> None:
    await run_sync(_create_session, token, account_id, seller_id, thread_id, hours)


def _get_session(token: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_VALID_SESSION_SQL, (token,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        # 心跳只在"确实旧了"时才写：每个请求都 UPDATE 会把读路径变成写路径，
        # 而且没有任何人需要秒级的 last_seen。
        cur.execute(
            "UPDATE seller_session SET last_seen_at=NOW() "
            "WHERE token=%s AND (last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL 10 MINUTE)",
            (token,),
        )
        conn.commit()
        return dict(zip(cols, row))


async def get_session(token: str) -> dict[str, Any] | None:
    return await run_sync(_get_session, token)


def _revoke_session(token: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE seller_session SET revoked_at=NOW() WHERE token=%s AND revoked_at IS NULL",
            (token,),
        )
        changed = cur.rowcount > 0
        conn.commit()
        return changed


async def revoke_session(token: str) -> bool:
    return await run_sync(_revoke_session, token)


def _purge_expired(keep_days: int = 7) -> int:
    """删除早已过期的会话（保留 7 天便于排查"我什么时候被踢下线的"）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM seller_session WHERE expires_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (keep_days,),
        )
        removed = cur.rowcount
        conn.commit()
        return int(removed)


async def purge_expired(keep_days: int = 7) -> int:
    return await run_sync(_purge_expired, keep_days)


def _account_exists(username: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM seller_account WHERE username=%s", (username,))
        return cur.fetchone() is not None


async def account_exists(username: str) -> bool:
    return await run_sync(_account_exists, username)


def session_expiry_text(session: dict[str, Any]) -> str:
    """把 `expires_at` 格式化成前端直接可显示的样子（后端统一口径）。"""
    value = session.get("expires_at")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "")
