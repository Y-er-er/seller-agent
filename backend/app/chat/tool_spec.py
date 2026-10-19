"""对话工具规格 —— 给模型看的「可调用能力清单」。

三条设计要点：

1. **不暴露 `case_id` 入参**：由执行层注入当前会话的 `case_id`，模型无法指定任意工单
   （防越权）。这是对 P1 实施设计 §七「入参含 case_id」的**有意收紧**——
   模型连"指定别的工单"这个动作都表达不出来，比事后校验更可靠。

2. **禁用清单是硬约束**：只暴露下面 6 个工具；退款 / 打款 / 改规则 / 改阈值 / 任意 SQL /
   任意 URL 抓取**一律不在其中**。执行层用 `is_allowed_tool()` 二次校验——
   即使模型幻觉出一个工具名，也执行不了（不依赖模型守规矩）。

3. 规格格式对齐 OpenAI function-calling，可直接喂给 `bind_tools`。
"""

from __future__ import annotations

from typing import Any

from app.chat.slots import CLAIM_DIRECTIONS, ISSUE_OBJECTS, ISSUE_REASONS

# ============================================================
# 工具名
# ============================================================

TOOL_QUERY_ORDER = "query_order"
TOOL_QUERY_LOGISTICS = "query_logistics"
TOOL_QUERY_CASE_PROGRESS = "query_case_progress"
TOOL_REGISTER_EVIDENCE = "register_evidence"
TOOL_SUBMIT_FOR_DECISION = "submit_for_decision"
TOOL_ESCALATE_TO_HUMAN = "escalate_to_human"

#: 允许执行的工具白名单（执行层必须用它校验，不得只看模型给的 name）
ALLOWED_TOOLS: frozenset[str] = frozenset({
    TOOL_QUERY_ORDER,
    TOOL_QUERY_LOGISTICS,
    TOOL_QUERY_CASE_PROGRESS,
    TOOL_REGISTER_EVIDENCE,
    TOOL_SUBMIT_FOR_DECISION,
    TOOL_ESCALATE_TO_HUMAN,
})

#: 明确**禁止**暴露的动作（写在此处便于评审核对；这些动作在本项目里要么不存在、要么必须人工）
DISABLED_ACTIONS: tuple[str, ...] = (
    "refund",            # 退款 / 打款（资金动作，只能由既有处置链路在双闸门下执行）
    "transfer_money",    # 打款
    "update_rule",       # 改规则
    "update_threshold",  # 改阈值
    "run_sql",           # 任意 SQL
    "fetch_url",         # 任意 URL 抓取
)

#: 转人工原因码（模型只能在这些里选；与 state.ESCALATE_* 的业务含义对齐）
ESCALATE_REASON_CODES: tuple[str, ...] = (
    "buyer_request",   # 买家主动要求
    "cannot_help",     # 智能体无法处理
    "dispute",         # 买家对结论有异议
)


def is_allowed_tool(name: str) -> bool:
    """执行层二次校验：模型给的 `name` 是否在白名单内。"""
    return name in ALLOWED_TOOLS


# ============================================================
# 规格（OpenAI function-calling 格式）
# ============================================================

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_QUERY_ORDER,
            "description": (
                "查询本工单对应订单的事实：实付金额、商品类目、物流状态、签收时间。"
                "只读；取不到时返回 available=false，**不得据此编造信息**。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_QUERY_LOGISTICS,
            "description": (
                "查询本工单的物流轨迹摘要与签收状态。只读；"
                "取不到时返回 available=false，**不得据此编造信息**。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_QUERY_CASE_PROGRESS,
            "description": "查询本工单当前进度：所处阶段、已登记材料、剩余时限。只读。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_REGISTER_EVIDENCE,
            "description": (
                "登记买家提供的举证材料（图片/凭证引用）。幂等：同一引用不会重复登记。"
                "登记结果只说明「收到了什么」，**不做任何真伪或质量判断**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "材料引用列表（图片 URL / COS key / 文件标识）",
                    },
                    "note": {"type": "string", "description": "对材料的简要说明（可选）"},
                },
                "required": ["evidence_refs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_SUBMIT_FOR_DECISION,
            "description": (
                "把本工单提交规则引擎判定，返回结论与逐条依据。"
                "调用前必须已收齐三个必填项：问题原因、问题对象、诉求方向；"
                "缺任一项**不要调用**，应先向买家追问。金额与物流以系统数据为准，无需你提供。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_reason": {
                        "type": "string",
                        "enum": list(ISSUE_REASONS),
                        "description": "问题原因（必填）：为什么申请售后",
                    },
                    "issue_object": {
                        "type": "string",
                        "enum": list(ISSUE_OBJECTS),
                        "description": "问题对象（必填）：问题出在哪个对象上",
                    },
                    "claim_direction": {
                        "type": "string",
                        "enum": list(CLAIM_DIRECTIONS),
                        "description": "诉求方向（必填）：买家希望怎么处理——它决定最终是退款、补发还是换货",
                    },
                    "symptom": {"type": "string", "description": "问题现象（可选，简述）"},
                    "affected_qty": {"type": "string", "description": "影响数量（可选，如「2 件」）"},
                    "note": {"type": "string", "description": "补充说明（可选）"},
                },
                "required": ["issue_reason", "issue_object", "claim_direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_ESCALATE_TO_HUMAN,
            "description": (
                "把本工单转人工客服。用于：买家主动要求、你无法处理、或买家对结论有异议。"
                "转人工后本次会话结束，请向买家明确告知已转人工。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason_code": {
                        "type": "string",
                        "enum": list(ESCALATE_REASON_CODES),
                        "description": "转人工原因码",
                    },
                    "note": {"type": "string", "description": "补充说明（可选）"},
                },
                "required": ["reason_code"],
            },
        },
    },
]
