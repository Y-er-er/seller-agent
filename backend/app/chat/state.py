"""会话状态机与护栏判定 —— **纯逻辑、无 IO**（可完全离线单测）。

分两块：
1. **状态机**：7 个状态 + 合法迁移表 + 三条硬性迁移守卫；
2. **护栏判定**：轮数 / 字数 / 成本 / 工具次数 / 追问次数的裁决函数与提示语。

设计原则：**判定与提示语都在这里**，调用方（`session.py` / API 层）只负责取数、落库与返回，
避免"同一条件在多处判断、文案各处不一致"。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

# ============================================================
# 状态
# ============================================================

CREATED = "CREATED"
CLARIFYING = "CLARIFYING"
COLLECTING = "COLLECTING"
SUBMITTED = "SUBMITTED"
DECIDED = "DECIDED"
ESCALATED = "ESCALATED"
CLOSED = "CLOSED"

ALL_STATUSES = (CREATED, CLARIFYING, COLLECTING, SUBMITTED, DECIDED, ESCALATED, CLOSED)

# 终态：不再接受买家消息，也不发生自动迁移
TERMINAL_STATUSES = (ESCALATED, CLOSED)

# 合法迁移表（未列出的组合一律拒绝）
TRANSITIONS: dict[str, tuple[str, ...]] = {
    CREATED: (CLARIFYING, ESCALATED, CLOSED),
    CLARIFYING: (COLLECTING, SUBMITTED, ESCALATED, CLOSED),
    COLLECTING: (SUBMITTED, ESCALATED, CLOSED),
    SUBMITTED: (DECIDED, ESCALATED, CLOSED),
    DECIDED: (CLOSED,),
    ESCALATED: (CLOSED,),
    CLOSED: (),
}

# ============================================================
# 升级人工的触发源（每个都必须写明原因并落 guardrail_event）
# ============================================================

ESCALATE_ROUND_LIMIT = "round_limit"        # 轮数耗尽
ESCALATE_MSG_TOO_LONG = "msg_too_long"      # 字数二次超长
ESCALATE_COST_PAUSED = "cost_paused"        # 成本闸门暂停
ESCALATE_RULE_TO_HUMAN = "rule_to_human"    # 判定返回 to_human
ESCALATE_TOOL_FAILED = "tool_failed"        # 工具连续失败
ESCALATE_BUYER_REQUEST = "buyer_request"    # 买家主动要求
ESCALATE_SESSION_TIMEOUT = "session_timeout"  # 会话静默超时

ESCALATE_REASONS = (
    ESCALATE_ROUND_LIMIT,
    ESCALATE_MSG_TOO_LONG,
    ESCALATE_COST_PAUSED,
    ESCALATE_RULE_TO_HUMAN,
    ESCALATE_TOOL_FAILED,
    ESCALATE_BUYER_REQUEST,
    ESCALATE_SESSION_TIMEOUT,
)

# ============================================================
# 面向买家的提示语（统一在此处，禁止散落在调用方）
# ============================================================

MSG_TOO_LONG_FIRST = (
    "内容较长（{count} 字），请精简到 {limit} 字内重发，这样我能更快帮你处理。（本次不计入次数）"
)
MSG_TOO_LONG_REPEAT = (
    "消息仍然超出 {limit} 字，为避免误判，已为你转接人工客服，请等待接入。"
)
MSG_ROUND_LIMIT = "本次咨询已达上限，已为你转接人工客服，请等待接入。"
MSG_COST_PAUSED = "系统当前暂停接待，已为你转接人工客服，请等待接入。"
MSG_SESSION_CLOSED = "本次会话已结束，如需继续请重新发起咨询。"


# ============================================================
# 状态机
# ============================================================


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: str, target: str) -> bool:
    """`current → target` 是否为合法迁移。"""
    return target in TRANSITIONS.get(current, ())


def transition_error(current: str, target: str) -> str | None:
    """不合法时返回可读原因，合法返回 None。"""
    if current not in TRANSITIONS:
        return f"未知状态：{current}"
    if target not in ALL_STATUSES:
        return f"未知目标状态：{target}"
    if is_terminal(current):
        return f"{current} 为终态，不能再迁移到 {target}"
    if not can_transition(current, target):
        return f"不允许的迁移：{current} → {target}"
    return None


@dataclass
class GuardVerdict:
    """三条硬性迁移守卫的裁决结果。"""

    allowed: bool
    code: str = ""            # "" / closed / cost_paused / round_limit
    message: str = ""
    escalate_reason: str = "" # 需要转人工时给出原因码（否则空）


def check_can_accept_message(*, status: str, round_used: int, gate_paused: bool) -> GuardVerdict:
    """买家要发消息时，是否允许继续对话（迁移守卫）。

    三条硬条件（任一不满足即不放行）：
    1. 会话未处于终态；
    2. 成本闸门未暂停；
    3. 已用轮次 < 上限。
    """
    if is_terminal(status):
        return GuardVerdict(False, "closed", MSG_SESSION_CLOSED, "")

    if gate_paused:
        return GuardVerdict(False, "cost_paused", MSG_COST_PAUSED, ESCALATE_COST_PAUSED)

    if round_used >= settings.max_rounds:
        return GuardVerdict(False, "round_limit", MSG_ROUND_LIMIT, ESCALATE_ROUND_LIMIT)

    return GuardVerdict(True)


# ============================================================
# 护栏判定
# ============================================================


@dataclass
class LengthVerdict:
    """单条消息长度裁决。

    **不许一票否决**：首次超长只提示精简（且**不计轮次**），再犯才转人工。
    """

    ok: bool
    code: str = ""          # "" / msg_too_long_first / msg_too_long_repeat
    escalate: bool = False
    message: str = ""


def evaluate_message_length(text: str, *, already_warned: bool) -> LengthVerdict:
    """`already_warned` 表示本会话此前是否已因超长提示过一次。"""
    limit = settings.max_msg_chars
    count = len(text or "")
    if count <= limit:
        return LengthVerdict(True)

    if not already_warned:
        return LengthVerdict(
            ok=False,
            code="msg_too_long_first",
            escalate=False,
            message=MSG_TOO_LONG_FIRST.format(count=count, limit=limit),
        )
    return LengthVerdict(
        ok=False,
        code="msg_too_long_repeat",
        escalate=True,
        message=MSG_TOO_LONG_REPEAT.format(limit=limit),
    )


def tool_limit_exceeded(calls_this_round: int) -> bool:
    """单轮内工具调用是否超限（防模型空转）。"""
    return calls_this_round > settings.max_tools_per_round


def slot_ask_exceeded(ask_count: int) -> bool:
    """同一槽位追问次数是否超限（仍拿不到即转人工）。"""
    return ask_count > settings.max_slot_ask
