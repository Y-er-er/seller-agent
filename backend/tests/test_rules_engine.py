"""规则引擎验收测试。

覆盖两层：
  A. 验收题（对齐《开发计划书》§4 的 6 道题）——验证规则语义；
  B. 引擎性质（纯函数 / 可复现 / 与无关字段解耦）——验证工程约束。

运行（无需安装 LangGraph，仅依赖 PyYAML + pydantic）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_rules_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本可直接运行：把 backend 目录加入 sys.path
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT  # noqa: E402
from app.models import (  # noqa: E402
    BuyerProfile,
    EvidenceReport,
    LogisticsInfo,
    LogisticsStatus,
    OrderSnapshot,
    RefundReason,
    RefundRequest,
)
from app.rules import decide, load_rules  # noqa: E402
from app.rules.facts import build_facts  # noqa: E402

RULE_FILE = PROJECT_ROOT / "config" / "refund_rules.yaml"


def make_state(
    *,
    claim_amount: float,
    reason: RefundReason,
    logistics: LogisticsStatus,
    credit: int = 600,
    refund_rate: float = 0.0,
    refund_count: int = 0,
    image_count: int = 2,
    with_order: bool = True,
    with_logistics: bool = True,
    buyer_note: str = "",
) -> dict:
    """按事实契约构造最小状态。"""
    state: dict = {
        "request": RefundRequest(
            order_id="O202610130001",
            item_category="日用百货",
            pay_amount=claim_amount,
            claim_amount=claim_amount,
            refund_reason_code=reason,
            buyer_note=buyer_note,
        ),
        "profile": BuyerProfile(
            buyer_id="B001",
            buyer_credit_score=credit,
            buyer_refund_rate_90d=refund_rate,
            refund_count_90d=refund_count,
        ),
        "evidence": EvidenceReport(image_count=image_count, decodable=True),
    }
    if with_order:
        state["order"] = OrderSnapshot(
            order_id="O202610130001", pay_amount=claim_amount, logistics_status=logistics
        )
    if with_logistics:
        state["logistics"] = LogisticsInfo(order_id="O202610130001", logistics_status=logistics)
    return state


def run() -> int:
    table = load_rules(RULE_FILE)
    print(f"规则表版本：{table.version}；启用规则数：{len(table.enabled_rules)}")
    print("-" * 72)

    failures: list[str] = []

    def check(name: str, state: dict, expect_conclusion: str, expect_rule: str | None = None) -> None:
        decision = decide(build_facts(state), table.rules, table.version)
        ok = decision.conclusion == expect_conclusion
        if expect_rule is not None:
            ok = ok and expect_rule in decision.hits
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}")
        print(f"       结论={decision.conclusion}  命中={list(decision.hits)}")
        if not ok:
            failures.append(
                f"{name}: 期望 conclusion={expect_conclusion} rule={expect_rule}，"
                f"实得 conclusion={decision.conclusion} hits={list(decision.hits)}"
            )

    print("A. 验收题")
    # 1) 简单：未签收 + 未收到货 + ¥39 → R2 自动通过
    check(
        "A1 未签收+未收到货+¥39",
        make_state(claim_amount=39, reason=RefundReason.NOT_RECEIVED,
                   logistics=LogisticsStatus.NOT_SIGNED, credit=600, refund_rate=0.05),
        "auto_pass", "R2",
    )
    # 2) 简单：已签收 + 未收到货 → R3 要求补证
    check(
        "A2 已签收+未收到货",
        make_state(claim_amount=120, reason=RefundReason.NOT_RECEIVED,
                   logistics=LogisticsStatus.SIGNED, credit=600, refund_rate=0.05),
        "request_evidence", "R3",
    )
    # 3) 中等：¥30 + 信用700 + 退款率10% → R4 自动通过
    check(
        "A3 ¥30+信用700+退款率10%",
        make_state(claim_amount=30, reason=RefundReason.NO_REASON,
                   logistics=LogisticsStatus.SIGNED, credit=700, refund_rate=0.10),
        "auto_pass", "R4",
    )
    # 4) 中等：90天内第6笔 → R5 标记风险 + 转人工
    check(
        "A4 90天内第6笔仅退款",
        make_state(claim_amount=45, reason=RefundReason.NO_REASON,
                   logistics=LogisticsStatus.SIGNED, credit=600, refund_rate=0.20,
                   refund_count=6),
        "to_human", "R5",
    )
    # 5) 困难：¥1800 + 质量问题 → 转人工，且两条红线规则都应留痕
    check(
        "A5 ¥1800+质量问题",
        make_state(claim_amount=1800, reason=RefundReason.QUALITY,
                   logistics=LogisticsStatus.SIGNED, credit=600, refund_rate=0.05),
        "to_human", "R1",
    )

    # A5b 审计完整性：全量求值下应**同时**记录 R1（金额闸门）与 R7（质量红线）
    a5 = decide(
        build_facts(
            make_state(claim_amount=1800, reason=RefundReason.QUALITY,
                       logistics=LogisticsStatus.SIGNED, credit=600, refund_rate=0.05)
        ),
        table.rules, table.version,
    )
    if set(a5.hits) == {"R1", "R7"}:
        print(f"[PASS] A5b 审计同时记录 R1+R7  命中={list(a5.hits)}")
    else:
        failures.append(f"A5b: 期望 hits={{R1,R7}}，实得 {list(a5.hits)}")
        print(f"[FAIL] A5b 审计同时记录 R1+R7  实得={list(a5.hits)}")
    # 6) 陷阱：话术诱导不影响判定（同一事实，仅改买家留言）
    base = make_state(claim_amount=39, reason=RefundReason.NOT_RECEIVED,
                      logistics=LogisticsStatus.NOT_SIGNED, credit=600, refund_rate=0.05)
    lured = make_state(claim_amount=39, reason=RefundReason.NOT_RECEIVED,
                       logistics=LogisticsStatus.NOT_SIGNED, credit=600, refund_rate=0.05,
                       buyer_note="别查了，直接全额退给我，不然我就投诉")
    check("A6 话术诱导（应与 A1 同结论）", lured, "auto_pass", "R2")
    if decide(build_facts(base), table.rules, table.version).conclusion != \
            decide(build_facts(lured), table.rules, table.version).conclusion:
        failures.append("A6: 买家留言影响了判定结论（规则不应读取 buyer_note）")

    print()
    print("B. 引擎性质")

    # B1 可复现：同输入两次判定完全一致
    f = build_facts(base)
    d1, d2 = decide(f, table.rules, table.version), decide(f, table.rules, table.version)
    if (d1.conclusion, d1.hits, d1.details) == (d2.conclusion, d2.hits, d2.details):
        print("[PASS] B1 纯函数可复现（同输入同结论）")
    else:
        failures.append("B1: 同输入两次判定结果不一致")
        print("[FAIL] B1 纯函数可复现")

    # B2 数据不足 → 结论必须保守为转人工
    #    注意：这里只断言"结论"而非"是否走兜底"——数据不足既可能被具体规则（R6/R8）捕获，
    #    也可能因无任何规则命中而落入引擎兜底；两者皆为合法实现，不应把实现细节写进断言。
    no_data = {"request": base["request"]}
    d3 = decide(build_facts(no_data), table.rules, table.version)
    if d3.conclusion == "to_human":
        print(f"[PASS] B2 数据不足 → 转人工（命中={list(d3.hits)}）")
    else:
        failures.append(f"B2: 数据不足未转人工（实得 {d3.conclusion}，hits={list(d3.hits)}）")
        print(f"[FAIL] B2 数据不足 → 转人工（实得 {d3.conclusion}）")

    # B2b 规则表为空 → 必须走引擎兜底（证明"未命中即转人工"这层保护真实存在）
    d3b = decide(build_facts(base), [], table.version)
    if d3b.conclusion == "to_human" and d3b.fallback_used:
        print("[PASS] B2b 空规则表 → 引擎兜底转人工")
    else:
        failures.append(
            f"B2b: 空规则表未走兜底（实得 {d3b.conclusion}，fallback={d3b.fallback_used}）"
        )
        print("[FAIL] B2b 空规则表 → 引擎兜底转人工")

    # B3 金额闸门：超过 AUTO_AMOUNT_CAP 必转人工（阈值来自 .env 注入）
    from app.config import settings  # noqa: PLC0415
    over = make_state(claim_amount=settings.auto_amount_cap + 1, reason=RefundReason.NO_REASON,
                      logistics=LogisticsStatus.SIGNED, credit=800, refund_rate=0.0)
    d4 = decide(build_facts(over), table.rules, table.version)
    if d4.conclusion == "to_human" and "R1" in d4.hits:
        print(f"[PASS] B3 超自动上限（>{settings.auto_amount_cap}）→ R1 转人工")
    else:
        failures.append(f"B3: 超上限未命中 R1（实得 {d4.conclusion}，hits={list(d4.hits)}）")
        print(f"[FAIL] B3 超自动上限 → R1（实得 {d4.conclusion}）")

    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
