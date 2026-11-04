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
import re
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


# ⚠️ 原有的「物理删除商品行」已移除：删除语义现在是**移入回收站**（`soft_delete`），
#    真正物理删除只允许走 `purge_hard`（且仅限回收站内的商品）。
#    留一个随手可用的物理删除入口，早晚会被误用。


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


#: 允许排序的列（**白名单**：排序字段来自外部输入，绝不能直接拼进 SQL）
_SORTABLE = {"price": "price", "stock": "stock", "updated_at": "updated_at", "created_at": "created_at"}


def _day_start(value: str) -> str:
    """把日期补成当天的 00:00:00；已是完整时间则原样返回。"""
    text = str(value or "").strip()
    return f"{text} 00:00:00" if len(text) == 10 else text


def _day_end(value: str) -> str:
    """把日期补成当天的 23:59:59 —— 否则 `<= 2026-10-19` 会把当天创建的全部漏掉。"""
    text = str(value or "").strip()
    return f"{text} 23:59:59" if len(text) == 10 else text


#: 关键词切段：连续中文为一个词，连续字母数字为一个词。中文标点/空格自动丢弃。
#: 为什么不用第三方分词：这里只需要"把中英混排的短语切成能各自命中的片段"，
#: 一个正则就够；引入分词库会为一个 LIKE 条件增加一处依赖（需审批）。
_KEYWORD_SPLIT = re.compile(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+")


def keyword_terms(q: str) -> list[str]:
    """把搜索词切成若干片段（去重保序）。例：「英伟达5090显卡」→ 英伟达 / 5090 / 显卡。"""
    terms: list[str] = []
    for raw in _KEYWORD_SPLIT.findall(str(q or "")):
        if raw and raw not in terms:
            terms.append(raw)
    return terms


def _like(term: str) -> str:
    """构造 LIKE 参数：转义 `%` / `_` / `\\`，否则卖家输个 `%` 就等于全表匹配。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def keyword_where(q: str) -> tuple[str, list[Any]]:
    """关键词的 WHERE 片段与参数 —— **逐片段 AND**，每片段命中标题或编号即可。

    ⚠️ 这里曾经是整串 `title LIKE '%整串%'`，2026-10-21 实测翻车：
    卖家问「联想笔记本电脑与英伟达5090显卡差多少钱」，库里那件叫
    **「英伟达RTX5090显卡」**（编号 G-0012，就排在列表第一行）。整串 LIKE
    一条都匹配不上 → 工具返回「没有符合条件的商品」→ 助手据此断言
    「咱们店里目前并没有售卖这款商品」。**数据没错，是检索把"没匹配上"说成了"没有"。**

    切成片段后：「英伟达」+「5090」+「显卡」三段都能在 `英伟达RTX5090显卡` 里命中。
    片段之间用 AND（不是 OR）—— 否则「联想 显卡」会把半店的商品都捞出来。
    """
    clauses: list[str] = []
    params: list[Any] = []
    for term in keyword_terms(q):
        clauses.append("(title LIKE %s OR goods_no LIKE %s)")
        pattern = _like(term)
        params.extend([pattern, pattern])
    return " AND ".join(clauses), params


def _list_goods(
    seller_id: str, scope: str, q: str, status: str, low_stock_only: bool, limit: int,
    category: str = "",
    price_min: float | None = None, price_max: float | None = None,
    stock_min: int | None = None, stock_max: int | None = None,
    sort: str = "", order: str = "asc",
    created_after: str = "", created_before: str = "",
) -> list[dict[str, Any]]:
    where = ["seller_id=%s", "deleted_at IS NULL"]
    params: list[Any] = [seller_id]
    if scope == "shelf":
        where.append("status=%s")
        params.append(STATUS_ON_SHELF)
    if status:
        where.append("status=%s")
        params.append(status)
    if q:
        # 逐片段 AND（见 `keyword_where` 的注释：整串匹配会把"没匹配上"变成"没有"）
        kw_clause, kw_params = keyword_where(q)
        if kw_clause:
            where.append(kw_clause)
            params.extend(kw_params)
    if low_stock_only:
        # 与 `_list_low_stock` 同口径：断货（stock<=0）不看阈值，一律算低库存
        where.append("(stock <= 0 OR (stock_alert > 0 AND stock <= stock_alert))")
    # ---- 与界面筛选**同口径**的区间条件（闭区间）----
    if category:
        where.append("category=%s")
        params.append(category)
    if price_min is not None:
        where.append("price >= %s")
        params.append(price_min)
    if price_max is not None:
        where.append("price <= %s")
        params.append(price_max)
    if stock_min is not None:
        where.append("stock >= %s")
        params.append(stock_min)
    if stock_max is not None:
        where.append("stock <= %s")
        params.append(stock_max)
    # 创建时间：接受 `YYYY-MM-DD`；`before` 补到当天 23:59:59（否则会漏掉当天的商品）
    if created_after:
        where.append("created_at >= %s")
        params.append(_day_start(created_after))
    if created_before:
        where.append("created_at <= %s")
        params.append(_day_end(created_before))
    params.append(limit)

    col = _SORTABLE.get(str(sort or "").strip())
    if col:
        direction = "DESC" if str(order or "").strip().lower() == "desc" else "ASC"
        order_sql = f" ORDER BY {col} {direction}, id DESC"
    else:
        # 默认：陈列顺序 —— 置顶优先 → 排序值 → 越新越前
        order_sql = " ORDER BY pinned DESC, sort_no ASC, id DESC"

    sql = "SELECT * FROM goods WHERE " + " AND ".join(where) + order_sql + " LIMIT %s"
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
    category: str = "",
    price_min: float | None = None,
    price_max: float | None = None,
    stock_min: int | None = None,
    stock_max: int | None = None,
    sort: str = "",
    order: str = "asc",
    created_after: str = "",
    created_before: str = "",
) -> list[dict[str, Any]]:
    """商品列表。`scope='shelf'` 只看已上架（陈列视图），`'all'` 全量（管理视图）。

    筛选参数与**界面筛选一一对应**（AI 要能做人能做的事，口径必须一致）。
    `sort` 支持 price / stock / updated_at / created_at；`order` 为 asc / desc。
    创建时间接受 `YYYY-MM-DD`（`before` 会自动补到当天 23:59:59）。
    """
    _require_uri()
    return await run_sync(
        _list_goods, seller_id, scope, q, status, low_stock_only, limit,
        category, price_min, price_max, stock_min, stock_max, sort, order,
        created_after, created_before,
    )


def _get_goods(goods_id: int, seller_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    """读商品。默认**排除回收站里的**；`include_deleted=True` 供恢复 / 彻底删除使用。"""
    sql = "SELECT * FROM goods WHERE id=%s AND seller_id=%s"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (goods_id, seller_id))
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


async def get_goods(
    goods_id: int, seller_id: str = DEFAULT_SELLER_ID, *, include_deleted: bool = False
) -> dict[str, Any] | None:
    """商品详情（含图片列表）；默认**排除回收站里的**。"""
    _require_uri()
    return await run_sync(_get_goods, goods_id, seller_id, include_deleted)


# ============================================================
# 回收站（软删除）—— 删除只在 deleted_at 上打标记，**不动 status**，
# 所以恢复时原状态（已上架/草稿）会自动回来，不需要另存一份。
# ============================================================


def _list_categories(seller_id: str) -> list[dict[str, Any]]:
    """现有类目清单（按商品数降序）。

    类目不是独立表，而是从商品上聚合出来的 —— 这样"新建类目"就是给商品填一个新名字，
    不会出现"类目表里有、却没有商品"的空壳类目。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT category, COUNT(*) AS cnt FROM goods "
            "WHERE seller_id=%s AND deleted_at IS NULL AND COALESCE(category,'') <> '' "
            "GROUP BY category ORDER BY cnt DESC, category ASC",
            (seller_id,),
        )
        return [{"category": str(r[0]), "count": int(r[1])} for r in cur.fetchall()]


