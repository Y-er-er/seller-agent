"""节点 4：资格判定（judge）—— 本方案的"骨"。

红线（方案 §7）：规则引擎**不得**暴露为大模型工具。
本节点直接函数调用 `decide()`，不经过任何 tool-calling 通道，模型无从干预。
"""

from __future__ import annotations

from functools import lru_cache

from app.config import PROJECT_ROOT
from app.graph.state import RefundState
from app.models import AuditEvent
from app.rules import RuleTable, decide, load_rules
from app.rules.facts import build_facts

_RULE_FILE = PROJECT_ROOT / "config" / "refund_rules.yaml"


@lru_cache(maxsize=1)
def get_rule_table() -> RuleTable:
    """规则表在进程内只加载一次，避免每笔判定重复读盘（方案 §7）。"""
    return load_rules(_RULE_FILE)


def judge_node(state: RefundState) -> dict:
    """组装事实 → 执行规则 → 输出结论与命中明细。"""
    table = get_rule_table()
    facts = build_facts(state)
    decision = decide(facts, table.rules, table.version)

    # 命中明细（规则 + 字段 + 运算符 + 阈值 + 实际值），供审计逐项回溯
    details = [
        {
            "rule_id": hit.rule_id,
            "priority": hit.priority,
            "conclusion": hit.conclusion,
            "description": hit.description,
            "leaves": [
                {"field": leaf.field, "op": leaf.op, "threshold": leaf.threshold, "actual": leaf.actual}
                for leaf in hit.leaves
            ],
        }
        for hit in decision.details
    ]

    return {
        "hits": list(decision.hits),
        "hit_details": details,
        "conclusion": decision.conclusion,
        "rule_version": decision.rule_version,
        "audit_events": [
            AuditEvent(
                stage="judge",
                message=f"判定结论：{decision.conclusion}",
                payload={
                    "hits": list(decision.hits),
                    "rule_version": decision.rule_version,
                    "fallback_used": decision.fallback_used,
                },
            )
        ],
    }
