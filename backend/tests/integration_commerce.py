"""真实 MySQL 烟测：写入专用临时记录、核对归属与消息、最后只清理本次 ID。

默认不执行。运行前确认目标库，再显式传 --run；不调用模型或外部平台。
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import order_repo, service_repo  # noqa: E402
from app.db.connection import connect, parse_mysql_uri  # noqa: E402
from app.orders.service import ImportLine, ImportOrder, import_order  # noqa: E402


def cleanup(order_id: int | None, conversation_id: str | None, reply_id: int | None) -> None:
    """只按本次返回的主键清理；不按模糊前缀删除任何既有数据。"""
    with connect() as conn:
        conn.autocommit(False)
        try:
            with conn.cursor() as cur:
                if conversation_id:
                    cur.execute("SELECT id FROM service_conversation WHERE public_id=%s", (conversation_id,))
                    row = cur.fetchone()
                    if row:
                        cur.execute("DELETE FROM service_message WHERE conversation_id=%s", (row[0],))
                        cur.execute("DELETE FROM service_conversation WHERE id=%s", (row[0],))
                if reply_id:
                    cur.execute("DELETE FROM quick_reply WHERE id=%s", (reply_id,))
                if order_id:
                    cur.execute("DELETE FROM merchant_order_event WHERE order_id=%s", (order_id,))
                    cur.execute("DELETE FROM merchant_order_line WHERE order_id=%s", (order_id,))
                    cur.execute("DELETE FROM merchant_order WHERE id=%s", (order_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


async def smoke() -> None:
    suffix = secrets.token_hex(6)
    order_id = reply_id = None
    conversation_id = None
    try:
        order = ImportOrder(
            external_order_id=f"CODEX-VERIFY-{suffix}", status="paid", paid_cent=1234,
            lines=[ImportLine(line_id="1", title="验证商品", quantity=1,
                              unit_price_cent=1234, paid_cent=1234)],
        )
        imported = await import_order("default", order)
        assert imported["created"] and imported["source"] == "imported"
        order_id = imported["order_id"]
        same = await import_order("default", order)
        assert same["order_id"] == order_id and not same["created"]
        own = await order_repo.get_order("default", order_id)
        other = await order_repo.get_order("CODEX-OTHER-SELLER", order_id)
        assert own and len(own["lines"]) == 1 and other is None

        conversation = await service_repo.create("default", "这是一条自动化验证咨询")
        conversation_id = conversation["conversation_id"]
        buyer_token = conversation["buyer_token"]
        await service_repo.append(conversation_id, "buyer", "请问何时发货？", token=buyer_token)
        reply = await service_repo.create_reply("default", f"验证模板-{suffix}", "这是验证回复")
        reply_id = reply["id"]
        await service_repo.append(conversation_id, "human", "这是验证回复", seller_id="default",
                                  author="codex-verification", reply_id=reply_id)
        own_chat = await service_repo.get(conversation_id, seller_id="default")
        assert len(own_chat["messages"]) == 3
        try:
            await service_repo.get(conversation_id, seller_id="CODEX-OTHER-SELLER")
        except service_repo.ServiceRepoError as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("跨卖家读取未被阻止")
        print("订单幂等、订单隔离、客服消息和客服隔离均通过")
    finally:
        if order_id or conversation_id or reply_id:
            cleanup(order_id, conversation_id, reply_id)
            print("本次测试记录已按主键清理")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="执行真实 MySQL 写入与清理")
    args = parser.parse_args()
    if not settings.mysql_uri:
        print("MYSQL_URI 未配置")
        return 1
    info = parse_mysql_uri(settings.mysql_uri)
    print(f"目标：{info['host']}:{info['port']}/{info['database']}（不显示口令）")
    if not args.run:
        print("仅预览，未连接数据库。传 --run 才写入并清理专用测试记录。")
        return 0
    if settings.resolved_data_source != "local":
        print("非本地数据模式，拒绝写入演示验证数据")
        return 1
    asyncio.run(smoke())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
