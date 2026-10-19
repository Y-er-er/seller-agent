"""口径问答（客服释疑）。

边界（方案 §12）：**不参与资格判定**，不写状态、不触发节点、不改变任何结论。
默认走确定性解释 + 关键词口径检索；仅当润色开关开启时才让模型参与措辞。
"""

from app.qa.answering import BOUNDARY, answer_question, explain_order
from app.qa.knowledge import get_knowledge_base, list_entries, search

__all__ = [
    "BOUNDARY",
    "answer_question",
    "explain_order",
    "get_knowledge_base",
    "list_entries",
    "search",
]