async def list_categories(seller_id: str = DEFAULT_SELLER_ID) -> list[dict[str, Any]]:
    """现有类目清单（含每个类目下的商品数）。"""
    _require_uri()
    return await run_sync(_list_categories, seller_id)


def _list_trash(seller_id: str, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT *, DATEDIFF(NOW(), deleted_at) AS days_since "
            "FROM goods WHERE seller_id=%s AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT %s",
            (seller_id, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_trash(seller_id: str = DEFAULT_SELLER_ID, limit: int = 100) -> list[dict[str, Any]]:
    """回收站列表（按删除时间倒序）。"""
    _require_uri()
    return await run_sync(_list_trash, seller_id, limit)


def _soft_delete(goods_id: int, seller_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE goods SET deleted_at=NOW() "
            "WHERE id=%s AND seller_id=%s AND deleted_at IS NULL",
            (goods_id, seller_id),
        )
        return cur.rowcount > 0


async def soft_delete(goods_id: int, seller_id: str = DEFAULT_SELLER_ID) -> bool:
    _require_uri()
    return await run_sync(_soft_delete, goods_id, seller_id)


def _restore(goods_id: int, seller_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE goods SET deleted_at=NULL "
            "WHERE id=%s AND seller_id=%s AND deleted_at IS NOT NULL",
            (goods_id, seller_id),
        )
        return cur.rowcount > 0


async def restore(goods_id: int, seller_id: str = DEFAULT_SELLER_ID) -> bool:
    """从回收站恢复（原状态一并回来）。"""
    _require_uri()
    return await run_sync(_restore, goods_id, seller_id)


def _purge_hard(goods_id: int, seller_id: str) -> bool:
    """彻底删除。**仅限回收站里的** —— 防止误删正常商品。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM goods WHERE id=%s AND seller_id=%s AND deleted_at IS NOT NULL",
            (goods_id, seller_id),
        )
        return cur.rowcount > 0


async def purge_hard(goods_id: int, seller_id: str = DEFAULT_SELLER_ID) -> bool:
    _require_uri()
    return await run_sync(_purge_hard, goods_id, seller_id)


def _list_expired(days: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, seller_id, goods_no, title, deleted_at FROM goods "
            "WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL %s DAY)",
            (days,),
        )
        columns = [d[0] for d in cur.description]
        return [_normalize(dict(zip(columns, row))) for row in cur.fetchall()]


async def list_expired(days: int) -> list[dict[str, Any]]:
    """回收站里超过保留期的商品（供清理任务使用）。"""
    _require_uri()
    return await run_sync(_list_expired, days)


def _count_by_status(seller_id: str) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM goods WHERE seller_id=%s AND deleted_at IS NULL "
            "GROUP BY status", (seller_id,)
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}


async def count_by_status(seller_id: str = DEFAULT_SELLER_ID) -> dict[str, int]:
    """按状态统计（概览看板用）。"""
    _require_uri()
    return await run_sync(_count_by_status, seller_id)


def _list_low_stock(seller_id: str, limit: int) -> list[dict[str, Any]]:
    """低库存 / 断货商品。

    ⚠️ **断货（`stock <= 0`）不论有没有设预警阈值都要报** ——
    之前写成 `stock_alert > 0 AND stock <= stock_alert`，导致
    "没设阈值 + 库存 0" 的商品被静默放过（实测：库存 0 的风扇不在巡检里）。
    断货是绝对异常，比"低于自定义线"更严重。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, goods_no, title, stock, stock_alert FROM goods "
            "WHERE seller_id=%s AND deleted_at IS NULL "
            "AND (stock <= 0 OR (stock_alert > 0 AND stock <= stock_alert)) "
            "ORDER BY (stock <= 0) DESC, (stock_alert - stock) DESC LIMIT %s",
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
