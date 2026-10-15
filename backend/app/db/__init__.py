"""数据访问层（MySQL）。

- `audit_repo` —— 判定审计（`decision_audit`）；
- `chat_repo`  —— 对话层（会话 / 消息 / 槽位 / 护栏事件 / 模型用量）；
- `connection` —— 连接与线程池封装。

写路径一律**吞异常降级**，读路径一律**透出异常**（详见各模块 docstring）。
"""

from app.db.audit_repo import (
    get_decisions_by_request,
    get_stats,
    list_decisions,
    save_decision,
)
from app.db.chat_repo import (
    append_message,
    count_guardrail_since,
    create_case,
    get_case,
    insert_usage,
    list_cases_by_status,
    list_guardrails,
    list_messages,
    list_slots,
    record_guardrail,
    sum_usage,
    update_case,
    upsert_slot,
)
from app.db.connection import connect, parse_mysql_uri, run_sync

__all__ = [
    "connect",
    "parse_mysql_uri",
    "run_sync",
    # 判定审计
    "save_decision",
    "list_decisions",
    "get_decisions_by_request",
    "get_stats",
    # 对话层
    "create_case",
    "get_case",
    "update_case",
    "list_cases_by_status",
    "append_message",
    "list_messages",
    "upsert_slot",
    "list_slots",
    "record_guardrail",
    "list_guardrails",
    "count_guardrail_since",
    "insert_usage",
    "sum_usage",
]
