"""规则表加载器。

职责：
1. 把 YAML 规则表解析为 `Rule` 对象列表；
2. 解析其中的 `${ENV_VAR}` 占位符（阈值优先走 `.env`，纪律 9）；
3. **加载期严格校验**——规则写错就立刻失败，绝不静默放过。

为什么必须严格校验：规则条件里的字段名/运算符一旦写错，该条件会**永远为 False**
且不产生任何报错，判定结果会静默偏离预期，属于最难排查的一类缺陷。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.rules.engine import CONCLUSIONS, OPERATORS, Rule
from app.rules.facts import FACT_FIELDS

# 形如 ${AUTO_AMOUNT_CAP} 的整串占位符
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# 无需 value 的运算符（存在性/布尔判断）
_NO_VALUE_OPERATORS = frozenset({"exists", "not_exists", "is_true", "is_false"})


@dataclass(frozen=True)
class RuleTable:
    """一张版本化的规则表。"""

    version: str
    rules: tuple[Rule, ...]

    @property
    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]


def _coerce(text: str) -> Any:
    """把占位符解析出的字符串转成合适的 Python 类型（int/float/bool/str）。"""
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _resolve_placeholders(node: Any) -> Any:
    """递归解析条件树中的 `${VAR}` 占位符，取值来源为 settings（即 .env）。"""
    if isinstance(node, dict):
        return {k: _resolve_placeholders(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_placeholders(v) for v in node]
    if isinstance(node, str):
        m = _PLACEHOLDER_RE.match(node)
        if m:
            name = m.group(1)
            if not hasattr(settings, name.lower()):
                # 统一抛 ValueError：加载期所有校验失败都是同一种异常类型，
                # 调用方只需 except ValueError 即可（避免 KeyError / ValueError 混杂）。
                raise ValueError(f"规则表引用了未定义的配置项：{name}（请检查 .env）")
            return getattr(settings, name.lower())
    return node


def _validate_condition(node: Any, rule_id: str, path: str = "condition") -> None:
    """递归校验条件树结构。非法即抛 ValueError，并给出可定位的路径。"""
    if not isinstance(node, dict):
        raise ValueError(f"[{rule_id}] {path} 必须是对象，实得 {type(node).__name__}")

    combinators = [k for k in ("all", "any", "not") if k in node]
    if combinators:
        if len(combinators) > 1:
            raise ValueError(f"[{rule_id}] {path} 同时出现多个组合键：{combinators}（只允许一个）")
        key = combinators[0]
        value = node[key]
        if key == "not":
            _validate_condition(value, rule_id, f"{path}.not")
        else:
            if not isinstance(value, list) or not value:
                raise ValueError(f"[{rule_id}] {path}.{key} 必须是非空列表")
            for index, sub in enumerate(value):
                _validate_condition(sub, rule_id, f"{path}.{key}[{index}]")
        return

    # ---- 叶子条件 ----
    field = node.get("field")
    op = node.get("op")
    if not field or not op:
        raise ValueError(f"[{rule_id}] {path} 叶子条件必须同时包含 field 与 op")
    if op not in OPERATORS:
        raise ValueError(f"[{rule_id}] {path} 未知运算符 {op!r}；可用：{sorted(OPERATORS)}")
    if field not in FACT_FIELDS:
        raise ValueError(
            f"[{rule_id}] {path} 引用了未知事实字段 {field!r}；可用：{sorted(FACT_FIELDS)}"
        )
    if op not in _NO_VALUE_OPERATORS and "value" not in node:
        raise ValueError(f"[{rule_id}] {path} 运算符 {op!r} 必须提供 value")


def load_rules(path: str | Path) -> RuleTable:
    """从 YAML 文件加载规则表，并做加载期严格校验。

    YAML 结构：
        version: "2026.10-draft1"
        rules:
          - rule_id: R1
            priority: 10
            enabled: true
            description: "..."
            conclusion: to_human
            condition: {all: [{field: claim_amount, op: gt, value: "${AUTO_AMOUNT_CAP}"}]}
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"规则表不存在：{path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    version = str(raw.get("version", "unknown"))

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    seen_priorities: dict[int, str] = {}

    for item in raw.get("rules", []):
        if not isinstance(item, dict):
            raise ValueError(f"规则项必须是对象，实得 {type(item).__name__}")

        rule_id = str(item.get("rule_id", "")).strip()
        if not rule_id:
            raise ValueError("存在缺少 rule_id 的规则")
        if rule_id in seen_ids:
            raise ValueError(f"规则 ID 重复：{rule_id}")
        seen_ids.add(rule_id)

        conclusion = str(item.get("conclusion", ""))
        if conclusion not in CONCLUSIONS:
            raise ValueError(
                f"[{rule_id}] 非法结论 {conclusion!r}；可用：{list(CONCLUSIONS)}"
            )

        priority = int(item["priority"])
        if priority in seen_priorities:
            raise ValueError(
                f"[{rule_id}] 优先级 {priority} 与 {seen_priorities[priority]} 重复；"
                "优先级必须唯一，否则求值顺序不确定"
            )
        seen_priorities[priority] = rule_id

        condition = _resolve_placeholders(item.get("condition", {}))
        _validate_condition(condition, rule_id)

        rules.append(
            Rule(
                rule_id=rule_id,
                priority=priority,
                conclusion=conclusion,
                condition=condition,
                description=str(item.get("description", "")),
                enabled=bool(item.get("enabled", True)),
            )
        )

    return RuleTable(version=version, rules=tuple(rules))
