"""结论解释 —— 把判定结论渲染成面向买家的说明。

**这是"呈现层"，不是"决策层"**（见规划书 §9.4 三层归属）：
- 结论本身由规则引擎给出，**本模块只做措辞渲染**，不产生也不修改结论；
- 因此用**确定性模板**实现——同样的结论永远得到同样的解释，且不受模型影响。

分工：
- `render_decision()` —— 判定结论 → 面向买家的解释（含逐条依据）；
- `render_ask()`      —— 必填槽位缺失 → 追问话术；
- `render_escalate()` —— 转人工话术。
"""

from __future__ import annotations

from typing import Any

#: 结论码 → 面向买家的定性说明（**不含金额与处置细节**，那部分由依据逐条列出）
#: ⚠️ 措辞**不绑定"仅退款"**：`auto_pass` 在不同诉求方向下会落地为退款或补发，
#:    结论页不能替买家预设处置方式（那是处置层的事，见 rules/disposal.py）。
CONCLUSION_LABELS: dict[str, str] = {
    "auto_pass": "经核实，你的申请符合处理条件，我们会按流程为你处理。",
    "auto_reject": "经核实，本次申请暂不符合处理条件。",
    "request_evidence": "目前材料还不足以判断，需要你补充举证后再继续。",
    "to_human": "这笔申请需要人工客服进一步核实，已为你转接。",
}

#: 槽位键 → 中文名（追问话术用）。键名与 `slots.SLOT_*` 一致（术语三分后的新名）。
SLOT_LABELS: dict[str, str] = {
    "issue_reason": "遇到了什么问题（未收到货 / 质量问题 / 错发漏发 / 无理由）",
    "issue_object": "问题出在哪里（商品本身 / 包装 / 物流）",
    "claim_direction": "你希望怎么处理（仅退款 / 补发 / 换货）",
}

_FALLBACK = "这笔申请的处理结果如下。"


def render_decision(summary: dict[str, Any]) -> str:
    """把判定结论渲染为买家可读的解释。

    `summary` 来自判定图（`conclusion` / `hit_rules` / `hit_details`）。
    依据部分**逐条列出**，便于买家核对——不隐藏、不美化。
    """
    conclusion = str(summary.get("conclusion") or "")
    label = CONCLUSION_LABELS.get(conclusion, _FALLBACK)

    lines = [label]
    # ⚠️ `submit_for_decision` 落库的判定字典用键 `details`（非 `hit_details`）；
    # 此处两者都读，避免"有依据却只回显规则代号"（管理员查看沟通过程时尤其重要）。
    details = list(summary.get("hit_details") or summary.get("details") or [])
    if details:
        lines.append("判断依据：")
        for idx, detail in enumerate(details[:8], start=1):
            lines.append(f"{idx}. {detail}")
    else:
        rules = list(summary.get("hit_rules") or [])
        if rules:
            lines.append("涉及规则：" + "、".join(str(r) for r in rules[:8]))

    if summary.get("needs_human"):
        lines.append("稍后会有客服与你联系，请保持手机畅通。")
    return "\n".join(lines)


def render_ask(missing: list[str]) -> str:
    """必填槽位缺失时的追问话术。"""
    if not missing:
        return ""
    items = [SLOT_LABELS.get(key, key) for key in missing]
    if len(items) == 1:
        return f"为了帮你处理，我还需要确认一下：{items[0]}？"
    joined = "；".join(items)
    return f"为了帮你处理，还需要确认几件事：{joined}？"


def render_escalate(reason_code: str = "") -> str:
    """转人工话术。"""
    return "已为你转接人工客服，请稍等，客服会尽快接入处理。"


def render_unavailable() -> str:
    """对话入口不可用时的兜底话术（模型未启用 / 闸门关闭）。"""
    return "在线助手当前暂不可用，已为你转接人工客服，请稍等。"


def render_exhausted() -> str:
    """单轮工具预算耗尽、且模型未给出收尾文本时的兜底话术。"""
    return "抱歉，这次没能一次把问题处理完，已为你转接人工客服，请稍等。"


def render_human_takeover() -> str:
    """管理员已接管时的买家侧提示。

    注意：**这不是会话消息**（不落库），而是给界面用的即时提示——
    会话此刻归人工，智能体若再写一条进去，会与人说的话打架。
    """
    return "已由人工客服接管，你的消息已收到，请稍候。"
