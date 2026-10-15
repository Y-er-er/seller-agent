"""口径问答用到的中文标签与数值格式化。

为什么单独一层：规则表的字段名与运算符是**给机器看**的（`claim_amount` / `gte`），
直接呈现给客服或买家等于没解释。本模块负责把它们翻译成业务语言。

⚠️ 这些映射必须与 `config/refund_rules.yaml` 的实际取值保持同步；
   新增事实字段或运算符时，请同步在此登记（否则会回落到原始英文，仍可读但不友好）。
"""

from __future__ import annotations

from typing import Any

# ---------- 事实字段 → 业务名称 ----------
FIELD_LABELS: dict[str, str] = {
    "order_id": "订单号",
    "item_category": "商品类目",
    "pay_amount": "实付金额",
    "claim_amount": "申请退款金额",
    "refund_reason_code": "退款原因",
    "logistics_status": "物流状态",
    "signed_at": "签收时间",
    "buyer_credit_score": "买家信用分",
    "buyer_refund_rate_90d": "90天退款率",
    "refund_count_90d": "90天退款笔数",
    "image_count": "有效举证图片数",
    "decodable": "图片可解码",
    "has_quality_claim": "存在质量主张信号",
    "has_order": "已取到订单数据",
    "has_logistics": "已取到物流数据",
    "has_profile": "已取到买家画像",
}

# ---------- 运算符 → 数学符号 / 业务用语 ----------
OP_LABELS: dict[str, str] = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "in": "属于",
    "not_in": "不属于",
    "is_true": "为真",
    "is_false": "为假",
}

# ---------- 枚举值 → 业务名称 ----------
REASON_LABELS: dict[str, str] = {
    "not_received": "未收到货",
    "quality": "质量问题",
    "wrong_item": "错发/漏发",
    "no_reason": "无理由",
    "other": "其他",
}

LOGISTICS_LABELS: dict[str, str] = {
    "not_shipped": "未发货",
    "in_transit": "运输中",
    "not_signed": "已到达未签收",
    "signed": "已签收",
}

CONCLUSION_LABELS: dict[str, str] = {
    "auto_pass": "自动通过",
    "auto_reject": "自动驳回",
    "request_evidence": "要求补证",
    "to_human": "转人工复核",
}

# 阈值展示需带百分号的字段（业务习惯看百分比，而不是 0.2）
_PERCENT_FIELDS = {"buyer_refund_rate_90d"}
# 阈值展示需带货币符号的字段
_MONEY_FIELDS = {"claim_amount", "pay_amount"}
# 属于枚举语义的字段（阈值与实际值都要翻译）
_ENUM_FIELDS = {"refund_reason_code": REASON_LABELS, "logistics_status": LOGISTICS_LABELS}
# 布尔字段（不展示"阈值"，只展示是否成立）
_BOOL_FIELDS = {"decodable", "has_quality_claim", "has_order", "has_logistics", "has_profile"}


def label_field(field: str) -> str:
    """字段名 → 业务名称（未知字段原样返回，便于发现遗漏）。"""
    return FIELD_LABELS.get(field, field)


def label_op(op: str) -> str:
    """运算符 → 数学符号（未知运算符原样返回）。"""
    return OP_LABELS.get(op, op)


def format_value(field: str, value: Any) -> str:
    """把阈值/实际值格式化为业务可读文本。"""
    if value is None:
        return "无"
    if field in _ENUM_FIELDS:
        return _ENUM_FIELDS[field].get(str(value), str(value))
    if isinstance(value, bool) or field in _BOOL_FIELDS:
        return "是" if value else "否"
    if field in _PERCENT_FIELDS:
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(value)
    if field in _MONEY_FIELDS:
        try:
            return f"¥{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float):
        # 金额以外的浮点（如退款率已在上面处理）统一去掉多余小数位
        return f"{value:g}"
    return str(value)


def describe_leaf(leaf: dict[str, Any]) -> str:
    """把一条叶子条件描述成一句业务话术。

    等值比较（`=` / `属于`）时阈值与实际值必然相同，故**不再重复展示实际值**——
    否则会出现"退款原因 = 质量问题（实际 质量问题）"这类冗余表述。
    """
    field = str(leaf.get("field", ""))
    op = str(leaf.get("op", ""))
    name = label_field(field)

    if field in _BOOL_FIELDS:
        # 布尔字段：只说明"成立/不成立"
        return f"{name}{label_op(op)}"

    threshold = format_value(field, leaf.get("threshold"))
    if op in {"eq", "ne", "in", "not_in"}:
        return f"{name} {label_op(op)} {threshold}"

    actual = format_value(field, leaf.get("actual"))
    return f"{name} {label_op(op)} {threshold}（实际 {actual}）"


def describe_conclusion(conclusion: str | None) -> str:
    """结论码 → 业务名称。"""
    if not conclusion:
        return "未判定"
    return CONCLUSION_LABELS.get(conclusion, conclusion)
