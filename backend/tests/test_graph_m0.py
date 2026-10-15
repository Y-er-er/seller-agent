"""M0 端到端测试：图可 invoke + 转人工可挂起/恢复。

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_graph_m0.py

说明：取数节点为 async，检查点用 AsyncSqliteSaver，故整条链路以异步方式驱动。

覆盖：
    C1 小额未签收 → auto_pass，一次跑完（不挂起）
    C2 大额质量主张 → to_human，挂起；resume 后完成并记录复核人
    C3 数据不足（未注入 mock）→ 转人工
    C4 同一输入两次执行结论一致（可复现）
    C5 审计事件齐备（各节点均留痕）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langgraph.types import Command  # noqa: E402

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402

failures: list[str] = []

# 审计应覆盖的节点集合
_REQUIRED_STAGES = {
    "intake", "fetch_order", "fetch_logistics", "fetch_profile",
    "evidence", "judge", "risk", "dispose", "audit",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印用例结果；detail 仅在失败时输出，避免给通过项附加误导性文案。"""
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


def cfg(order_id: str) -> dict:
    """thread_id = 受理号。"""
    return {"configurable": {"thread_id": order_id}}


async def run_checks(graph) -> None:
    # ---------------- C1 小额未签收 → 自动通过 ----------------
    oid1 = "T-M0-001"
    seed_order(oid1, pay_amount=39.0, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_logistics(oid1, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_profile(oid1, buyer_credit_score=600, buyer_refund_rate_90d=0.05, refund_count_90d=1)

    r1 = await graph.ainvoke(
        {
            "request": RefundRequest(
                order_id=oid1, pay_amount=39.0, claim_amount=39.0,
                refund_reason_code=RefundReason.NOT_RECEIVED,
            )
        },
        config=cfg(oid1),
    )
    check("C1 小额未签收 → auto_pass", r1.get("conclusion") == "auto_pass", f"实得 {r1.get('conclusion')}")
    check("C1 命中 R2", "R2" in list(r1.get("hits", [])), f"hits={list(r1.get('hits', []))}")
    check("C1 未挂起", not r1.get("__interrupt__"), "出现了意外中断")
    # 影子模式：auto_pass 路径会经过 dispose，故应产出"未落地执行"的动作记录
    act1 = r1.get("action")
    check("C1 影子模式未落地执行", act1 is not None and act1.executed is False, f"action={act1}")

    # ---------------- C2 大额 + 质量主张 → 转人工并可恢复 ----------------
    oid2 = "T-M0-002"
    seed_order(oid2, pay_amount=1800.0, logistics_status=LogisticsStatus.SIGNED)
    seed_logistics(oid2, logistics_status=LogisticsStatus.SIGNED)
    seed_profile(oid2, buyer_credit_score=700, buyer_refund_rate_90d=0.05, refund_count_90d=0)

    r2 = await graph.ainvoke(
        {
            "request": RefundRequest(
                order_id=oid2, pay_amount=1800.0, claim_amount=1800.0,
                refund_reason_code=RefundReason.QUALITY,
            )
        },
        config=cfg(oid2),
    )
    check("C2 大额质量 → to_human", r2.get("conclusion") == "to_human", f"实得 {r2.get('conclusion')}")
    r2_hits = set(r2.get("hits", []))
    check("C2 命中 R1 金额闸门", "R1" in r2_hits, f"hits={sorted(r2_hits)}")
    check("C2 同时记录 R7 质量红线", "R7" in r2_hits, f"hits={sorted(r2_hits)}")
    check("C2 已挂起待复核", bool((await graph.aget_state(cfg(oid2))).next), "未挂起")

    r2b = await graph.ainvoke(
        Command(resume={"decision": "maintain", "reviewer": "qa-zhang"}),
        config=cfg(oid2),
    )
    check("C2 恢复后记录复核结论", r2b.get("review_decision") == "maintain", f"实得 {r2b.get('review_decision')}")
    check("C2 恢复后记录复核人", r2b.get("reviewer") == "qa-zhang", f"实得 {r2b.get('reviewer')}")
    check("C2 恢复后不再挂起", not (await graph.aget_state(cfg(oid2))).next, "恢复后仍处于挂起态")
    # 说明：to_human 路径按方案 §4 直接旁路到 human_review，**不经过 risk / dispose**，
    # 因此这里不应出现 action 记录——影子模式断言放在 C1（auto_pass 路径）验证。

    # ---------------- C3 数据不足 → 转人工 ----------------
    oid3 = "T-M0-003"  # 故意不注入任何 mock
    r3 = await graph.ainvoke(
        {
            "request": RefundRequest(
                order_id=oid3, pay_amount=100.0, claim_amount=100.0,
                refund_reason_code=RefundReason.NOT_RECEIVED,
            )
        },
        config=cfg(oid3),
    )
    check("C3 数据不足 → to_human", r3.get("conclusion") == "to_human", f"实得 {r3.get('conclusion')}")

    # ---------------- C4 可复现 ----------------
    r4 = await graph.ainvoke(
        {
            "request": RefundRequest(
                order_id=oid1, pay_amount=39.0, claim_amount=39.0,
                refund_reason_code=RefundReason.NOT_RECEIVED,
            )
        },
        config=cfg("T-M0-004"),
    )
    check(
        "C4 同输入同结论",
        r4.get("conclusion") == r1.get("conclusion") and list(r4.get("hits", [])) == list(r1.get("hits", [])),
        f"{r4.get('conclusion')}/{list(r4.get('hits', []))}",
    )

    # ---------------- C5 审计留痕 ----------------
    stages = {getattr(e, "stage", None) for e in r1.get("audit_events", [])}
    missing = _REQUIRED_STAGES - stages
    check("C5 审计覆盖全部节点", not missing, f"缺失={sorted(missing)}")
    check("C5 规则版本入留痕", bool(r1.get("rule_version")), f"rule_version={r1.get('rule_version')}")


async def amain() -> int:
    """异步主流程：建检查点 → 跑用例 → 关连接并清理文件。"""
    # 单元测试保持**无外部依赖**：关闭审计落库（MySQL 集成验证见 test_audit_mysql.py）
    settings.audit_db_enabled = False

    ckpt_path = PROJECT_ROOT / "data" / "test_ckpt_m0.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)

    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    try:
        await run_checks(graph)
    finally:
        # 先关连接再删文件：Windows 下 SQLite 连接会占用文件句柄
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
