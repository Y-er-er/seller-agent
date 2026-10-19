"""任务中心的数据访问。

一张表 `ops_task`（见 `sql/task_schema.sql`），承载两类东西：

- **长任务**（AI 生图 / 文案 / 定价）：异步执行，进度可见，关掉界面也在跑；
- **批量操作**：即时完成，但把**失败明细**落下来 —— 这类"部分失败"事后必须能查。

与 `goods_repo` 同一套取向：同步 pymysql + `run_sync`；写路径失败上抛。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

DEFAULT_SELLER_ID = "default"

#: 任务状态
TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"
#: 服务重启导致的中断 —— **不假装还在跑**，明确标注
TASK_INTERRUPTED = "interrupted"

#: 未终结的状态（启动时会被标为 interrupted）
_UNFINISHED = (TASK_QUEUED, TASK_RUNNING)


def _require_uri() -> None:
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，任务中心无法访问数据库")


def _loads(raw: Any) -> Any:
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["detail"] = _loads(item.get("detail"))
    for key in ("created_at", "started_at", "finished_at"):
        value = item.get(key)
        if isinstance(value, datetime):
            item[key] = value.isoformat(timespec="seconds")
    return item


# ============================================================
# 写
# ============================================================


def _create_task(
    seller_id: str, kind: str, title: str, target: str,
    action_id: int | None, status: str,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops_task (seller_id, kind, title, target, action_id, status)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (seller_id, kind, title, target or None, action_id, status),
        )
        return int(cur.lastrowid)


async def create_task(
    kind: str, title: str, *, target: str = "", action_id: int | None = None,
    status: str = TASK_QUEUED, seller_id: str = DEFAULT_SELLER_ID,
) -> int:
    """登记一条任务，返回 id。"""
    _require_uri()
    return await run_sync(_create_task, seller_id, kind, title, target, action_id, status)


def _update_task(task_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key}=%s" for key in fields)
    params = list(fields.values()) + [task_id]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE ops_task SET {columns} WHERE id=%s", params)


async def update_task(task_id: int, **fields: Any) -> None:
    """更新任务字段。`detail` 传 dict 会自动序列化；`finished_at` 等传 datetime。"""
    _require_uri()
    payload = dict(fields)
    if "detail" in payload and payload["detail"] is not None:
        payload["detail"] = json.dumps(payload["detail"], ensure_ascii=False, default=str)
    await run_sync(_update_task, task_id, payload)


def _mark_interrupted() -> int:
    marks = ", ".join(["%s"] * len(_UNFINISHED))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE ops_task SET status=%s, error=%s, finished_at=NOW() "
            f"WHERE status IN ({marks})",
            (TASK_INTERRUPTED, "服务重启导致中断（任务未完成，可重新发起）", *_UNFINISHED),
        )
        return cur.rowcount


async def mark_interrupted() -> int:
    """把上次进程遗留的"排队中/执行中"任务标为**中断**。启动时调用。

    为什么必须做：这些任务的后台协程随进程一起没了，若不标注就会永远显示"执行中"。
    诚实地写"被中断"，比让用户盯着一个永远不动的进度条好。
    """
    if not settings.mysql_uri:
        return 0
    try:
        fixed = await run_sync(_mark_interrupted)
    except Exception as exc:  # noqa: BLE001 —— 启动期清理是旁路，失败不阻断启动
        logger.warning("标记中断任务失败（已忽略）：%s: %s", type(exc).__name__, exc)
        return 0
    if fixed:
        logger.warning("已将 %d 个未完成任务标记为「中断」（上次进程退出时未跑完）", fixed)
    return fixed


# ============================================================
# 读
# ============================================================


def _get_task(task_id: int, seller_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ops_task WHERE id=%s AND seller_id=%s", (task_id, seller_id))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
    return _normalize(dict(zip(columns, row)))


async def get_task(task_id: int, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, Any] | None:
    _require_uri()
    return await run_sync(_get_task, task_id, seller_id)


def _list_tasks(seller_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    where = ["seller_id=%s"]
    params: list[Any] = [seller_id]
    if status:
        where.append("status=%s")
        params.append(status)
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ops_task WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT %s", params,
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_tasks(
    seller_id: str = DEFAULT_SELLER_ID, *, status: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """任务列表（按创建时间倒序）。"""
    _require_uri()
    return await run_sync(_list_tasks, seller_id, status, limit)


def _count_unfinished(seller_id: str) -> int:
    marks = ", ".join(["%s"] * len(_UNFINISHED))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM ops_task WHERE seller_id=%s AND status IN ({marks})",
            (seller_id, *_UNFINISHED),
        )
        return int(cur.fetchone()[0])


async def count_unfinished(seller_id: str = DEFAULT_SELLER_ID) -> int:
    """进行中的任务数（左栏角标用）。DB 不可达时返回 0，不阻断渲染。"""
    if not settings.mysql_uri:
        return 0
    try:
        return await run_sync(_count_unfinished, seller_id)
    except Exception as exc:  # noqa: BLE001 —— 角标是装饰，失败就显示 0
        logger.warning("统计进行中任务失败（已降级为 0）：%s: %s", type(exc).__name__, exc)
        return 0
