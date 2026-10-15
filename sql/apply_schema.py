"""Schema 应用器（幂等）—— 建表 + 增列 + 回读校验。

为什么需要它（而不是直接贴 SQL）：
- MySQL 8.0 **不支持** `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，单纯的 SQL 文件无法做到幂等；
- 本脚本对"建表"用 SQL 文件的 `IF NOT EXISTS`，对"增列"先查 `information_schema` 再决定是否执行，
  因此**可以反复运行**，Dev / Test / 生产同一套流程。

用法：
    python sql/apply_schema.py            # 应用（建表 + 补列）并回读校验
    python sql/apply_schema.py --check    # 只校验，不改动任何结构

连接串取自 `backend/.env` 的 `MYSQL_URI`（纪律 9：不在此硬编码任何连接信息）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许以 `python sql/apply_schema.py` 方式直接运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db.connection import connect, parse_mysql_uri  # noqa: E402

# schema 文件（按执行顺序）
SCHEMA_FILES = ("decision_audit.sql", "chat_schema.sql")

# decision_audit 的增量列（P1.1：卷宗与护栏所需）
# 列名 -> (DDL 片段, 存在性说明)
EXTRA_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "decision_audit": {
        "review_reason": (
            "review_reason VARCHAR(255) NULL COMMENT '人工复核说明：改判理由**必填**（R1）'",
            "改判理由（R1 必填）",
        ),
        "sla_deadline": (
            "sla_deadline DATETIME NULL COMMENT '本单处理时限（补证 / 复核 SLA）'",
            "处理时限",
        ),
        "guardrail_hits": (
            "guardrail_hits VARCHAR(255) NOT NULL DEFAULT '' COMMENT '本单触发的护栏类型，逗号分隔'",
            "护栏触发汇总",
        ),
    },
    "conversation_case": {
        "workflow_status": (
            "workflow_status VARCHAR(24) NOT NULL DEFAULT 'queued' "
            "COMMENT '工单状态（指挥台）：queued / agent_running / awaiting_confirm / resolved / escalated'",
            "工单状态",
        ),
        "assignee": (
            "assignee VARCHAR(16) NULL COMMENT '处理者：agent（智能体）/ manual（管理员）'",
            "处理者",
        ),
        "decision_result": (
            "decision_result VARCHAR(24) NULL "
            "COMMENT '判定处置结果：refund / reship / reject / manual / pending_evidence'",
            "判定处置结果",
        ),
        "decision_amount": (
            "decision_amount DECIMAL(12,2) NULL COMMENT '判定金额（退款金额等，元）'",
            "判定金额",
        ),
        "confirmed_by": (
            "confirmed_by VARCHAR(64) NULL COMMENT '确认人（管理员）'",
            "确认人",
        ),
        "confirmed_at": (
            "confirmed_at DATETIME NULL COMMENT '确认时间'",
            "确认时间",
        ),
    },
}


def split_sql(raw: str) -> list[str]:
    """把 SQL 文件切成语句列表。

    ⚠️ 必须先**逐行剔除 `--` 注释行**再按 `;` 切分：
    否则以注释开头的首个语句块会被整块丢弃（本项目曾因此出现"已执行 0 条 DDL"）。
    """
    body = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def column_exists(cur, database: str, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
        (database, table, column),
    )
    return cur.fetchone()[0] > 0


def table_columns(cur, database: str, table: str) -> list[tuple[str, str]]:
    cur.execute(
        "SELECT column_name, column_type FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (database, table),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def main() -> int:
    check_only = "--check" in sys.argv
    if not settings.mysql_uri:
        print("[失败] backend/.env 未配置 MYSQL_URI，无法应用 schema")
        return 1

    info = parse_mysql_uri(settings.mysql_uri)
    db_name = info["database"]
    print(f"目标库：{info['host']}:{info['port']}/{db_name}  用户={info['user']}")

    with connect() as conn, conn.cursor() as cur:
        # ---------- 1) 建表（幂等）----------
        for name in SCHEMA_FILES:
            path = PROJECT_ROOT / "sql" / name
            statements = split_sql(path.read_text(encoding="utf-8"))
            if check_only:
                print(f"  [check] {name}：解析出 {len(statements)} 条语句（未执行）")
                continue
            for stmt in statements:
                cur.execute(stmt)
            print(f"  已应用 {name}（{len(statements)} 条语句）")

        # ---------- 2) 补列（幂等：先查后加）----------
        for table, columns in EXTRA_COLUMNS.items():
            for column, (ddl, desc) in columns.items():
                if column_exists(cur, db_name, table, column):
                    print(f"  列已存在，跳过：{table}.{column}（{desc}）")
                    continue
                if check_only:
                    print(f"  [check] 缺少列：{table}.{column}（{desc}）→ 需执行 ALTER")
                    continue
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                print(f"  已新增列：{table}.{column}（{desc}）")

        # ---------- 3) 回读校验 ----------
        print("\n=== 回读校验 ===")
        expected_tables = ("decision_audit", "conversation_case", "conversation_message",
                       "case_slot", "guardrail_event", "llm_usage")
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s ORDER BY table_name",
            (db_name,),
        )
        present = {row[0] for row in cur.fetchall()}
        ok = True
        for name in expected_tables:
            exists = name in present
            ok = ok and exists
            print(f"  {'OK  ' if exists else 'MISS'} {name}")
        print(f"  库内全部表：{sorted(present)}")

        for table, columns in EXTRA_COLUMNS.items():
            cols = table_columns(cur, db_name, table)
            have = {c for c, _ in cols}
            missing = [c for c in columns if c not in have]
            ok = ok and not missing
            print(f"  {table} 列数={len(cols)}  缺失={missing or '无'}")

    print(f"\n结果：{'全部就绪' if ok else '存在缺失'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
