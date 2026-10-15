"""配置一致性校验（启动期自检）。

**为什么需要**：本项目的若干配置项之间存在**语义依赖**，单独看每一项都合法，组合起来却会
让业务规则静默失效。例如：

- `SMALL_AMOUNT >= AUTO_AMOUNT_CAP`：R1（金额闸门，`gte`）会先于 R2/R4 命中，
  小额线形同虚设 —— 两条直通规则**永远不会生效**；
- `MAX_REFUND_RATE > HIGH_REFUND_RATE`：退款率出现"无人区"（既不能自动通过、
  也不被标记高风险，全部落兜底转人工）；
- 关闭影子模式却未开启处置执行：每笔资金动作都会被拒绝并转人工（安全但会积压）。

这些都属于"配置合法但语义失效"，靠人眼看 `.env` 很难发现。故在进程启动时统一自检，
把问题**提前暴露在日志里**，而不是等业务发现判定异常。

设计取向：**只告警、不阻断启动**。配置问题不应导致服务起不来（不同环境需要灵活调整），
但必须让运维一眼看到风险。
"""

from __future__ import annotations

from app.config import settings


def validate_settings() -> list[str]:
    """返回配置告警列表（无告警则返回空列表）。"""
    warnings: list[str] = []

    # ---- 阈值语义：小额线必须严格小于金额闸门，否则 R2/R4 永远不生效 ----
    if settings.small_amount >= settings.auto_amount_cap:
        warnings.append(
            f"SMALL_AMOUNT({settings.small_amount}) >= AUTO_AMOUNT_CAP({settings.auto_amount_cap})"
            "：R1 金额闸门会先命中，导致 R2/R4 两条自动通过规则**永远不生效**"
        )

    # ---- 退款率衔接：MAX 不得超过 HIGH，否则出现无人区 ----
    if settings.max_refund_rate > settings.high_refund_rate:
        warnings.append(
            f"MAX_REFUND_RATE({settings.max_refund_rate}) > HIGH_REFUND_RATE({settings.high_refund_rate})"
            "：退款率落在两者之间时既不能自动通过、也不被标记高风险，会全部落兜底转人工（无人区）"
        )

    # ---- 处置执行双闸门 ----
    if not settings.shadow_mode and not settings.disposal_enabled:
        warnings.append(
            "SHADOW_MODE=false 但 DISPOSAL_ENABLED=false：所有资金动作都会被拒绝并转人工（安全，但会积压）"
        )
    if settings.disposal_enabled and not settings.disposal_api_base:
        warnings.append(
            "DISPOSAL_ENABLED=true 但 DISPOSAL_API_BASE 为空：资金动作将调用失败并转人工"
        )
    if settings.shadow_mode and settings.disposal_enabled:
        # 影子模式优先，此时 disposal_enabled 不生效——不是错误，但值得提示以免误解
        warnings.append(
            "SHADOW_MODE=true 时 DISPOSAL_ENABLED 不生效：仍为影子模式（只记录不执行）"
        )

    # ---- 审计落库 ----
    if settings.audit_db_enabled and not settings.mysql_uri:
        warnings.append(
            "AUDIT_DB_ENABLED=true 但 MYSQL_URI 为空：审计将降级为仅内存事件，数据库无留痕"
        )

    # ---- 模型计费闸门 ----
    if settings.llm_enabled and not settings.dashscope_api_key:
        warnings.append("LLM_ENABLED=true 但未配置 DASHSCOPE_API_KEY：模型调用会失败")

    # ---- 对话入口（P1）----
    if settings.chat_enabled and not settings.llm_enabled:
        warnings.append(
            "CHAT_ENABLED=true 但 LLM_ENABLED=false：对话入口依赖模型，启动后将无法回复"
        )
    if settings.chat_enabled and not settings.price_configured:
        warnings.append(
            "CHAT_ENABLED=true 但未配置模型单价（PRICE_INPUT_PER_1K / PRICE_OUTPUT_PER_1K）："
            "成本闸门的**金额口径不可用**，将降级为按 token 兜底判定"
        )
    if settings.cost_monthly_cap_cny > 0 and settings.cost_warning_cny >= settings.cost_monthly_cap_cny:
        warnings.append(
            f"COST_WARNING_CNY({settings.cost_warning_cny}) >= "
            f"COST_MONTHLY_CAP_CNY({settings.cost_monthly_cap_cny})："
            "预警线不早于月度上限，**失去提前止损的意义**"
        )
    for name, value in (
        ("MAX_ROUNDS", settings.max_rounds),
        ("MAX_MSG_CHARS", settings.max_msg_chars),
        ("MAX_TOOLS_PER_ROUND", settings.max_tools_per_round),
        ("MAX_SLOT_ASK", settings.max_slot_ask),
    ):
        if value <= 0:
            warnings.append(f"{name}={value} 非法：护栏参数必须为正整数，否则会话一发起即被拦")

    return warnings


def log_warnings(logger) -> list[str]:
    """把校验结果写入日志，并返回告警列表（便于调用方复用）。"""
    warnings = validate_settings()
    if not warnings:
        logger.info("配置自检通过：未发现语义冲突")
        return warnings
    for item in warnings:
        logger.warning("配置自检告警：%s", item)
    return warnings
