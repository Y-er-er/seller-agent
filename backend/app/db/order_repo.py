"""本地订单快照的 MySQL 仓储。所有查询以 seller_id 限定归属。"""

from __future__ import annotations

import json
from typing import Any

import pymysql

from app.db.connection import connect, run_sync


class OrderRepoError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _missing_table(exc: pymysql.MySQLError) -> None:
    if getattr(exc, "args", ()) and exc.args[0] == 1146:
        raise OrderRepoError("schema_missing", "订单表未初始化，请先执行 sql/order_schema.sql", 503) from exc
    raise exc


def _save(seller_id: str, source: str, order: dict[str, Any], digest: str) -> dict[str, Any]:
    try:
        with connect() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(
                        "SELECT id, content_hash FROM merchant_order WHERE seller_id=%s AND source=%s "
                        "AND external_order_id=%s FOR UPDATE",
                        (seller_id, source, order["external_order_id"]),
                    )
                    existing = cur.fetchone()
                    if existing:
                        if existing["content_hash"] != digest:
                            raise OrderRepoError("order_conflict", "同一订单号已有不同内容，请核对来源数据", 409)
                        conn.commit()
                        return {"order_id": existing["id"], "created": False, "source": source}
                    cur.execute(
                        "INSERT INTO merchant_order "
                        "(seller_id, source, external_order_id, status, paid_cent, order_amount_cent, placed_at, content_hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (seller_id, source, order["external_order_id"], order["status"], order["paid_cent"],
                         order["order_amount_cent"],
                         (order["placed_at"].replace("T", " ") if order["placed_at"] else None), digest),
                    )
                    order_id = cur.lastrowid
                    cur.executemany(
                        "INSERT INTO merchant_order_line "
                        "(order_id, line_id, goods_no, sku_id, title, quantity, unit_price_cent, paid_cent) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(order_id, line["line_id"], line["goods_no"], line["sku_id"], line["title"],
                          line["quantity"], line["unit_price_cent"], line["paid_cent"])
                         for line in order["lines"]],
                    )
                    if order.get("import_payload") is not None:
                        # 原始字段与订单在同一事务保存，避免详情缺失或部分导入。
                        cur.execute(
                            "INSERT INTO merchant_order_import_payload (order_id, source_rows) VALUES (%s, %s)",
                            (order_id, json.dumps(order["import_payload"], ensure_ascii=False)),
                        )
                    cur.execute(
                        "INSERT INTO merchant_order_event (order_id, event_type, detail, simulated) "
                        "VALUES (%s, %s, %s, %s)",
                        (order_id, "demo_created" if source == "demo" else "imported",
                         "生成本地演示订单" if source == "demo" else "商家导入订单快照", source == "demo"),
                    )
                conn.commit()
                return {"order_id": order_id, "created": True, "source": source}
            except pymysql.IntegrityError as exc:
                conn.rollback()
                # 两个导入请求并发时唯一键会挡住重复写；让调用方重试读取同一订单。
                if exc.args and exc.args[0] == 1062:
                    raise OrderRepoError("concurrent_import", "订单正在导入，请稍后重试", 409) from exc
                raise
            except Exception:
                conn.rollback()
                raise
    except pymysql.MySQLError as exc:
        _missing_table(exc)


async def import_order(seller_id: str, order: dict[str, Any], digest: str) -> dict[str, Any]:
    return await run_sync(_save, seller_id, "imported", order, digest)


async def create_demo(seller_id: str, order: dict[str, Any], digest: str) -> dict[str, Any]:
    return await run_sync(_save, seller_id, "demo", order, digest)


def _list(seller_id: str, query: str, limit: int, offset: int) -> dict[str, Any]:
    where = "seller_id=%s"
    args: list[Any] = [seller_id]
    if query:
        where += " AND external_order_id LIKE %s"
        args.append(f"%{query}%")
    try:
        with connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM merchant_order WHERE {where}", tuple(args))
            total = cur.fetchone()["n"]
            cur.execute(
                f"SELECT id, source, external_order_id, status, paid_cent, order_amount_cent, placed_at, imported_at, "
                f"EXISTS (SELECT 1 FROM merchant_order_import_payload p WHERE p.order_id=merchant_order.id "
                f"AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(p.source_rows, '$[0].\"物流公司\"')), '') <> '' "
                f"AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(p.source_rows, '$[0].\"物流单号\"')), '') <> '') "
                f"AS has_tracking "
                f"FROM merchant_order WHERE {where} ORDER BY imported_at DESC, id DESC LIMIT %s OFFSET %s",
                tuple(args + [limit, offset]),
            )
            return {"items": cur.fetchall(), "total": total, "limit": limit, "offset": offset}
    except pymysql.MySQLError as exc:
        _missing_table(exc)


async def list_orders(seller_id: str, *, query: str, limit: int, offset: int) -> dict[str, Any]:
    return await run_sync(_list, seller_id, query, limit, offset)


def _get(seller_id: str, order_id: int) -> dict[str, Any] | None:
    try:
        with connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, source, external_order_id, status, paid_cent, order_amount_cent, placed_at, imported_at "
                "FROM merchant_order WHERE seller_id=%s AND id=%s", (seller_id, order_id),
            )
            order = cur.fetchone()
            if not order:
                return None
            cur.execute(
                "SELECT line_id, goods_no, sku_id, title, quantity, unit_price_cent, paid_cent "
                "FROM merchant_order_line WHERE order_id=%s ORDER BY id", (order_id,),
            )
            order["lines"] = cur.fetchall()
            cur.execute(
                "SELECT source_rows FROM merchant_order_import_payload WHERE order_id=%s", (order_id,),
            )
            payload = cur.fetchone()
            order["import_payload"] = json.loads(payload["source_rows"]) if payload else None
            cur.execute(
                "SELECT event_type, detail, simulated, created_at FROM merchant_order_event "
                "WHERE order_id=%s ORDER BY id", (order_id,),
            )
            order["events"] = cur.fetchall()
            return order
    except pymysql.MySQLError as exc:
        _missing_table(exc)


async def get_order(seller_id: str, order_id: int) -> dict[str, Any] | None:
    return await run_sync(_get, seller_id, order_id)
