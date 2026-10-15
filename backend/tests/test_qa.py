"""口径问答测试（**全部离线**，默认零模型调用）。

覆盖目标：
1. 知识库加载期严格校验（写错就应立刻失败，而不是静默失效）；
2. 检索可复现、无匹配不硬答；
3. 事实解释来自规则命中明细（含中文标签），不来自模型；
4. **只读性**：问答前后，图状态（结论 / 命中规则）完全不变；
5. **零成本**：未开启润色闸门时，`model_used` 恒为 False。

运行（无需 MySQL、无网络、零费用）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_qa.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.graph.queries import get_thread_state  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.qa import answer_question, list_entries, search  # noqa: E402
from app.qa.knowledge import load_knowledge  # noqa: E402
from app.qa.labels import describe_leaf  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


def expect_value_error(name: str, yaml_text: str) -> None:
    """加载期非法输入必须抛 ValueError（与规则表加载器保持一致的异常类型）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "kb.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        try:
            load_knowledge(path)
        except ValueError:
            check(name, True)
        except Exception as exc:  # noqa: BLE001 — 其它异常类型即视为不符合约定
            check(name, False, f"抛出了 {type(exc).__name__}：{exc}")
        else:
            check(name, False, "未抛出任何异常")


# ============================================================
# Q1 知识库加载与校验
# ============================================================
def run_knowledge_checks() -> None:
    print("【Q1】知识库加载与加载期校验")
    entries = list_entries()
    check("Q1 知识库条目非空", len(entries) >= 10, f"实得 {len(entries)} 条")
    check("Q1 条目均含 id/title/keywords",
          all(e["id"] and e["title"] and e["keywords"] for e in entries))
    check("Q1 质量红线口径存在",
          any(e["id"] == "KB-QUALITY-ALWAYS-HUMAN" for e in entries))

    expect_value_error("Q1 条目缺 id → 抛 ValueError",
                       "version: t\nentries:\n  - title: a\n    keywords: [x]\n    answer: y\n")
    expect_value_error("Q1 条目缺 keywords → 抛 ValueError",
                       "version: t\nentries:\n  - id: A\n    title: a\n    answer: y\n")
    expect_value_error("Q1 条目 ID 重复 → 抛 ValueError",
                       "version: t\nentries:\n"
                       "  - id: A\n    title: a\n    keywords: [x]\n    answer: y\n"
                       "  - id: A\n    title: b\n    keywords: [z]\n    answer: w\n")
    expect_value_error("Q1 entries 非列表 → 抛 ValueError", "version: t\nentries: nope\n")


# ============================================================
# Q2 检索
# ============================================================
def run_search_checks() -> None:
    print()
    print("【Q2】口径检索")
    hit = search("质量问题为什么不自动判定", limit=3)
    check("Q2 命中质量红线口径",
          hit and hit[0][0].id == "KB-QUALITY-ALWAYS-HUMAN", f"实得 {[e.id for e, _ in hit]}")

    hit2 = search("金额闸门是多少", limit=3)
    check("Q2 命中金额闸门口径",
          hit2 and hit2[0][0].id == "KB-AMOUNT-GATE", f"实得 {[e.id for e, _ in hit2]}")

    check("Q2 无关问题不硬答（返回空）", search("完全无关的问题 zzz", limit=3) == [])
    check("Q2 空问题返回空", search("   ", limit=3) == [])

    first = [(e.id, s) for e, s in search("补证时限多久", limit=3)]
    second = [(e.id, s) for e, s in search("补证时限多久", limit=3)]
    check("Q2 检索结果可复现（同输入同输出）", first == second, f"{first} vs {second}")

    check("Q2 长关键词权重更高（更具体的口径排前）",
          bool(first) and first[0][0] == "KB-EVIDENCE-DEADLINE", f"实得 {first}")


# ============================================================
# Q3 标签中文化
# ============================================================
def run_label_checks() -> None:
    print()
    print("【Q3】命中明细中文化")
    cases = [
        ({"field": "claim_amount", "op": "gte", "threshold": 300, "actual": 1200},
         "申请退款金额 ≥ ¥300.00（实际 ¥1,200.00）"),
        ({"field": "refund_reason_code", "op": "eq", "threshold": "quality", "actual": "quality"},
         "退款原因 = 质量问题"),
        ({"field": "buyer_refund_rate_90d", "op": "lte", "threshold": 0.2, "actual": 0.2},
         "90天退款率 ≤ 20.0%（实际 20.0%）"),
        ({"field": "refund_count_90d", "op": "gte", "threshold": 6, "actual": 6},
         "90天退款笔数 ≥ 6（实际 6）"),
    ]
    for leaf, expected in cases:
        actual = describe_leaf(leaf)
        check(f"Q3 中文化 {leaf['field']}", actual == expected, f"实得「{actual}」")

    # 不得把内部代码直接透出给业务岗
    text = describe_leaf(cases[0][0]) + describe_leaf(cases[1][0])
    leaked = [t for t in ("gte", "lte", "claim_amount", "refund_reason_code") if t in text]
    check("Q3 不泄露字段/运算符代码", not leaked, f"仍暴露 {leaked}")


