"""商品域数据访问（MySQL）。

设计取向与 `chat_repo` 一致：

- **所有 SQL 只在本模块**，service / api 不直接写 SQL；
- 同步 pymysql 通过 `run_sync()` 丢线程池，不阻塞事件循环；
- **写路径失败上抛**（商品是不可丢的业务数据，不像审计那样可降级）；
- 读路径失败同样上抛，由 API 层转成明确错误（不静默返回空）。

表结构见 `sql/goods_schema.sql`。四个关键约定：

1. `seller_id` 目前固定 `'default'`（预留多卖家，见 `app/seller/deps.py`）；
2. 「陈列」与「管理」共用 `goods` 一张表，`scope` 只影响**查询条件**；
3. 删除商品时由 **service 层**级联清理图片与对象存储文件，repo 只管行；
4. `operation_log` 的 before/after 存 JSON，撤销据此回滚（仅可逆动作）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

DEFAULT_SELLER_ID = "default"

#: 商品状态（与 DDL 注释、service 状态机一致）
STATUS_DRAFT = "draft"
STATUS_ON_SHELF = "on_shelf"
STATUS_OFF_SHELF = "off_shelf"

#: 操作日志的来源
ACTOR_HUMAN = "human"
ACTOR_AI = "ai"
ACTOR_SYSTEM = "system"


def _require_uri() -> None:
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，商品域无法访问数据库")


# ============================================================
# 商品：写
# ============================================================


def _create_goods(
    seller_id: str,
    goods_no: str,
    title: str,
    description: str,
    price: float,
    cost_price: float,
    stock: int,
    stock_alert: int,
    category: str,
    status: str,
    sort_no: int,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO goods (seller_id, goods_no, title, description, price, cost_price, "
            "stock, stock_alert, category, status, sort_no) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (seller_id, goods_no, title, description, price, cost_price,
             stock, stock_alert, category, status, sort_no),
        )
        return int(cur.lastrowid)


async def create_goods(**fields: Any) -> int:
    """新建商品，返回自增 id。编号冲突会抛 `pymysql.err.IntegrityError`（由 service 层转业务错误）。"""
    _require_uri()
    return await run_sync(_create_goods, **fields)


def _update_goods(goods_id: int, seller_id: str, fields: dict[str, Any]) -> bool:
    if not fields:
        return False
    allowed = {
        "title", "description", "price", "cost_price", "stock", "stock_alert",
        "category", "status", "cover_url", "sort_no", "pinned",
    }
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"不允许更新的字段：{key}")
        sets.append(f"{key}=%s")
        params.append(value)
    params.extend([goods_id, seller_id])
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE goods SET {', '.join(sets)} WHERE id=%s AND seller_id=%s", params
        )
        return cur.rowcount > 0


async def update_goods(goods_id: int, seller_id: str, **fields: Any) -> bool:
    """按白名单更新商品字段；返回是否命中行。"""
    _require_uri()
    return await run_sync(_update_goods, goods_id, seller_id, fields)


def _delete_goods(goods_id: int, seller_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM goods WHERE id=%s AND seller_id=%s", (goods_id, seller_id))
        return cur.rowcount > 0


async def delete_goods(goods_id: int, seller_id: str) -> bool:
    """删除商品行。**图片行与对象存储文件由 service 层负责清理**。"""
    _require_uri()
    return await run_sync(_delete_goods, goods_id, seller_id)


def _next_goods_no(seller_id: str) -> str:
    """生成下一个商品编号（G-0001 递增）。在同卖家内取当前最大序号 +1。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT goods_no FROM goods WHERE seller_id=%s AND goods_no LIKE 'G-%%' "
            "ORDER BY goods_no DESC LIMIT 1",
            (seller_id,),
        )
        row = cur.fetchone()
    last = int(row[0].split("-")[1]) if row and row[0] else 0
    return f"G-{last + 1:04d}"


async def next_goods_no(seller_id: str) -> str:
    """下一个可用编号。⚠️ 仅用于展示与创建；并发下仍可能撞号，靠唯一键兜底。"""
    _require_uri()
    return await run_sync(_next_goods_no, seller_id)


def _max_sort_no(seller_id: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_no), 0) FROM goods WHERE seller_id=%s", (seller_id,))
        return int(cur.fetchone()[0] or 0)


async def max_sort_no(seller_id: str) -> int:
    """当前最大排序值（新建商品时追加到末尾）。"""
    _require_uri()
    return await run_sync(_max_sort_no, seller_id)


