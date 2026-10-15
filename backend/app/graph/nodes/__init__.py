"""图节点集合。

每个节点 = 一个"接收 state、返回局部更新"的普通函数（可为 async）。
组织方式对齐方案 §3 的 7 个 Agent + 1 个 HITL 节点。
"""

from app.graph.nodes.audit import audit_node
from app.graph.nodes.dispose import dispose_node
from app.graph.nodes.evidence import evidence_node
from app.graph.nodes.fetch import fetch_logistics_node, fetch_order_node, fetch_profile_node
from app.graph.nodes.human_review import human_review_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.judge import judge_node
from app.graph.nodes.risk import risk_node

__all__ = [
    "audit_node",
    "dispose_node",
    "evidence_node",
    "fetch_logistics_node",
    "fetch_order_node",
    "fetch_profile_node",
    "human_review_node",
    "intake_node",
    "judge_node",
    "risk_node",
]
