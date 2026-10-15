"""售后处置客户端（M2：**资金/状态变更动作**的唯一出口）。

⚠️ 与只读取数层（`order.py` / `logistics.py` / `profile.py`）的**根本区别**：

| | 取数层（只读） | 本模块（写） |
| - | - | - |
| 失败处理 | 返回 `None`，交由规则判"数据不足 → 转人工"（**安全**） | **绝不可当成功**；必须显式返回失败并升级人工 |
| 未配置时 | 降级为内存 mock（便于开发） | **拒绝执行**（绝不 mock 成功） |
| 幂等 | 无需 | **必须**：携带 `Idempotency-Key`，防网络重试导致重复退款 |
| 限流 | 与取数共用一个信号量 | **独立信号量**，避免资金动作与取数抢配额 |

设计原则：**宁可拒绝执行并转人工，也不产生"以为退了、其实没退"的假成功。**
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings

# 资金动作独立限流：并发上限远低于只读通道（外部资金系统通常配额更紧）
_MAX_CONCURRENCY = 3

# 视为"服务端已受理"的业务状态码
_ACCEPTED_STATUS = {"ok", "accepted", "success", "succeeded"}

# 处置结果状态（供调用方与审计使用）
STATUS_EXECUTED = "executed"              # 真实执行成功
STATUS_SHADOW = "shadow"                  # 影子模式：未执行（预期行为）
STATUS_NOT_CONFIGURED = "not_configured"  # 未获授权 / 未配置接口 → 拒绝执行
STATUS_FAILED = "failed"                  # 调用失败（网络 / 非 2xx / 业务拒绝）

_semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)


@dataclass
class DisposalResult:
    """处置执行结果。

    `executed` 是**唯一**表示"资金动作真的落地了"的字段，审计的 `executed` 列直接取它。
    其余状态一律 `executed=False`，并由调用方决定是否升级人工。
    """

    status: str
    action: str
    executed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def needs_human(self) -> bool:
        """是否需要升级人工：调用失败、或未获授权时**必须**有人跟进，不能静默结束。"""
        return self.status in (STATUS_FAILED, STATUS_NOT_CONFIGURED)


async def aexecute_action(
    *,
    action: str,
    request_id: str,
    order_id: str,
    amount: float,
) -> DisposalResult:
    """执行处置动作。

    闸门顺序（任一不满足即**拒绝执行**，不产生任何外部调用）：
        1. `disposal_enabled` 未开启 → not_configured
        2. `disposal_api_base` 未配置 → not_configured
        3. action 为 noop → 不调用（由调用方处理）

    调用失败（超时 / 连接失败 / 非 2xx / 业务拒绝）→ failed，由调用方升级人工。
    """
    # ---- 闸门 1/2：未获授权或未配置 → 明确拒绝，绝不 mock 成功 ----
    if not settings.disposal_enabled:
        return DisposalResult(
            status=STATUS_NOT_CONFIGURED,
            action=action,
            error="未开启处置执行（DISPOSAL_ENABLED=false）",
        )
    if not settings.disposal_api_base:
        return DisposalResult(
            status=STATUS_NOT_CONFIGURED,
            action=action,
            error="未配置处置执行接口（DISPOSAL_API_BASE 为空）",
        )

    url = f"{settings.disposal_api_base.rstrip('/')}/disposals"
    # 幂等键：受理号即天然的业务幂等标识（一笔申请只应产生一次资金动作）
    headers = {
        "Idempotency-Key": request_id,
        "Content-Type": "application/json",
    }
    body = {
        "action": action,
        "request_id": request_id,
        "order_id": order_id,
        "amount": amount,
    }

    async with _semaphore:
        try:
            async with httpx.AsyncClient(timeout=settings.disposal_timeout) as client:
                response = await client.post(url, json=body, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 — 任何异常都转为"失败"，交由调用方升级人工
            return DisposalResult(
                status=STATUS_FAILED,
                action=action,
                error=f"{type(exc).__name__}: {exc}",
            )

    # 业务层还需要确认受理状态：2xx 不等于"已受理"
    biz_status = str((payload or {}).get("status", "")).lower()
    if biz_status not in _ACCEPTED_STATUS:
        return DisposalResult(
            status=STATUS_FAILED,
            action=action,
            error=f"业务未受理：status={biz_status or '(空)'}",
            detail={"response": payload},
        )

    return DisposalResult(
        status=STATUS_EXECUTED,
        action=action,
        executed=True,
        detail={
            "disposal_id": (payload or {}).get("disposal_id"),
            "idempotency_key": request_id,
            "response_status": biz_status,
        },
    )
