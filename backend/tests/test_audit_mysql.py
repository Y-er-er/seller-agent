"""审计落库集成测试（**真实打 MySQL**）。

与其余测试的区别：本文件是唯一会连接外部数据库的测试。
因此它必须**自清理**——写入的审计行在断言后立即删除，不污染 `decision_audit`。

前置：`backend/.env` 中配置了可用的 `MYSQL_URI`，且已执行 `sql/decision_audit.sql`。
未配置时本测试打印 SKIP 并以 0 退出（不视为失败，保证 CI 无数据库也能通过）。

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_audit_mysql.py

覆盖：
    M1 判定完成后审计行已写入 MySQL，且关键字段正确
    M2 命中明细（field_values）以 JSON 落库
    M3 数据库不可用时**自动降级**：图仍正常跑完，audit_persisted=False
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.db.connection import connect  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402

ORDER_A = "T-AUDIT-OK"
ORDER_B = "T-AUDIT-DEGRADED"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


def fetch_rows(request_id: str) -> list[tuple]:
    """从 MySQL 读取该受理号的审计行（同步调用，测试场景可接受）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, order_id, conclusion, hit_rules, rule_version,"
            " field_values, executed, shadow FROM decision_audit WHERE request_id=%s",
            (request_id,),
        )
        return list(cur.fetchall())


def delete_rows(request_id: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        return cur.execute("DELETE FROM decision_audit WHERE request_id=%s", (request_id,))


def seed(order_id: str, amount: float) -> None:
    seed_order(order_id, pay_amount=amount, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_logistics(order_id, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_profile(order_id, buyer_credit_score=600, buyer_refund_rate_90d=0.05, refund_count_90d=1)


async def amain() -> int:
    if not settings.mysql_uri:
        print("SKIP：未配置 MYSQL_URI，跳过审计落库集成测试")
        return 0

    # 先探测可达性：数据库临时不可用时 SKIP，而不是让连接异常中断测试
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP：审计库不可用（{type(exc).__name__}: {exc}），跳过审计落库集成测试")
        return 0

    original_enabled = settings.audit_db_enabled
    original_uri = settings.mysql_uri

    ckpt_path = PROJECT_ROOT / "data" / "test_audit_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)
    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    try:
        # ---------------- M1 / M2 正常落库 ----------------
        settings.audit_db_enabled = True
        seed(ORDER_A, 39.0)
        result = await graph.ainvoke(
            {
                "request": RefundRequest(
                    order_id=ORDER_A, pay_amount=39.0, claim_amount=39.0,
                    refund_reason_code=RefundReason.NOT_RECEIVED,
                )
            },
            config={"configurable": {"thread_id": ORDER_A}},
        )

        rows = fetch_rows(ORDER_A)
        check("M1 审计行已写入 MySQL", len(rows) == 1, f"实得 {len(rows)} 行")
        if rows:
            r = rows[0]
            check("M1 结论字段正确", r[2] == "auto_pass", f"实得 {r[2]}")
            check("M1 命中规则字段正确", r[3] == "R2", f"实得 {r[3]}")
            check("M1 规则版本已记录", bool(r[4]), f"实得 {r[4]}")
            check("M1 影子模式标记正确", int(r[7]) == 1, f"实得 {r[7]}")
            check("M2 命中明细以 JSON 落库", isinstance(r[5], str) and "R2" in r[5], f"实得 {r[5][:80] if r[5] else r[5]}")

        persisted_flag = None
        for ev in result.get("audit_events", []):
            if getattr(ev, "stage", None) == "audit":
                persisted_flag = ev.payload.get("audit_persisted")
        check("M1 审计事件标注已落库", persisted_flag is True, f"实得 {persisted_flag}")

        # ---------------- M3 数据库不可用 → 自动降级 ----------------
        settings.mysql_uri = "mysql+pymysql://bad:bad@127.0.0.1:13306/nodb?charset=utf8mb4"
        seed(ORDER_B, 39.0)
        degraded = await graph.ainvoke(
            {
                "request": RefundRequest(
                    order_id=ORDER_B, pay_amount=39.0, claim_amount=39.0,
                    refund_reason_code=RefundReason.NOT_RECEIVED,
                )
            },
            config={"configurable": {"thread_id": ORDER_B}},
        )
        check("M3 数据库不可用仍跑完判定", degraded.get("conclusion") == "auto_pass",
              f"实得 {degraded.get('conclusion')}")
        flag = None
        for ev in degraded.get("audit_events", []):
            if getattr(ev, "stage", None) == "audit":
                flag = ev.payload.get("audit_persisted")
        check("M3 审计事件标注未落库（已降级）", flag is False, f"实得 {flag}")

        settings.mysql_uri = original_uri
        check("M3 降级时未写入脏数据", len(fetch_rows(ORDER_B)) == 0)
    finally:
        settings.mysql_uri = original_uri
        settings.audit_db_enabled = original_enabled
        # 自清理：删除本测试写入的行
        try:
            removed = delete_rows(ORDER_A) + delete_rows(ORDER_B)
            print(f"        已清理测试数据：{removed} 行")
        except Exception as exc:  # noqa: BLE001
            print(f"        ⚠️ 清理失败，请手工删除 request_id in ('{ORDER_A}','{ORDER_B}')：{exc}")
        await checkpointer.conn.close()
        ckpt_path.unlink(missing_ok=True)

    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