def _reorder(seller_id: str, ordered_ids: list[int]) -> int:
    """按给定顺序重写 sort_no（0,1,2…）。整体一次事务，避免中间态。"""
    if not ordered_ids:
        return 0
    with connect() as conn, conn.cursor() as cur:
        for index, goods_id in enumerate(ordered_ids):
            cur.execute(
                "UPDATE goods SET sort_no=%s WHERE id=%s AND seller_id=%s",
                (index, goods_id, seller_id),
            )
        return len(ordered_ids)


async def reorder_goods(seller_id: str, ordered_ids: list[int]) -> int:
    """陈列编排：按传入的 id 顺序落 `sort_no`。返回更新条数。"""
    _require_uri()
    return await run_sync(_reorder, seller_id, ordered_ids)


# ============================================================
# 商品：读
# ============================================================


def _list_goods(
    seller_id: str, scope: str, q: str, status: str, low_stock_only: bool, limit: int
) -> list[dict[str, Any]]:
    where = ["seller_id=%s"]
    params: list[Any] = [seller_id]
    if scope == "shelf":
        where.append("status=%s")
        params.append(STATUS_ON_SHELF)
    if status:
        where.append("status=%s")
        params.append(status)
    if q:
        where.append("(title LIKE %s OR goods_no LIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if low_stock_only:
        where.append("stock_alert > 0 AND stock <= stock_alert")
    params.append(limit)

    # 陈列顺序：置顶优先 → 排序值 → 越新越前
    sql = (
        "SELECT * FROM goods WHERE " + " AND ".join(where) +
        " ORDER BY pinned DESC, sort_no ASC, id DESC LIMIT %s"
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_goods(
    seller_id: str = DEFAULT_SELLER_ID,
    *,
    scope: str = "all",
    q: str = "",
    status: str = "",
    low_stock_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """商品列表。`scope='shelf'` 只看已上架（陈列视图），`'all'` 全量（管理视图）。"""
    _require_uri()
    return await run_sync(_list_goods, seller_id, scope, q, status, low_stock_only, limit)


def _get_goods(goods_id: int, seller_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM goods WHERE id=%s AND seller_id=%s", (goods_id, seller_id))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        goods = _normalize(dict(zip(columns, row)))
        cur.execute(
            "SELECT id, url, object_key, kind, sort_no FROM goods_media "
            "WHERE goods_id=%s ORDER BY sort_no ASC, id ASC",
            (goods_id,),
        )
        mcols = [d[0] for d in cur.description]
        goods["media"] = [dict(zip(mcols, r)) for r in cur.fetchall()]
    return goods


async def get_goods(goods_id: int, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, Any] | None:
    """商品详情（含图片列表）；不存在返回 `None`。"""
    _require_uri()
    return await run_sync(_get_goods, goods_id, seller_id)


def _count_by_status(seller_id: str) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM goods WHERE seller_id=%s GROUP BY status", (seller_id,)
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}


async def count_by_status(seller_id: str = DEFAULT_SELLER_ID) -> dict[str, int]:
    """按状态统计（概览看板用）。"""
    _require_uri()
    return await run_sync(_count_by_status, seller_id)


def _list_low_stock(seller_id: str, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, goods_no, title, stock, stock_alert FROM goods "
            "WHERE seller_id=%s AND stock_alert > 0 AND stock <= stock_alert "
            "ORDER BY (stock_alert - stock) DESC LIMIT %s",
            (seller_id, limit),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


async def list_low_stock(seller_id: str = DEFAULT_SELLER_ID, limit: int = 20) -> list[dict[str, Any]]:
    """低库存商品（概览预警用）。"""
    _require_uri()
    return await run_sync(_list_low_stock, seller_id, limit)


# ============================================================
# 图片
# ============================================================


def _add_media(seller_id: str, goods_id: int, url: str, object_key: str,
               kind: str, sort_no: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO goods_media (seller_id, goods_id, url, object_key, kind, sort_no) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (seller_id, goods_id, url, object_key, kind, sort_no),
        )
        return int(cur.lastrowid)


async def add_media(goods_id: int, url: str, object_key: str = "",
                    kind: str = "cover", sort_no: int = 0,
                    seller_id: str = DEFAULT_SELLER_ID) -> int:
    """登记一张商品图片，返回自增 id。"""
    _require_uri()
    return await run_sync(_add_media, seller_id, goods_id, url, object_key, kind, sort_no)


def _delete_media(media_id: int, seller_id: str) -> dict[str, Any] | None:
    """删除图片行，返回被删行（调用方据此清理对象存储文件）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, goods_id, object_key FROM goods_media WHERE id=%s AND seller_id=%s",
            (media_id, seller_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM goods_media WHERE id=%s", (media_id,))
        return {"id": row[0], "goods_id": row[1], "object_key": row[2]}


async def delete_media(media_id: int, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, Any] | None:
    """删除图片行；返回被删行信息（含 object_key），供调用方清理文件。"""
    _require_uri()
    return await run_sync(_delete_media, media_id, seller_id)


def _list_media_keys(goods_id: int) -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT object_key FROM goods_media WHERE goods_id=%s AND object_key IS NOT NULL",
            (goods_id,),
        )
        return [str(r[0]) for r in cur.fetchall() if r[0]]


async def list_media_keys(goods_id: int) -> list[str]:
    """该商品全部图片的对象存储 key（删除商品时用于清理文件）。"""
    _require_uri()
    return await run_sync(_list_media_keys, goods_id)


def _delete_media_by_goods(goods_id: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM goods_media WHERE goods_id=%s", (goods_id,))
        return cur.rowcount


async def delete_media_by_goods(goods_id: int) -> int:
    """删除该商品的全部图片行。"""
    _require_uri()
    return await run_sync(_delete_media_by_goods, goods_id)


# ============================================================
# 操作日志
# ============================================================


def _insert_operation(
    seller_id: str, actor: str, action: str, target_type: str, target_id: str,
    summary: str, before: dict[str, Any] | None, after: dict[str, Any] | None, undoable: bool,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO operation_log (seller_id, actor, action, target_type, target_id, "
            "summary, before_json, after_json, undoable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                seller_id, actor, action, target_type, target_id, summary,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                1 if undoable else 0,
            ),
        )
        return int(cur.lastrowid)


async def insert_operation(
    *,
    actor: str,
    action: str,
    target_id: str,
    summary: str,
    target_type: str = "goods",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    undoable: bool = False,
    seller_id: str = DEFAULT_SELLER_ID,
) -> int:
    """写一条操作日志，返回自增 id。"""
    _require_uri()
    return await run_sync(
        _insert_operation, seller_id, actor, action, target_type, target_id,
        summary, before, after, undoable,
    )


def _list_operations(seller_id: str, actor: str, target_id: str, limit: int) -> list[dict[str, Any]]:
    where = ["seller_id=%s"]
    params: list[Any] = [seller_id]
    if actor:
        where.append("actor=%s")
        params.append(actor)
    if target_id:
        where.append("target_id=%s")
        params.append(target_id)
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM operation_log WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT %s",
            params,
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(zip(columns, row))
        # JSON 列在 pymysql 下可能是字符串，统一解析为对象，供界面直接渲染
        for key in ("before_json", "after_json"):
            raw = item.get(key)
            if isinstance(raw, str) and raw:
                try:
                    item[key] = json.loads(raw)
                except json.JSONDecodeError:
                    item[key] = None
        out.append(item)
    return out


async def list_operations(
    seller_id: str = DEFAULT_SELLER_ID, *, actor: str = "", target_id: str = "", limit: int = 100
) -> list[dict[str, Any]]:
    """操作日志列表（按来源 / 对象筛选）。"""
    _require_uri()
    return await run_sync(_list_operations, seller_id, actor, target_id, limit)


def _get_operation(log_id: int, seller_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM operation_log WHERE id=%s AND seller_id=%s", (log_id, seller_id))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        item = dict(zip(columns, row))
    for key in ("before_json", "after_json"):
        raw = item.get(key)
        if isinstance(raw, str) and raw:
            try:
                item[key] = json.loads(raw)
            except json.JSONDecodeError:
                item[key] = None
    return item


async def get_operation(log_id: int, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, Any] | None:
    """单条操作日志（撤销前读取 before 快照）。"""
    _require_uri()
    return await run_sync(_get_operation, log_id, seller_id)


def _mark_undone(log_id: int, seller_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE operation_log SET undone_at=NOW() WHERE id=%s AND seller_id=%s "
            "AND undone_at IS NULL AND undoable=1",
            (log_id, seller_id),
        )
        return cur.rowcount > 0


async def mark_operation_undone(log_id: int, seller_id: str = DEFAULT_SELLER_ID) -> bool:
    """把日志标记为已撤销；返回是否成功（已撤销过 / 不可撤销 → False）。"""
    _require_uri()
    return await run_sync(_mark_undone, log_id, seller_id)


# ============================================================
# 工具
# ============================================================


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """DECIMAL → float，便于 JSON 序列化（pymysql 返回 Decimal）。"""
    for key in ("price", "cost_price"):
        value = row.get(key)
        if value is not None and not isinstance(value, (int, float)):
            row[key] = float(value)
    for key in ("pinned",):
        if key in row:
            row[key] = bool(row[key])
    return row
