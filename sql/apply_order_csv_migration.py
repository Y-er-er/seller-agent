"""中文订单 CSV 所需的显式迁移；默认预览，--apply 才连接并变更。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db.connection import connect, parse_mysql_uri  # noqa: E402

PAYLOAD_DDL = """CREATE TABLE IF NOT EXISTS merchant_order_import_payload (
    order_id BIGINT UNSIGNED NOT NULL,
    source_rows JSON NOT NULL,
    PRIMARY KEY (order_id),
    CONSTRAINT fk_merchant_payload_order FOREIGN KEY (order_id) REFERENCES merchant_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="变更表结构并核对")
    args = parser.parse_args()
    if not settings.mysql_uri:
        print("未配置 MYSQL_URI")
        return 1
    target = parse_mysql_uri(settings.mysql_uri)
    print(f"目标：{target['host']}:{target['port']}/{target['database']}（不显示口令）")
    print("变更：订单金额列（旧数据按 paid_cent 回填）、原始导入行表")
    if not args.apply:
        print("仅预览，未连接数据库")
        return 0

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='merchant_order'",
            (target["database"],),
        )
        if cur.fetchone()[0] != 1:
            print("缺少 merchant_order；请先运行基础订单建表")
            return 1
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=%s "
            "AND table_name='merchant_order' AND column_name='order_amount_cent'",
            (target["database"],),
        )
        if cur.fetchone()[0] == 0:
            cur.execute("ALTER TABLE merchant_order ADD COLUMN order_amount_cent BIGINT UNSIGNED NULL AFTER paid_cent")
        cur.execute("UPDATE merchant_order SET order_amount_cent=paid_cent WHERE order_amount_cent IS NULL")
        conn.commit()
        cur.execute("ALTER TABLE merchant_order MODIFY COLUMN order_amount_cent BIGINT UNSIGNED NOT NULL")
        cur.execute(PAYLOAD_DDL)
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s "
            "AND table_name='merchant_order_import_payload'",
            (target["database"],),
        )
        table_ok = cur.fetchone()[0] == 1
        cur.execute(
            "SELECT COUNT(*) FROM merchant_order WHERE order_amount_cent IS NULL"
        )
        amount_ok = cur.fetchone()[0] == 0
    print(f"订单金额列：{'OK' if amount_ok else 'MISSING'}")
    print(f"原始导入行表：{'OK' if table_ok else 'MISSING'}")
    return 0 if amount_ok and table_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