# ============================================================
# Q4 无受理号问答
# ============================================================
async def run_plain_qa() -> None:
    print()
    print("【Q4】通用口径问答（零模型调用）")
    qa_llm_polish_backup = settings.qa_llm_polish
    settings.qa_llm_polish = False

    r = await answer_question("质量问题为什么不自动判定？")
    check("Q4 返回边界声明", "不参与资格判定" in r["boundary"], f"实得 {r['boundary']}")
    check("Q4 默认不调用模型", r["model_used"] is False, f"实得 {r['model_used']}")
    check("Q4 引用质量红线口径",
          any(s["id"] == "KB-QUALITY-ALWAYS-HUMAN" for s in r["sources"]),
          f"实得 {r['sources']}")
    check("Q4 回答含口径正文", "不做质量判定" in r["answer"])

    r2 = await answer_question("完全无关的问题 zzz")
    check("Q4 无匹配时给出引导而非沉默", "可以换个问法" in r2["answer"], f"实得 {r2['answer'][:40]}")
    check("Q4 无匹配时 sources 为空", r2["sources"] == [])

    # 即使总开关打开，未开润色闸门依然零模型调用
    settings.llm_enabled = True
    r3 = await answer_question("质量问题为什么不自动判定？")
    check("Q4 润色闸门关闭 → 仍为零模型调用", r3["model_used"] is False)
    settings.qa_llm_polish = qa_llm_polish_backup


# ============================================================
# Q5 带受理号的事实解释 + 只读性
# ============================================================
async def run_order_qa(graph) -> None:
    print()
    print("【Q5】带受理号的事实解释（确定性）")
    oid = "T-QA-1"
    seed_order(oid, pay_amount=1800.0, logistics_status=LogisticsStatus.SIGNED)
    seed_logistics(oid, logistics_status=LogisticsStatus.SIGNED)
    seed_profile(oid, buyer_credit_score=700, buyer_refund_rate_90d=0.05, refund_count_90d=0)

    request = RefundRequest(order_id=oid, pay_amount=1800.0, claim_amount=1800.0,
                            refund_reason_code=RefundReason.QUALITY)
    cfg = {"configurable": {"thread_id": oid}}
    await graph.ainvoke({"request": request}, config=cfg)

    before = await get_thread_state(graph, oid)
    r = await answer_question("这笔为什么转人工？", order_id=oid, graph=graph)
    after = await get_thread_state(graph, oid)

    check("Q5 返回事实解释", r["facts"] is not None)
    check("Q5 结论为转人工",
          r["facts"] and r["facts"]["conclusion"] == "to_human",
          f"实得 {r['facts'] and r['facts']['conclusion']}")
    check("Q5 命中规则已列出（含 R1/R7）",
          r["facts"] and {"R1", "R7"}.issubset(set(r["facts"]["hit_rules"])),
          f"实得 {r['facts'] and r['facts']['hit_rules']}")
    check("Q5 依据逐条中文化", any("申请退款金额" in line for line in r["facts"]["rule_explanations"]),
          f"实得 {r['facts']['rule_explanations']}")
    check("Q5 按命中规则交叉召回口径",
          any(s["id"] == "KB-AMOUNT-GATE" for s in r["sources"]) or
          any(s["id"] == "KB-QUALITY-ALWAYS-HUMAN" for s in r["sources"]),
          f"实得 {r['sources']}")
    # 有受理号时，口径应优先由**实际命中规则**决定，避免被无关关键词带偏
    hit_rule_set = set(r["facts"]["hit_rules"])
    unrelated = [
        s["id"] for s in r["sources"]
        if s["related_rules"] and not (set(s["related_rules"]) & hit_rule_set)
    ]
    check("Q5 不含与命中规则无关的口径", not unrelated, f"实得 {unrelated}")
    check("Q5 问答不调用模型", r["model_used"] is False)

    # 只读性：问答是纯旁路，绝不能改变任何状态
    check("Q6 问答前后结论不变",
          before["conclusion"] == after["conclusion"],
          f"{before['conclusion']} → {after['conclusion']}")
    check("Q6 问答前后命中规则不变",
          sorted(before["hit_rules"]) == sorted(after["hit_rules"]),
          f"{sorted(before['hit_rules'])} → {sorted(after['hit_rules'])}")
    check("Q6 问答前后挂起状态不变",
          before["status"] == after["status"], f"{before['status']} → {after['status']}")

    # 不存在的受理号：不报错，给出提示并只答通用口径
    r2 = await answer_question("这笔为什么转人工？", order_id="NOT-EXIST-XYZ", graph=graph)
    check("Q7 受理号不存在时不报错", r2["facts"] is None)
    check("Q7 受理号不存在时给出明确提示", "未找到受理号" in r2["answer"], f"实得 {r2['answer'][:60]}")
    check("Q7 受理号不存在时仍返回边界声明", "不参与资格判定" in r2["boundary"])


async def amain() -> int:
    original = {
        "llm_enabled": settings.llm_enabled,
        "qa_llm_polish": settings.qa_llm_polish,
        "audit_db_enabled": settings.audit_db_enabled,
    }
    settings.audit_db_enabled = False
    settings.llm_enabled = False  # 全程默认关闭模型（零费用）

    ckpt_path = PROJECT_ROOT / "data" / "test_qa_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)
    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    try:
        run_knowledge_checks()
        run_search_checks()
        run_label_checks()
        await run_plain_qa()
        await run_order_qa(graph)
    finally:
        for key, value in original.items():
            setattr(settings, key, value)
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
