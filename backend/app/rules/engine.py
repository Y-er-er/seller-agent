"""确定性规则引擎 —— 本方案的"骨"。

设计红线（对齐方案 §4 / §7）：
1. **纯函数**：同 (facts, rules) 必得同 Decision。不读时间、不读随机数、不调网络、不调模型。
2. **不暴露为大模型工具**：由 judge 节点直接 import 调用；模型无权决定"是否执行规则"。
3. **冲突消解固定优先级**：转人工 > 要求补证 > 自动通过。
4. **全量求值（不短路）**：按 priority 升序遍历**全部**启用规则，命中项全部记录，
   最终取结论优先级最高者。**不因命中 to_human 而中断遍历**——理由是
   质量主张（R7）、金额闸门（R1）等属于必须留痕的**业务红线**，短路会导致审计漏记。
   （此处**有意偏离**《开发计划书》§14 的短路伪代码，经用户 2026-10-13 确认。）
5. **可复现**：每条结论附带"命中规则 + 命中字段 + 运算符 + 阈值 + 实际值"，可逐项回溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 结论取值（与方案 §3 的结论枚举一致）。
# 说明：'flag'（风险标记）不在此处表达，而是由独立的 risk 节点产出，避免规则结论与风险标签耦合。
CONCLUSIONS: tuple[str, ...] = ("auto_pass", "auto_reject", "request_evidence", "to_human")

# 结论优先级：数值越大越优先。命中即向上覆盖。
# 顺序（自低到高）：自动通过 < 要求补证 < 拒绝 < 转人工。
# 「拒绝」高于「要求补证」：超时效等"理由不成立"情形应直接驳回，而非再让买家补证。
CONCLUSION_PRECEDENCE: dict[str, int] = {
    "auto_pass": 0,
    "request_evidence": 1,
    "auto_reject": 2,
    "to_human": 3,
}

# 未命中任何规则时的兜底结论：数据不足一律转人工（不猜测）
DEFAULT_CONCLUSION = "to_human"


class _Missing:
    """字段缺失的哨兵值（区别于 None：字段存在但值为 None）。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return "<MISSING>"


MISSING = _Missing()


# --------------------------------------------------------------------------
# 值提取与运算符
# --------------------------------------------------------------------------
def resolve_field(facts: dict[str, Any], path: str) -> Any:
    """按点号路径从 facts 取值；任一层缺失返回 MISSING。"""
    current: Any = facts
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _cmp(actual: Any, expected: Any, func: Callable[[Any, Any], bool]) -> bool:
    """安全比较：缺失值或类型不匹配一律判定为 False（绝不让规则"意外命中"）。"""
    if actual is MISSING or actual is None:
        return False
    try:
        return bool(func(actual, expected))
    except TypeError:
        return False


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: _cmp(a, b, lambda x, y: x == y),
    "ne": lambda a, b: _cmp(a, b, lambda x, y: x != y),
    "gt": lambda a, b: _cmp(a, b, lambda x, y: x > y),
    "gte": lambda a, b: _cmp(a, b, lambda x, y: x >= y),
    "lt": lambda a, b: _cmp(a, b, lambda x, y: x < y),
    "lte": lambda a, b: _cmp(a, b, lambda x, y: x <= y),
    "in": lambda a, b: _cmp(a, b, lambda x, y: x in y),
    "not_in": lambda a, b: _cmp(a, b, lambda x, y: x not in y),
    "contains": lambda a, b: _cmp(a, b, lambda x, y: y in x),
    # 存在性判断：字段已解析且非 None 即视为存在
    "exists": lambda a, b: a is not MISSING and a is not None,
    "not_exists": lambda a, b: a is MISSING or a is None,
    "is_true": lambda a, b: a is True,
    "is_false": lambda a, b: a is False,
}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    """一条规则。条件以 JSON 结构表达，便于从 YAML/SQL 加载与版本化。"""

    rule_id: str
    priority: int
    conclusion: str
    condition: dict[str, Any]
    description: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class LeafHit:
    """一个叶子条件的命中明细（用于审计回溯）。"""

    field: str
    op: str
    threshold: Any
    actual: Any


