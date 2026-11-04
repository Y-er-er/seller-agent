"""订单与客服表的显式迁移器。默认仅预览；传 --apply 才连接 MySQL。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.connection import connect, parse_mysql_uri  # noqa: E402
from app.config import settings  # noqa: E402

FILES = ("order_schema.sql", "service_schema.sql")
TABLES = (
    "merchant_order", "merchant_order_import_payload", "merchant_order_line", "merchant_order_event",
    "service_conversation", "service_message", "quick_reply",
)


def statements(path: Path) -> list[str]:
    """本迁移只允许 CREATE TABLE IF NOT EXISTS，拒绝其它 SQL。"""
    body = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("--"))
    result = [part.strip() for part in body.split(";") if part.strip()]
    expected = 4 if path.name == "order_schema.sql" else 3
    if len(result) != expected or any(not part.upper().startswith("CREATE TABLE IF NOT EXISTS ") for part in result):
        raise ValueError(f"迁移内容不符合预期：{path.name}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行建表并只读核对；省略则仅预览 SQL")
    args = parser.parse_args()
    batches = [(name, statements(ROOT / "sql" / name)) for name in FILES]
    if not settings.mysql_uri:
        print("未配置 MYSQL_URI；不能确认目标库")
        return 1
    info = parse_mysql_uri(settings.mysql_uri)
    print(f"目标：{info['host']}:{info['port']}/{info['database']}（不显示口令）")
    for name, stmts in batches:
        print(f"{name}: {len(stmts)} 条 CREATE TABLE IF NOT EXISTS")
    if not args.apply:
        print("仅预览，未连接数据库。传 --apply 才会执行。")
        return 0
    with connect() as conn, conn.cursor() as cur:
        for name, stmts in batches:
            for stmt in stmts:
                cur.execute(stmt)
            print(f"已执行：{name}")
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
            "AND table_name IN (" + ",".join(["%s"] * len(TABLES)) + ")",
            (info["database"], *TABLES),
        )
        present = {row[0] for row in cur.fetchall()}
    for table in TABLES:
        print(f"{'OK' if table in present else 'MISSING'} {table}")
    return 0 if present == set(TABLES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
