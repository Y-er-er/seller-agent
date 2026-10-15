"""LangGraph 图的状态定义。

说明：本模块**不导入 langgraph**——只用标准库的 TypedDict + Annotated reducer，
     因此规则引擎等下游模块可在依赖未安装时独立测试。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.models import (
    ActionRecord,
    AuditEvent,
    BuyerProfile,
    EvidenceReport,
    LogisticsInfo,
    OrderSnapshot,
    RefundRequest,
    RiskReport,
)


class RefundState(TypedDict, total=False):
    """整图共享状态。

    约定：
    - 普通字段由单节点写入（后写覆盖）。
    - 追加型字段使用 `operator.add` reducer，支持并行/多节点累加而不覆盖。
    """

    # ---------- 入参 ----------
    request: RefundRequest

    # ---------- 事实（三个并行取数节点各写一个字段，互不冲突）----------
    order: OrderSnapshot | None
    logistics: LogisticsInfo | None
    profile: BuyerProfile | None

    # ---------- 核验 ----------
    evidence: EvidenceReport | None

    # ---------- 判定 ----------
    hits: Annotated[list[str], operator.add]          # 命中规则 ID
    hit_details: Annotated[list[dict[str, Any]], operator.add]  # 命中明细（字段/运算符/阈值/实际值）
    conclusion: str | None                            # auto_pass / auto_reject / request_evidence / to_human
    rule_version: str | None                          # 规则版本号（结论可回溯）

    # ---------- 风险与处置 ----------
    risk: RiskReport | None
    action: ActionRecord | None                       # 执行了什么 / 影子模式下"本应执行什么"

    # ---------- 审计与计量（追加型）----------
    audit_events: Annotated[list[AuditEvent], operator.add]
    llm_usage: Annotated[list[dict[str, Any]], operator.add]   # 每次模型调用的 token 计量

    # ---------- 链路标识 ----------
    trace_id: str                                     # 等于 request_id（受理号），亦即 LangGraph thread_id

    # ---------- 控制流辅助 ----------
    intake_error: str | None                          # 受理校验失败原因；非空则由路由短路到 audit
    review_decision: str | None                       # 人工复核决定：maintain / override
    reviewer: str | None                              # 复核人标识
