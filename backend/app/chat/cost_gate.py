"""成本闸门 —— **配置项**（不是设计前提）：按配置给出"何时该停"的兜底判定。

判定优先级（阈值全部来自 `.env`）：
1. **额度到期 / 耗尽** —— 免费额度失效或已用满，后续调用按量付费，直接暂停；
2. **预警线**（`COST_WARNING_CNY`）—— 达线即暂停新会话并汇报；
3. **月度硬上限**（`COST_MONTHLY_CAP_CNY`）—— 达线即停用对话入口。

两条边界（都容易被写成"静默失效"）：
- **金额口径只在单价已配置时参与判定**。单价属使用者**可选**提供的外部事实：未配置时
  一律按 **token 计量**，不去用"金额恒为 0"假装闸门在工作；这是正常状态，**不是配置缺陷**。
- **DB 抖动 ≠ 不计量**：记账失败时写入**进程内内存兜底**，预检时与 DB 累计相加，避免"库一抖就失控"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.config import settings
from app.db import chat_repo

logger = logging.getLogger(__name__)

# ---- 闸门状态码（健康端点与前端提示共用）----
GATE_ACTIVE = "active"
GATE_COST_WARNING = "cost_warning"
GATE_COST_CAP = "cost_cap"
GATE_QUOTA_EXHAUSTED = "quota_exhausted"
GATE_QUOTA_EXPIRED = "quota_expired"

# ---- 进程内兜底计数器（DB 不可达时仍能粗略止损）----
_fallback: dict[str, float] = {"amount": 0.0, "tokens": 0}

# ---- 已汇报过的原因（避免每轮都刷日志与事件表）----
_reported: set[str] = set()


@dataclass
class GateStatus:
    """闸门状态快照。可直接 JSON 序列化（健康端点返回它）。"""

    paused: bool                 # 是否已暂停**新会话**
    reason: str                  # active / cost_warning / cost_cap / quota_exhausted / quota_expired
    message: str                 # 面向用户的说明（可直接展示）
    month_amount: float          # 本月**等效消耗**金额（元）
    month_tokens: int            # 本月 token 数
    month_calls: int             # 本月模型调用次数
    total_tokens: int            # 本项目累计 token
    free_quota_tokens: int       # 免费额度
    price_configured: bool       # 是否已配置单价（未配置时金额口径不参与判定，按 token 计量）
    degraded: bool = False       # 是否因 DB 不可达而降级（用量可能被低估）
    quota_used: int = 0          # 已用额度 = 外部基线 + 本项目累计
    quota_remaining: int = 0     # 剩余额度（可为负，负数即已透支）
    quota_expire_date: str = ""  # 额度到期日（空 = 未配置）
    days_to_expire: int = -1     # 距到期天数（-1 = 未配置）

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": "paused" if self.paused else GATE_ACTIVE,
            "paused": self.paused,
            "reason": self.reason,
            "message": self.message,
            "month_amount": round(self.month_amount, 6),
            "month_tokens": self.month_tokens,
            "month_calls": self.month_calls,
            "total_tokens": self.total_tokens,
            "free_quota_tokens": self.free_quota_tokens,
            "quota_used": self.quota_used,
            "quota_remaining": self.quota_remaining,
            "quota_expire_date": self.quota_expire_date,
            "days_to_expire": self.days_to_expire,
            "price_configured": self.price_configured,
            "degraded": self.degraded,
            "warning_line": settings.cost_warning_cny,
            "monthly_cap": settings.cost_monthly_cap_cny,
        }


# ============================================================
# 计量与记账
# ============================================================


def compute_amount(input_tokens: int, output_tokens: int) -> float:
    """按官方单价折算**等效消耗**金额（元）。

    单价单位是「元 / 1K token」，故先除 1000。未配置单价（=0）时返回 0 —— 此时金额不可信，
    调用方应参考 `status.total_tokens` 的兜底判定。
    """
    return round(
        max(0, input_tokens) / 1000 * settings.price_input_per_1k
        + max(0, output_tokens) / 1000 * settings.price_output_per_1k,
        6,
    )


def month_start(now: datetime | None = None) -> datetime:
    """本月起点（用于月度累计）。"""
    ref = now or datetime.now()
    return ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def record_usage(
    input_tokens: int,
    output_tokens: int,
    *,
    case_id: str | None = None,
    seq: int | None = None,
    model: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """记账一次模型调用。

    `persist=False`（或用 `reset_fallback` 清理）便于**离线单测**不触碰数据库。
    落库失败时把用量记入进程内兜底，保证闸门不会因为库抖动而完全失效。
    """
    tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))
    amount = compute_amount(input_tokens, output_tokens)
    used_model = model or settings.llm_text_model

    ok = False
    if persist and settings.mysql_uri:
        try:
            ok = await chat_repo.insert_usage(
                case_id, seq, used_model, max(0, int(input_tokens)), max(0, int(output_tokens)), amount
            )
        except Exception as exc:  # noqa: BLE001 —— 记账失败不得中断对话
            logger.warning("用量落库异常（已降级为内存兜底）：%s: %s", type(exc).__name__, exc)
            ok = False

    if not ok:
        _fallback["amount"] += amount
        _fallback["tokens"] += tokens

    return {"tokens": tokens, "amount": amount, "persisted": ok}


def reset_fallback() -> None:
    """清空内存兜底与已汇报标记（仅供测试使用）。"""
    _fallback["amount"] = 0.0
    _fallback["tokens"] = 0
    _reported.clear()


# ============================================================
# 预检
# ============================================================


async def _load_usage() -> tuple[dict[str, Any], dict[str, Any], bool]:
    """读取本月与全量用量，并叠加内存兜底。返回 (本月, 全量, 是否降级)。"""
    degraded = False
    month = {"amount": 0.0, "tokens": 0, "calls": 0}
    total = {"amount": 0.0, "tokens": 0, "calls": 0}
    if settings.mysql_uri:
        try:
            month = await chat_repo.sum_usage(month_start())
            total = await chat_repo.sum_usage(None)
        except Exception as exc:  # noqa: BLE001 —— 读不到用量时降级，但不放弃闸门
            logger.warning("读取用量失败（降级为内存估算）：%s: %s", type(exc).__name__, exc)
            degraded = True

    month = {
        "amount": month["amount"] + _fallback["amount"],
        "tokens": month["tokens"] + int(_fallback["tokens"]),
        "calls": month["calls"],
    }
    total = {
        "amount": total["amount"] + _fallback["amount"],
        "tokens": total["tokens"] + int(_fallback["tokens"]),
        "calls": total["calls"],
    }
    return month, total, degraded


def quota_expire_date(today: date | None = None) -> tuple[date | None, int | None]:
    """解析额度到期日，返回 `(到期日, 距今天数)`。

    **未配置或格式非法时返回 `(None, None)`** —— 用 `None` 而不是 `-1`，是为了与
    "昨天到期"（天数 = -1）区分开；否则"未配置"会被误判成"已过期"（曾因此写出错误判定）。
    **不抛异常**（健康检查与预检都要能用）。
    """
    raw = (settings.free_quota_expire_date or "").strip()
    if not raw:
        return None, None
    try:
        expire = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("FREE_QUOTA_EXPIRE_DATE 格式非法（应为 YYYY-MM-DD）：%r", raw)
        return None, None
    return expire, (expire - (today or date.today())).days


def _decide(
    month_amount: float, used_tokens: int, days_to_expire: int | None
) -> tuple[bool, str, str]:
    """闸门判定。返回 (是否暂停, 状态码, 说明)。

    判定优先级：**额度到期 > 月度上限 > 预警线 > 额度耗尽**。
    到期排第一，是因为它意味着"额度已失效、后续调用直接按量付费"，性质最严重。
    """
    cap = settings.cost_monthly_cap_cny
    warn = settings.cost_warning_cny

    # 1) 额度到期（`None` = 未配置、不检查；<= 0 = 今天或已过期）
    if days_to_expire is not None and days_to_expire <= 0:
        return True, GATE_QUOTA_EXPIRED, (
            f"免费额度已于 {settings.free_quota_expire_date} 到期（官方额度失效），"
            "后续调用将按量付费，已暂停新会话，请确认后处理。"
        )

    # 2) 金额闸门（仅当单价已配置时才可靠）
    if settings.price_configured:
        if cap > 0 and month_amount >= cap:
            return True, GATE_COST_CAP, (
                f"本月等效消耗 ¥{month_amount:.4f} 已达月度上限 ¥{cap:g}，对话入口已停用，请转人工。"
            )
        if warn > 0 and month_amount >= warn:
            return True, GATE_COST_WARNING, (
                f"本月等效消耗 ¥{month_amount:.4f} 已达预警线 ¥{warn:g}，"
                "已暂停新会话（防止免费额度耗尽后自动扣费），请确认后处理。"
            )

    # 3) token 兜底：免费额度耗尽即进入现金计费，必须停
    quota = settings.free_quota_tokens
    if quota > 0 and used_tokens >= quota:
        return True, GATE_QUOTA_EXHAUSTED, (
            f"已用 token {used_tokens}（含外部基线 {settings.free_quota_used_baseline}）"
            f"已达免费额度 {quota}，后续调用将产生实际扣费，已暂停新会话，请确认后处理。"
        )

    return False, GATE_ACTIVE, ""


async def evaluate() -> GateStatus:
    """只读地评估当前闸门状态（不写事件、不汇报）。"""
    month, total, degraded = await _load_usage()
    expire, days_to_expire = quota_expire_date()

    # 真实已用量 = 官方控制台的外部基线 + 本项目累计
    quota_used = settings.free_quota_used_baseline + total["tokens"]
    quota_remaining = settings.free_quota_tokens - quota_used

    paused, reason, message = _decide(month["amount"], quota_used, days_to_expire)

    return GateStatus(
        paused=paused,
        reason=reason,
        message=message,
        month_amount=month["amount"],
        month_tokens=month["tokens"],
        month_calls=month["calls"],
        total_tokens=total["tokens"],
        free_quota_tokens=settings.free_quota_tokens,
        price_configured=settings.price_configured,
        degraded=degraded,
        quota_used=quota_used,
        quota_remaining=quota_remaining,
        quota_expire_date=(expire.isoformat() if expire else ""),
        days_to_expire=(days_to_expire if days_to_expire is not None else -1),
    )


async def _report_once(reason: str, message: str) -> None:
    """暂停**首次发生**时汇报一次（日志 + 护栏事件），避免每轮刷屏。"""
    if reason in _reported:
        return
    _reported.add(reason)
    logger.error("【成本闸门】%s —— %s", reason, message)
    if settings.mysql_uri:
        try:
            await chat_repo.record_guardrail(None, reason, message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("成本闸门事件写入失败（已降级）：%s: %s", type(exc).__name__, exc)


async def precheck(*, ongoing: bool = False) -> GateStatus:
    """调用前的闸门预检。

    语义（规划书 §6.6「暂停语义」）：
    - **新会话**（`ongoing=False`）：暂停即拒绝创建；
    - **进行中的会话**（`ongoing=True`）：**允许收尾**——能出结论就出结论，否则转人工；
      本函数仍返回 `paused=True`，由调用方决定如何收尾（不得静默中断）。
    """
    status = await evaluate()
    if status.paused:
        await _report_once(status.reason, status.message)
    return status
