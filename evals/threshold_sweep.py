"""阈值敏感性分析（决策辅助工具）。

用途：规则表草案里有一批**待定稿的阈值**（小额线、信用门槛、退款率线、金额上限…）。
      本工具用一组"探测用例"在**不同阈值取值**下重跑规则引擎，输出结论变化矩阵，
      从而回答："把 SMALL_AMOUNT 从 50 调到 100，哪些场景的结论会变？"

为什么需要它：阈值定稿是业务决策，但**不该靠猜**。跑一遍就能看出每个阈值在哪个区间
             "结论稳定"，哪些取值会真的改变判定结果。

运行（无需服务与数据库，纯本地）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe evals/threshold_sweep.py
    ... evals/threshold_sweep.py --param SMALL_AMOUNT --values 30,50,80,100

边界：本工具**只读**——它临时改的是内存中的 `settings` 副本并立即恢复，
      不会写回 `.env`，也不会改动 `config/refund_rules.yaml`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT, settings  # noqa: E402
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

# 结论缩写，便于在矩阵里对齐显示
ABBR = {
    "auto_pass": "过",
    "auto_reject": "驳",
    "request_evidence": "补",
    "to_human": "人",
}


# ============================================================
# 探测用例：每个用例只探测它该探测的阈值，其余事实固定
# ============================================================
def _state(
    *,
    amount: float,
    reason: RefundReason,
    logistics: LogisticsStatus,
    credit: int = 600,
    refund_rate: float = 0.05,
    refund_count: int = 1,
    image_count: int = 2,
) -> dict:
    return {
        "request": RefundRequest(
            order_id="SWEEP-0001", pay_amount=amount, claim_amount=amount,
            refund_reason_code=reason,
        ),
        "order": OrderSnapshot(order_id="SWEEP-0001", pay_amount=amount, logistics_status=logistics),
        "logistics": LogisticsInfo(order_id="SWEEP-0001", logistics_status=logistics),
        "profile": BuyerProfile(
            buyer_id="B001", buyer_credit_score=credit,
            buyer_refund_rate_90d=refund_rate, refund_count_90d=refund_count,
        ),
        "evidence": EvidenceReport(image_count=image_count, decodable=True),
    }


# 每个探测用例：名称 + 关注哪个阈值 + 状态构造
#
# ⚠️ 用例设计要点（否则扫描"扫不出东西"）：
# 探测某条**门槛型规则**（如 R5 高频退款）时，用例的基线结论必须是 `auto_pass`，
# 这样"命中 R5"才会让结论**翻转**为 to_human 而被观察到。
# 若用例基线本就是 to_human（例如兜底），那么"命中 R5"与"兜底"结论相同 —— 看不出差别。
PROBES: list[dict] = [
    {
        "name": "小额未签收 ¥39",
        "watch": ["SMALL_AMOUNT"],
        "state": lambda: _state(amount=39, reason=RefundReason.NOT_RECEIVED,
                                logistics=LogisticsStatus.NOT_SIGNED),
    },
    {
        "name": "小额未签收 ¥80",
        "watch": ["SMALL_AMOUNT"],
        "state": lambda: _state(amount=80, reason=RefundReason.NOT_RECEIVED,
                                logistics=LogisticsStatus.NOT_SIGNED),
    },
    {
        # 探测金额闸门：¥300 已超小额线，故基线为兜底转人工；
        # CAP 调低后 R1 会命中 —— 结论同为 to_human，但**命中规则不同**
        # （R1 是"资金风险闸门"，兜底是"无规则匹配"），审计含义完全不同，
        # 因此切换判据必须包含命中规则，而不只是结论。
        "name": "中额未签收 ¥300",
        "watch": ["AUTO_AMOUNT_CAP"],
        "state": lambda: _state(amount=300, reason=RefundReason.NOT_RECEIVED,
                                logistics=LogisticsStatus.NOT_SIGNED),
    },
    {
        "name": "高信用小额 ¥40",
        "watch": ["SMALL_AMOUNT", "MIN_CREDIT_SCORE", "MAX_REFUND_RATE"],
        "state": lambda: _state(amount=40, reason=RefundReason.NO_REASON,
                                logistics=LogisticsStatus.IN_TRANSIT,
                                credit=700, refund_rate=0.05),
    },
    {
        "name": "低信用小额 ¥40",
        "watch": ["SMALL_AMOUNT", "MIN_CREDIT_SCORE"],
        "state": lambda: _state(amount=40, reason=RefundReason.NO_REASON,
                                logistics=LogisticsStatus.IN_TRANSIT,
                                credit=600, refund_rate=0.05),
    },
    {
        # 退款率 0.12 落在"可自动通过"区间内 → 基线 auto_pass；
        # HIGH_REFUND_RATE 调到 ≤0.12 时 R5 会把它推向 to_human。
        "name": "退款率 0.12（边缘）",
        "watch": ["HIGH_REFUND_RATE", "MAX_REFUND_RATE"],
        "state": lambda: _state(amount=40, reason=RefundReason.NO_REASON,
                                logistics=LogisticsStatus.IN_TRANSIT,
                                credit=700, refund_rate=0.12),
    },
    {
        "name": "退款率 0.20（无人区）",
        "watch": ["HIGH_REFUND_RATE", "MAX_REFUND_RATE"],
        "state": lambda: _state(amount=40, reason=RefundReason.NO_REASON,
                                logistics=LogisticsStatus.IN_TRANSIT,
                                credit=700, refund_rate=0.20),
    },
    {
        # 5 笔、其余条件满足 R4 → 基线 auto_pass；
        # HIGH_REFUND_COUNT 调 ≤5 时 R5 命中 → 翻转为 to_human。
        "name": "90天 5 笔（边缘）",
        "watch": ["HIGH_REFUND_COUNT"],
        "state": lambda: _state(amount=40, reason=RefundReason.NO_REASON,
                                logistics=LogisticsStatus.IN_TRANSIT,
                                credit=700, refund_rate=0.05, refund_count=5),
    },
    {
        "name": "已签收称未收到",
        "watch": [],  # 对照用例：不应受任何阈值影响
        "state": lambda: _state(amount=199, reason=RefundReason.NOT_RECEIVED,
                                logistics=LogisticsStatus.SIGNED),
    },
    {
        "name": "质量主张（红线）",
        "watch": [],  # 对照用例：恒为 to_human
        "state": lambda: _state(amount=300, reason=RefundReason.QUALITY,
                                logistics=LogisticsStatus.SIGNED),
    },
]

# 可扫描参数：环境变量名 → (settings 属性, 中文名, 候选值, 当前值来源说明)
PARAMS: dict[str, dict] = {
    "SMALL_AMOUNT": {
        "attr": "small_amount",
        "label": "小额线（R2 / R4 共用）",
        "values": [10.0, 30.0, 50.0, 80.0, 100.0, 200.0],
    },
    "AUTO_AMOUNT_CAP": {
        "attr": "auto_amount_cap",
        "label": "自动处置金额上限（R1）",
        "values": [100.0, 200.0, 500.0, 1000.0, 3000.0],
    },
    "MIN_CREDIT_SCORE": {
        "attr": "min_credit_score",
        "label": "最低信用分（R4）",
        "values": [500, 600, 650, 700, 800],
    },
    "MAX_REFUND_RATE": {
        "attr": "max_refund_rate",
        "label": "自动通过的退款率上限（R4）",
        "values": [0.05, 0.10, 0.15, 0.20, 0.30],
    },
    "HIGH_REFUND_RATE": {
        "attr": "high_refund_rate",
        "label": "高风险退款率门槛（R5）",
        "values": [0.10, 0.20, 0.30, 0.50],
    },
    "HIGH_REFUND_COUNT": {
        "attr": "high_refund_count",
        "label": "高频退款笔数门槛（R5）",
        "values": [3, 5, 6, 10, 20],
    },
}


def verdict_for(state: dict, table) -> tuple[str, list[str]]:
    """跑一次判定，返回 (结论, 命中规则)。"""
    decision = decide(build_facts(state), table.rules, table.version)
    return decision.conclusion, list(decision.hits)


def sweep(param_name: str) -> None:
    """对单个参数做扫描，打印结论矩阵。"""
    spec = PARAMS[param_name]
    attr = spec["attr"]
    current = getattr(settings, attr)
    original = current

    probes = [p for p in PROBES if param_name in p["watch"]]
    if not probes:
        print(f"\n【{param_name}】{spec['label']} —— 无关联的探测用例，跳过")
        return

    values = list(spec["values"])
    if current not in values:
        values.append(current)
        values.sort()

    print(f"\n【{param_name}】{spec['label']}")
    print(f"  当前取值：{current}   候选：{values}")
    print()

    # 收集矩阵：probe × value
    matrix: list[tuple[str, list[tuple[object, str, list[str]]]]] = []
    for probe in probes:
        row = []
        for value in values:
            setattr(settings, attr, value)
            table = load_rules(RULE_FILE)  # 重新加载以重新注入 ${VAR}
            conclusion, hits = verdict_for(probe["state"](), table)
            row.append((value, conclusion, hits))
        matrix.append((probe["name"], row))
    setattr(settings, attr, original)  # 恢复，确保只读语义

    # 表头
    header = "  " + "用例".ljust(20) + "".join(str(v).rjust(8) for v in values)
    print(header)
    print("  " + "-" * (len(header) - 2))

    for name, row in matrix:
        cells = ""
        for _, conclusion, _hits in row:
            cells += ABBR.get(conclusion, "?").rjust(8)
        # 判据是「结论 + 命中规则」的组合，而非仅结论：
        # 例如 R1 命中与兜底都是 to_human，但业务含义完全不同，必须能被区分出来。
        signatures = [(c, tuple(h)) for _, c, h in row]
        flag = "  ← 随阈值变化" if len(set(signatures)) > 1 else ""
        print("  " + name.ljust(20) + cells + flag)

    print()
    print(f"  图例：过={ABBR['auto_pass']}  补={ABBR['request_evidence']}  "
          f"人={ABBR['to_human']}  驳={ABBR['auto_reject']}（兜底=无规则命中）")

    # 关键：报出**结论或命中规则**发生变化的拐点，这才是定稿的决策依据
    for name, row in matrix:
        signatures = [(c, tuple(h)) for _, c, h in row]
        if len(set(signatures)) == 1:
            continue
        print(f"  · 「{name}」在以下区间发生变化：")
        for i in range(1, len(row)):
            prev_v, prev_c, prev_h = row[i - 1]
            cur_v, cur_c, cur_h = row[i]
            if (prev_c, tuple(prev_h)) == (cur_c, tuple(cur_h)):
                continue
            prev_desc = f"{ABBR.get(prev_c, prev_c)}" + (f"{prev_h}" if prev_h else "（兜底）")
            cur_desc = f"{ABBR.get(cur_c, cur_c)}" + (f"{cur_h}" if cur_h else "（兜底）")
            print(f"      {prev_v} → {cur_v}：{prev_desc} → {cur_desc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="规则阈值敏感性分析（只读，不改任何文件）")
    parser.add_argument("--param", help="只扫描指定参数，如 SMALL_AMOUNT")
    parser.add_argument("--values", help="自定义候选值，逗号分隔，如 30,50,80")
    args = parser.parse_args()

    print("=" * 72)
    print("规则阈值敏感性分析")
    print(f"  规则表：{RULE_FILE}")
    print(f"  说明：本工具只读。临时改动仅作用于内存中的 settings，结束后自动恢复。")
    print("=" * 72)

    if args.param and args.param not in PARAMS:
        print(f"未知参数：{args.param}（可用：{sorted(PARAMS)}）")
        return 1

    targets = [args.param] if args.param else list(PARAMS)
    if args.values:
        if not args.param:
            print("--values 需与 --param 同时使用")
            return 1
        spec = PARAMS[args.param]
        # 保持原类型（float / int）
        cast = type(spec["values"][0])
        spec["values"] = [cast(v) for v in args.values.split(",")]

    for name in targets:
        sweep(name)

    print()
    print("=" * 72)
    print("提示：定稿时只需修改 backend/.env 中对应变量（规则表以 ${VAR} 引用），")
    print("      无需改动 config/refund_rules.yaml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
