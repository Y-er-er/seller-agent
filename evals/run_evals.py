"""验收回归跑批器（方案 §14）。

从 `evals/cases.yaml` 读取用例，**端到端**驱动图执行，断言：
    结论 conclusion / 命中规则 hit_rules / 是否挂起 pending / （可选）复核恢复

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe evals/run_evals.py

退出码：0 = 全部通过；1 = 存在失败用例（可直接用于 CI）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVALS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import yaml  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.config import settings  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.models import RefundRequest  # noqa: E402
from app.rules.disposal import resolve_disposal  # noqa: E402
from app.tools import aclose, seed_logistics, seed_order, seed_profile  # noqa: E402

CASES_FILE = EVALS_DIR / "cases.yaml"

failures: list[str] = []


def record(ok: bool, case_id: str, what: str, expected, actual) -> None:
    if not ok:
        failures.append(f"{case_id} {what}: 期望 {expected!r}，实得 {actual!r}")


async def run_case(graph, case: dict) -> None:
    """执行单个用例并核对全部断言。"""
    case_id = case["id"]
    order_id = case["request"]["order_id"]
    seed = case.get("seed", {})

    # ---- 注入 mock 事实（未配置 *_API_BASE 时生效）----
    if "order" in seed:
        seed_order(order_id, **seed["order"])
    if "logistics" in seed:
        seed_logistics(order_id, **seed["logistics"])
    if "profile" in seed:
        seed_profile(order_id, **seed["profile"])

    config = {"configurable": {"thread_id": f"eval-{case_id}-{order_id}"}}
    result = await graph.ainvoke({"request": RefundRequest(**case["request"])}, config=config)

    expect = case.get("expect", {})

    # ---- 断言 1：结论 ----
    actual_conclusion = result.get("conclusion")
    ok = actual_conclusion == expect.get("conclusion")
    record(ok, case_id, "结论", expect.get("conclusion"), actual_conclusion)

    # ---- 断言 2：命中规则（集合相等，顺序无关）----
    actual_hits = set(result.get("hits", []))
    expected_hits = set(expect.get("hit_rules", []))
    ok = actual_hits == expected_hits
    record(ok, case_id, "命中规则", sorted(expected_hits), sorted(actual_hits))

    # ---- 断言 2.5（可选）：处置结果（第一步新增，诉求+结论→处置的确定性映射）----
    if "disposal" in expect:
        claim_type = case["request"].get("claim_type", "refund")
        actual_disposal = resolve_disposal(claim_type, actual_conclusion)
        ok = actual_disposal == expect.get("disposal")
        record(ok, case_id, "处置结果", expect.get("disposal"), actual_disposal)

    # ---- 断言 3：是否挂起待复核 ----
    snapshot = await graph.aget_state(config)
    actual_pending = bool(snapshot.next)
    ok = actual_pending == bool(expect.get("pending"))
    record(ok, case_id, "是否挂起", bool(expect.get("pending")), actual_pending)

    # ---- 断言 4（可选）：提交人工复核决定并恢复 ----
    resume = case.get("resume")
    if resume:
        resumed = await graph.ainvoke(
            Command(resume={"decision": resume["decision"], "reviewer": resume["reviewer"]}),
            config=config,
        )
        actual_decision = resumed.get("review_decision")
        ok = actual_decision == resume.get("expect_review_decision")
        record(ok, case_id, "复核决定", resume.get("expect_review_decision"), actual_decision)
        ok = not (await graph.aget_state(config)).next
        record(ok, case_id, "恢复后不再挂起", True, False)

    mark = "PASS" if not any(f.startswith(case_id + " ") for f in failures) else "FAIL"
    print(
        f"[{mark}] {case_id:<4} {case['name']:<28} "
        f"结论={actual_conclusion:<17} 命中={sorted(actual_hits)}"
    )


async def amain() -> int:
    # 跑批保持无外部依赖：关闭审计落库，避免污染生产表
    # （MySQL 落库链路由 backend/tests/test_audit_mysql.py 单独验证）
    settings.audit_db_enabled = False

    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8")).get("cases", [])
    if not cases:
        print(f"未从 {CASES_FILE} 读到任何用例")
        return 1

    ckpt_path = PROJECT_ROOT / "data" / "evals_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)

    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    try:
        print(f"用例数：{len(cases)}  |  检查点：{ckpt_path}")
        print("-" * 78)
        for case in cases:
            await run_case(graph, case)
    finally:
        await checkpointer.conn.close()
        await aclose()
        ckpt_path.unlink(missing_ok=True)

    print("-" * 78)
    if failures:
        print(f"结果：{len(failures)} 项断言未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"结果：全部通过（{len(cases)} 个用例）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
