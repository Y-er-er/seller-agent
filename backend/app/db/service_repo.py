"""客服咨询持久化。买家凭随机令牌访问自己的会话；卖家按 seller_id 隔离。"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import pymysql

from app.db.connection import connect, run_sync


class ServiceRepoError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _schema_error(exc: pymysql.MySQLError) -> None:
    if exc.args and exc.args[0] == 1146:
        raise ServiceRepoError("schema_missing", "客服表未初始化，请先执行 sql/service_schema.sql", 503) from exc
    raise exc


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create(seller_id: str, text: str) -> dict[str, Any]:
    public_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    try:
        with connect() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM seller_account WHERE seller_id=%s AND status='active' LIMIT 1", (seller_id,))
                    if not cur.fetchone():
                        raise ServiceRepoError("seller_not_found", "商家咨询入口不存在", 404)
                    cur.execute(
                        "INSERT INTO service_conversation (public_id, buyer_token_hash, seller_id, last_seq) "
                        "VALUES (%s,%s,%s,1)", (public_id, _token_hash(token), seller_id),
                    )
                    cid = cur.lastrowid
                    cur.execute(
                        "INSERT INTO service_message (conversation_id, seq, role, content) "
                        "VALUES (%s,1,'buyer',%s)", (cid, text),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"conversation_id": public_id, "buyer_token": token, "status": "open", "assignee": "manual"}
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def create(seller_id: str, text: str) -> dict[str, Any]:
    return await run_sync(_create, seller_id, text)


def _find(cur: Any, public_id: str, *, seller_id: str = "", token: str = "", lock: bool = False) -> dict[str, Any]:
    where = "public_id=%s"
    args: list[Any] = [public_id]
    if seller_id:
        where += " AND seller_id=%s"
        args.append(seller_id)
    else:
        where += " AND buyer_token_hash=%s"
        args.append(_token_hash(token))
    cur.execute(
        "SELECT id, public_id, status, assignee, last_seq, created_at, updated_at "
        f"FROM service_conversation WHERE {where}" + (" FOR UPDATE" if lock else ""), tuple(args),
    )
    row = cur.fetchone()
    if not row:
        raise ServiceRepoError("conversation_not_found", "会话不存在或无权访问", 404)
    return row


def _get(public_id: str, seller_id: str, token: str) -> dict[str, Any]:
    try:
        with connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            row = _find(cur, public_id, seller_id=seller_id, token=token)
            cur.execute(
                "SELECT seq, role, author, content, quick_reply_id, created_at FROM service_message "
                "WHERE conversation_id=%s ORDER BY seq", (row["id"],),
            )
            row["messages"] = cur.fetchall()
            row.pop("id")
            return row
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def get(public_id: str, *, seller_id: str = "", token: str = "") -> dict[str, Any]:
    return await run_sync(_get, public_id, seller_id, token)


def _list(seller_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    where, args = "c.seller_id=%s", [seller_id]
    if status:
        where += " AND c.status=%s"
        args.append(status)
    try:
        with connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT c.public_id, c.status, c.assignee, c.updated_at, "
                "(SELECT m.content FROM service_message m WHERE m.conversation_id=c.id "
                "ORDER BY m.seq DESC LIMIT 1) AS last_message "
                f"FROM service_conversation c WHERE {where} ORDER BY c.updated_at DESC, c.id DESC LIMIT %s",
                tuple(args + [limit]),
            )
            return cur.fetchall()
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def list_conversations(seller_id: str, *, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
    return await run_sync(_list, seller_id, status, limit)


def _append(public_id: str, role: str, text: str, seller_id: str, token: str, author: str, reply_id: int | None) -> dict[str, Any]:
    try:
        with connect() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    row = _find(cur, public_id, seller_id=seller_id, token=token, lock=True)
                    if row["status"] != "open":
                        raise ServiceRepoError("conversation_closed", "会话已结束", 409)
                    if role == "human" and row["assignee"] != "manual":
                        raise ServiceRepoError("not_taken_over", "请先人工接管", 409)
                    if reply_id is not None:
                        cur.execute("SELECT id, content FROM quick_reply WHERE id=%s AND seller_id=%s", (reply_id, seller_id))
                        reply = cur.fetchone()
                        if not reply:
                            raise ServiceRepoError("reply_not_found", "快捷回复不存在", 404)
                        text = reply["content"]
                    seq = row["last_seq"] + 1
                    cur.execute(
                        "INSERT INTO service_message (conversation_id, seq, role, author, content, quick_reply_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (row["id"], seq, role, author, text, reply_id),
                    )
                    cur.execute("UPDATE service_conversation SET last_seq=%s WHERE id=%s", (seq, row["id"]))
                conn.commit()
                return {"seq": seq, "role": role, "content": text, "quick_reply_id": reply_id}
            except Exception:
                conn.rollback()
                raise
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def append(public_id: str, role: str, text: str, *, seller_id: str = "", token: str = "", author: str = "", reply_id: int | None = None) -> dict[str, Any]:
    return await run_sync(_append, public_id, role, text, seller_id, token, author, reply_id)


def _set_state(public_id: str, seller_id: str, *, status: str | None, assignee: str | None) -> dict[str, Any]:
    try:
        with connect() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    row = _find(cur, public_id, seller_id=seller_id, lock=True)
                    if row["status"] == "closed" and status != "closed":
                        raise ServiceRepoError("conversation_closed", "会话已结束", 409)
                    new_status = status or row["status"]
                    new_assignee = assignee or row["assignee"]
                    cur.execute(
                        "UPDATE service_conversation SET status=%s, assignee=%s WHERE id=%s",
                        (new_status, new_assignee, row["id"]),
                    )
                conn.commit()
                return {"conversation_id": public_id, "status": new_status, "assignee": new_assignee}
            except Exception:
                conn.rollback()
                raise
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def set_state(public_id: str, seller_id: str, *, status: str | None = None, assignee: str | None = None) -> dict[str, Any]:
    return await run_sync(_set_state, public_id, seller_id, status=status, assignee=assignee)


def _list_replies(seller_id: str) -> list[dict[str, Any]]:
    try:
        with connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, title, content FROM quick_reply WHERE seller_id=%s ORDER BY id DESC", (seller_id,))
            return cur.fetchall()
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def list_replies(seller_id: str) -> list[dict[str, Any]]:
    return await run_sync(_list_replies, seller_id)


def _create_reply(seller_id: str, title: str, content: str) -> dict[str, Any]:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO quick_reply (seller_id, title, content) VALUES (%s,%s,%s)", (seller_id, title, content))
            return {"id": cur.lastrowid, "title": title, "content": content}
    except pymysql.MySQLError as exc:
        _schema_error(exc)


async def create_reply(seller_id: str, title: str, content: str) -> dict[str, Any]:
    return await run_sync(_create_reply, seller_id, title, content)
