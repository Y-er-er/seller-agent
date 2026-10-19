"""AI 助手的数据访问（对话原文 + 待确认动作）。

与 `goods_repo` 同一套取向：同步 pymysql + `run_sync` 线程池；写路径失败上抛。

两张表（见 `sql/assistant_schema.sql`）：

- `assistant_message` —— 对话原文。**与售后对话分表**：两条对话的语义与生命周期不同，
  混存会污染彼此的审计链。
- `pending_action` —— 破坏性动作的"待确认"落点。**没有它，AI 就能直接删数据。**
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

DEFAULT_SELLER_ID = "default"

#: 待确认动作的状态
ACTION_PENDING = "pending"
ACTION_CONFIRMED = "confirmed"
ACTION_CANCELLED = "cancelled"


def _require_uri() -> None:
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，AI 助手无法访问数据库")


def _loads(raw: Any) -> Any:
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


# ============================================================
# 对话消息
# ============================================================


def _next_seq(thread_id: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM assistant_message WHERE thread_id=%s",
            (thread_id,),
        )
        return int(cur.fetchone()[0])


def _append_message(
    seller_id: str, thread_id: str, seq: int, role: str, content: str,
    tool_name: str | None, tool_args: dict[str, Any] | None,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO assistant_message (seller_id, thread_id, seq, role, content, tool_name, tool_args) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (seller_id, thread_id, seq, role, content, tool_name,
             json.dumps(tool_args, ensure_ascii=False) if tool_args is not None else None),
        )
        return int(cur.lastrowid)


async def append_message(
    thread_id: str,
    role: str,
    content: str = "",
    *,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    seller_id: str = DEFAULT_SELLER_ID,
) -> dict[str, Any]:
    """追加一条对话消息（自动分配 seq）。返回写入的完整行。"""
    _require_uri()
    seq = await run_sync(_next_seq, thread_id)
    mid = await run_sync(_append_message, seller_id, thread_id, seq, role, content, tool_name, tool_args)
    return {
        "id": mid, "thread_id": thread_id, "seq": seq, "role": role,
        "content": content, "tool_name": tool_name, "tool_args": tool_args,
    }


def _list_messages(thread_id: str, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, thread_id, seq, role, content, tool_name, tool_args, created_at "
            "FROM assistant_message WHERE thread_id=%s ORDER BY seq ASC LIMIT %s",
            (thread_id, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(columns, row))
        item["tool_args"] = _loads(item.get("tool_args"))
        out.append(item)
    return out


async def list_messages(thread_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """按 seq 顺序返回对话历史。"""
    _require_uri()
    return await run_sync(_list_messages, thread_id, limit)


def _reset_thread(thread_id: str, seller_id: str) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM assistant_message WHERE thread_id=%s AND seller_id=%s",
                    (thread_id, seller_id))
        msgs = cur.rowcount
        # 未决的待确认动作一并作废（否则"新对话"后还会冒出一个悬空的确认卡）
        cur.execute(
            "UPDATE pending_action SET status=%s, decided_at=NOW() "
            "WHERE thread_id=%s AND seller_id=%s AND status=%s",
            (ACTION_CANCELLED, thread_id, seller_id, ACTION_PENDING),
        )
        actions = cur.rowcount
    return {"messages": msgs, "actions_cancelled": actions}


async def reset_thread(thread_id: str, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, int]:
    """清空该线程（新对话）：删消息 + 作废未决动作。"""
    _require_uri()
    return await run_sync(_reset_thread, thread_id, seller_id)


# ============================================================
# 待确认动作
# ============================================================


def _create_action(
    seller_id: str, thread_id: str, tool_name: str,
    arguments: dict[str, Any], summary: str, detail: str,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_action (seller_id, thread_id, tool_name, arguments, summary, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (seller_id, thread_id, tool_name, json.dumps(arguments, ensure_ascii=False), summary, detail),
        )
        return int(cur.lastrowid)


async def create_action(
    thread_id: str, tool_name: str, arguments: dict[str, Any], summary: str, detail: str = "",
    seller_id: str = DEFAULT_SELLER_ID,
) -> int:
    """登记一个待确认动作，返回 id。"""
    _require_uri()
    return await run_sync(_create_action, seller_id, thread_id, tool_name, arguments, summary, detail)


def _get_action(action_id: int, seller_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pending_action WHERE id=%s AND seller_id=%s", (action_id, seller_id))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        item = dict(zip(columns, row))
    item["arguments"] = _loads(item.get("arguments")) or {}
    return item


async def get_action(action_id: int, seller_id: str) -> dict[str, Any] | None:
    """读取单个待确认动作。

    ⚠️ `seller_id` **刻意不给默认值**：这两个动作（读取 / 决定）直接决定"能不能执行破坏性操作"，
    漏传会静默按其它卖家处理 —— 让它在调用处就报错，比事后排查便宜得多（实测踩到过）。
    """
    _require_uri()
    return await run_sync(_get_action, action_id, seller_id)


def _list_actions(thread_id: str, seller_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    where = ["thread_id=%s", "seller_id=%s"]
    params: list[Any] = [thread_id, seller_id]
    if status:
        where.append("status=%s")
        params.append(status)
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_action WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT %s", params,
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(columns, row))
        item["arguments"] = _loads(item.get("arguments")) or {}
        out.append(item)
    return out


async def list_actions(
    thread_id: str, seller_id: str = DEFAULT_SELLER_ID, *, status: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """列出待确认动作（`status='pending'` 即当前待处理）。"""
    _require_uri()
    return await run_sync(_list_actions, thread_id, seller_id, status, limit)


def _decide_action(action_id: int, seller_id: str, status: str, result: str) -> bool:
    """标记动作已决。**只有 pending 能被决定** —— 重复确认会被这里挡住。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, result=%s, decided_at=NOW() "
            "WHERE id=%s AND seller_id=%s AND status=%s",
            (status, result, action_id, seller_id, ACTION_PENDING),
        )
        return cur.rowcount > 0


async def decide_action(
    action_id: int, status: str, result: str, seller_id: str
) -> bool:
    """把动作标记为 confirmed / cancelled。返回是否生效（已被决定过则 False）。

    ⚠️ `seller_id` **刻意不给默认值**，理由同 `get_action`：漏传会让"决定"落到别的卖家名下。
    """
    _require_uri()
    return await run_sync(_decide_action, action_id, seller_id, status, result)
