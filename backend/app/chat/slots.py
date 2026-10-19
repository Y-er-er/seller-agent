"""槽位定义与装配 —— 模型抽取的「推断」，与服务端**权威数据严格分开**。

**术语三分**（此前 `claim_type` 一个名字装两种含义，是"补发不可达"的根因）：

| 槽位 | 含义 | 取值 | 去向 |
| - | - | - | - |
| `issue_reason` | **为什么** | 未收到货 / 质量问题 / 错发漏发 / 无理由 / 其他 | → `RefundReason`（参与判定） |
| `issue_object` | **问题在哪** | 商品本身 / 包装 / 物流 / 其他 | 仅留痕 |
| `claim_direction` | **想要什么** | 仅退款 / 补发 / 换货 / 其他 | → `ClaimType`，**决定处置方向** |

红线（P1 实施设计 §八）：
1. 槽位**只用于"填表"**，资格判定仍由规则引擎做；缺必填槽位**不得猜测**；
2. 槽位**不进**规则事实字典（`rules.facts.FACT_FIELDS`）—— 物理上杜绝"推断参与判定"；
3. 每个槽位**必须带来源轮次**（`source_seq`），卷宗要能点回买家原话；
4. 买家陈述与权威数据冲突 → **以权威数据为准**，并记录 `conflict` 标记交规则/人工。

本模块是**纯逻辑**（无 IO）：给定订单快照、物流信息与模型给出的槽位，产出一份
`RefundRequest` 与冲突清单。这样装配规则可完全离线单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import (
    ClaimType,
    LogisticsInfo,
    LogisticsStatus,
    OrderSnapshot,
    RefundReason,
    RefundRequest,
)

# ============================================================
# 槽位键（8 个）
# ============================================================

SLOT_ISSUE_REASON = "issue_reason"      # 问题原因（必填）—— "为什么"
SLOT_ISSUE_OBJECT = "issue_object"      # 问题对象（必填）—— "问题在哪"
SLOT_CLAIM_DIRECTION = "claim_direction"  # 诉求方向（必填）—— "想要什么"，决定处置方向
SLOT_SYMPTOM = "symptom"                # 问题现象
SLOT_AFFECTED_QTY = "affected_qty"      # 影响数量
SLOT_SIGNED_DAYS = "signed_days"        # 签收后时长（可由订单推导，非模型必给）
SLOT_EVIDENCE = "evidence"              # 已提交材料（引用摘要）
SLOT_NOTE = "note"                      # 补充说明

# 必填槽位：缺任一项 → **继续追问**，不得猜测（受"同槽位追问 ≤2 次"约束，见 state.slot_ask_exceeded）
REQUIRED_SLOTS: tuple[str, ...] = (SLOT_ISSUE_REASON, SLOT_ISSUE_OBJECT, SLOT_CLAIM_DIRECTION)

# ============================================================
# 枚举取值（模型只能在这些值里选；越界视为无效，由装配层丢弃）
# ============================================================

ISSUE_REASONS: tuple[str, ...] = ("未收到货", "质量问题", "错发漏发", "无理由", "其他")
ISSUE_OBJECTS: tuple[str, ...] = ("商品本身", "包装", "物流", "其他")
CLAIM_DIRECTIONS: tuple[str, ...] = ("仅退款", "补发", "换货", "其他")

SLOT_CHOICES: dict[str, tuple[str, ...]] = {
    SLOT_ISSUE_REASON: ISSUE_REASONS,
    SLOT_ISSUE_OBJECT: ISSUE_OBJECTS,
    SLOT_CLAIM_DIRECTION: CLAIM_DIRECTIONS,
}

# 槽位值长度上限（防模型塞长文本进槽位，污染审计与卷宗）
MAX_SLOT_VALUE_CHARS = 60

# ============================================================
# 两张映射：**只做翻译，不改变任何判定**
# ============================================================

#: `issue_reason`（为什么）→ 判定图原因码。业务红线（如"质量问题 → 转人工"）由规则引擎决定，不在这里。
_ISSUE_REASON_TO_CODE: dict[str, RefundReason] = {
    "未收到货": RefundReason.NOT_RECEIVED,
    "质量问题": RefundReason.QUALITY,
    "错发漏发": RefundReason.WRONG_ITEM,
    "无理由": RefundReason.NO_REASON,
    "其他": RefundReason.OTHER,
}

#: `claim_direction`（想要什么）→ `ClaimType`（处置**方向**）。
#:
#: ⚠️ 这里的取值必须与 `rules/disposal.py::_PASS_BY_CLAIM_TYPE` 的键**同一值域**
#: （`ClaimType` 的英文值），否则映射会静默落空 —— 这正是此前"补发永不命中"的成因之一。
#: 「其他」= 诉求未明确，按最常见的"退款"处理，属**显式回落**（有注释、可审计），非静默猜测。
_DIRECTION_TO_CLAIM_TYPE: dict[str, ClaimType] = {
    "仅退款": ClaimType.REFUND,
    "补发": ClaimType.RESHIP,
    "换货": ClaimType.EXCHANGE,
    "其他": ClaimType.REFUND,
}


def direction_to_claim_type(value: object) -> ClaimType:
    """把槽位里的「诉求方向」翻译为 `ClaimType`。未知/缺失 → `REFUND`（显式回落）。"""
    return _DIRECTION_TO_CLAIM_TYPE.get(str(value or "").strip(), ClaimType.REFUND)


# ============================================================
# 校验与装配
# ============================================================


@dataclass
class AssemblyResult:
    """装配结果。`ok=False` 时 `missing` 说明还差哪些必填槽位。"""

    ok: bool
    missing: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    request: RefundRequest | None = None
    cleaned: dict[str, str] = field(default_factory=dict)


def _clean_value(key: str, value: object) -> str | None:
    """清洗单个槽位取值：去空白、截断、枚举校验。无效返回 None（视为未提供）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text[:MAX_SLOT_VALUE_CHARS]
    choices = SLOT_CHOICES.get(key)
    if choices is not None and text not in choices:
        # 枚举越界 → 丢弃（不猜测、不模糊匹配）
        return None
    return text