@dataclass(frozen=True)
class RuleHit:
    """一条规则的命中记录。"""

    rule_id: str
    priority: int
    conclusion: str
    description: str
    leaves: tuple[LeafHit, ...]


@dataclass(frozen=True)
class Decision:
    """判定结果。冻结以保证不可被下游意外修改。"""

    conclusion: str
    hits: tuple[str, ...]
    details: tuple[RuleHit, ...]
    rule_version: str = ""
    facts_echo: dict[str, Any] = field(default_factory=dict)

    @property
    def fallback_used(self) -> bool:
        """是否走了"未命中任何规则"的兜底路径。"""
        return len(self.hits) == 0


# --------------------------------------------------------------------------
# 条件求值
# --------------------------------------------------------------------------
def evaluate(condition: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, list[LeafHit]]:
    """递归求值条件树，返回 (是否命中, 命中叶子明细)。

    支持的组合键：all / any / not；叶子形如 {"field": ..., "op": ..., "value": ...}。
    """
    # 组合：all（全部满足）
    if "all" in condition:
        leaves: list[LeafHit] = []
        for sub in condition["all"]:
            ok, sub_leaves = evaluate(sub, facts)
            if not ok:
                return False, []
            leaves.extend(sub_leaves)
        return True, leaves

    # 组合：any（任一满足）
    if "any" in condition:
        for sub in condition["any"]:
            ok, sub_leaves = evaluate(sub, facts)
            if ok:
                return True, sub_leaves
        return False, []

    # 组合：not（取反；命中明细不透出，避免误导审计）
    if "not" in condition:
        ok, _ = evaluate(condition["not"], facts)
        return (not ok), []

    # 叶子条件
    field_path = condition.get("field")
    op_name = condition.get("op")
    if not field_path or not op_name:
        # 条件书写非法时按"未命中"处理，绝不静默放行
        return False, []

    op_func = OPERATORS.get(op_name)
    if op_func is None:
        return False, []

    actual = resolve_field(facts, field_path)
    expected = condition.get("value")
    matched = bool(op_func(actual, expected))
    if not matched:
        return False, []

    leaf = LeafHit(
        field=field_path,
        op=op_name,
        threshold=expected,
        actual=None if actual is MISSING else actual,
    )
    return True, [leaf]


# --------------------------------------------------------------------------
# 判定主流程
# --------------------------------------------------------------------------
def decide(facts: dict[str, Any], rules: list[Rule], rule_version: str = "") -> Decision:
    """执行规则判定。

    遍历顺序：按 priority 升序（数值越小越优先），仅取 enabled 规则。
    遍历策略：**全量求值**——所有启用规则都会被评估，命中项全部进入 hits / details，
              以保证审计完整（例如同时记录"金额超限 R1"与"质量主张 R7"）。
    冲突消解：遍历结束后，取结论优先级最高者（转人工 > 要求补证 > 自动处置）。
    """
    hits: list[RuleHit] = []
    winner: str | None = None

    for rule in sorted((r for r in rules if r.enabled), key=lambda r: r.priority):
        matched, leaves = evaluate(rule.condition, facts)
        if not matched:
            continue

        hits.append(
            RuleHit(
                rule_id=rule.rule_id,
                priority=rule.priority,
                conclusion=rule.conclusion,
                description=rule.description,
                leaves=tuple(leaves),
            )
        )

        # 结论优先级向上覆盖
        if winner is None or CONCLUSION_PRECEDENCE[rule.conclusion] > CONCLUSION_PRECEDENCE[winner]:
            winner = rule.conclusion

        # 注意：此处**不做短路**。即便已命中 to_human，仍需继续遍历，
        # 以便把 R7（质量红线）等其余命中规则一并留痕，保证审计完整。

    return Decision(
        conclusion=winner or DEFAULT_CONCLUSION,
        hits=tuple(h.rule_id for h in hits),
        details=tuple(hits),
        rule_version=rule_version,
        facts_echo=dict(facts),
    )
