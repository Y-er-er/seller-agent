"""规则引擎包。

对外仅暴露三样东西：规则表加载、判定函数、判定结果结构。
调用方（judge 节点）应只依赖这些，不感知 YAML 细节。
"""

from app.rules.engine import (
    CONCLUSIONS,
    CONCLUSION_PRECEDENCE,
    DEFAULT_CONCLUSION,
    Decision,
    LeafHit,
    Rule,
    RuleHit,
    decide,
    evaluate,
)
from app.rules.loader import RuleTable, load_rules

__all__ = [
    "CONCLUSIONS",
    "CONCLUSION_PRECEDENCE",
    "DEFAULT_CONCLUSION",
    "Decision",
    "LeafHit",
    "Rule",
    "RuleHit",
    "RuleTable",
    "decide",
    "evaluate",
    "load_rules",
]