def clean_slots(raw: object) -> dict[str, str]:
    """把模型给的槽位对象清洗为「合法且已提供」的字典。

    只保留本模块认识的键；枚举越界的值直接丢弃（宁可判为"缺失"去追问，
    也不接受一个含义不明的值进入卷宗）。
    """
    if not isinstance(raw, dict):
        return {}
    known = {
        SLOT_ISSUE_REASON, SLOT_ISSUE_OBJECT, SLOT_CLAIM_DIRECTION,
        SLOT_SYMPTOM, SLOT_AFFECTED_QTY, SLOT_SIGNED_DAYS, SLOT_EVIDENCE, SLOT_NOTE,
    }
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in known:
            continue
        cleaned = _clean_value(key, value)
        if cleaned is not None:
            out[key] = cleaned
    return out


def missing_required(slots: dict[str, str]) -> list[str]:
    """返回缺失的必填槽位列表（空列表 = 齐备，可以装配提交）。"""
    return [key for key in REQUIRED_SLOTS if not slots.get(key)]


def detect_conflicts(
    slots: dict[str, str], order: OrderSnapshot | None, logistics: LogisticsInfo | None
) -> list[dict[str, str]]:
    """检测"买家陈述 vs 权威数据"的冲突。

    **以权威数据为准**（不由模型调和），冲突记录进审计与卷宗，交规则/人工。
    """
    conflicts: list[dict[str, str]] = []
    claim = slots.get(SLOT_ISSUE_REASON)

    signed = False
    if logistics is not None and logistics.signed_at is not None:
        signed = True
    elif order is not None and order.signed_at is not None:
        signed = True
    elif logistics is not None and logistics.logistics_status == LogisticsStatus.SIGNED:
        signed = True

    if claim == "未收到货" and signed:
        conflicts.append({
            "slot": SLOT_ISSUE_REASON,
            "buyer_says": "未收到货",
            "authoritative": "物流显示已签收",
            "resolve": "以物流为准",
        })
    if claim == "错发漏发" and logistics is not None and logistics.logistics_status == LogisticsStatus.NOT_SHIPPED:
        conflicts.append({
            "slot": SLOT_ISSUE_REASON,
            "buyer_says": "错发漏发",
            "authoritative": "订单尚未发货",
            "resolve": "以订单为准",
        })
    return conflicts


def assemble_request(
    *,
    order_id: str,
    slots: dict[str, str],
    order: OrderSnapshot | None,
    logistics: LogisticsInfo | None,
    claim_direction: str | None = None,
    claim_images: list[str] | None = None,
    buyer_note: str = "",
    requested_amount: float | None = None,
) -> AssemblyResult:
    """把槽位装配成一份 `RefundRequest`。

    权威字段以**取数层**为准（`pay_amount` / `item_category` 取订单快照）；
    槽位只提供"原因 / 方向"类字段。必填缺失时返回 `ok=False`，**不猜测、不提交**。

    `claim_direction`：**决定处置方向**（退款 / 补发 / 换货）。
    不传时从 `slots` 里取同名字段；两者都没有 → 落 `REFUND`（`direction_to_claim_type`
    的显式回落，且必填校验在调用方已先行拦截）。

    `requested_amount`：买家主张的退款金额（来自买家消息，非模型推断）。
    **仅退款场景默认退全额**；主张金额若超过实付则**收敛为实付**（不能退超过付的钱）。
    """
    order_ok = order is not None
    pay_amount = float(order.pay_amount) if order_ok else 0.0
    category = order.item_category if order_ok else ""

    if claim_direction is None:
        claim_direction = slots.get(SLOT_CLAIM_DIRECTION)

    # 金额：以实付为上限；未主张则默认全额
    if requested_amount is None:
        claim_amount = pay_amount
    else:
        try:
            claim_amount = max(0.0, float(requested_amount))
        except (TypeError, ValueError):
            claim_amount = pay_amount
        claim_amount = min(claim_amount, pay_amount)

    conflicts = detect_conflicts(slots, order, logistics)
    missing = missing_required(slots)
    if not order_ok or missing:
        return AssemblyResult(ok=False, missing=missing, conflicts=conflicts, cleaned=slots)

    reason = _ISSUE_REASON_TO_CODE.get(slots.get(SLOT_ISSUE_REASON, ""), RefundReason.OTHER)
    claim_type = direction_to_claim_type(claim_direction)

    # buyer_note 汇总：把"现象 / 补充说明"这类自由文本并入留言（仅供模型理解，不参与判定）
    note_parts = [part for part in (slots.get(SLOT_SYMPTOM), slots.get(SLOT_NOTE), buyer_note) if part]
    note = "；".join(note_parts)[:500]

    request = RefundRequest(
        order_id=order_id,
        item_category=category,
        pay_amount=pay_amount,
        claim_amount=claim_amount,
        claim_type=claim_type,
        refund_reason_code=reason,
        claim_images=list(claim_images or []),
        buyer_note=note,
    )
    return AssemblyResult(ok=True, missing=[], conflicts=conflicts, request=request, cleaned=slots)
