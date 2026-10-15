"""演示假订单预置 —— 本地 mock 模式下批量注入取数事实。

⚠️ **边界声明（与 `dev_seed.py` 同口径）**：
- 本模块**不是业务能力**，只在**本地 mock 模式**（未配置真实业务 API）下提供数据，
  用途是让「买家 H5 扮演买家 + 卖家智能体自演演练」有可查询的订单 / 物流 / 画像事实；
- 已配置真实业务 API（`ORDER_API_BASE` / `LOGISTICS_API_BASE`）时**跳过注入**，
  避免演示数据混入真实链路；
- 生产部署可删除本模块、`config/demo_orders.yaml`，并移除 `main.py` lifespan 中的调用。

数据来源：`config/demo_orders.yaml`（可自行编辑添加订单，重启服务即生效）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.tools import seed_logistics, seed_order, seed_profile

logger = logging.getLogger(__name__)

# 项目根目录下的演示假订单配置（相对本文件向上三级：app/demo_seed.py → backend → 项目根）
_DEMO_ORDERS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "demo_orders.yaml"
)


def _real_api_configured() -> bool:
    """是否已接入真实业务 API —— 此时禁止注入演示数据。"""
    return bool(settings.order_api_base or settings.logistics_api_base)


def load_demo_orders() -> int:
    """批量注入 `demo_orders.yaml` 中的假订单，返回注入条数。

    任何读取/解析失败都**降级为 0 并告警**（不抛异常），绝不影响服务启动。
    """
    if _real_api_configured():
        logger.info("已配置真实业务 API，跳过演示假订单注入")
        return 0
    if not _DEMO_ORDERS_FILE.exists():
        logger.warning("演示假订单文件不存在：%s", _DEMO_ORDERS_FILE)
        return 0
    try:
        data = yaml.safe_load(_DEMO_ORDERS_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 —— 文件损坏不阻断启动
        logger.warning("读取演示假订单失败（已忽略）：%s", exc)
        return 0

    orders = list(data.get("demo_orders") or [])
    for spec in orders:
        order_id = str(spec.get("order_id") or "").strip()
        if not order_id:
            continue
        logistics_status = spec.get("logistics_status") or "signed"
        seed_order(
            order_id,
            item_category=str(spec.get("item_category") or ""),
            pay_amount=float(spec.get("pay_amount") or 0.0),
            logistics_status=logistics_status,
        )
        seed_logistics(
            order_id,
            logistics_status=logistics_status,
            traces=[str(t) for t in (spec.get("traces") or [])],
        )
        profile: dict[str, Any] = spec.get("profile") or {}
        seed_profile(
            order_id,
            buyer_id=str(profile.get("buyer_id") or ""),
            buyer_credit_score=int(profile.get("buyer_credit_score") or 600),
            buyer_refund_rate_90d=float(profile.get("buyer_refund_rate_90d") or 0.0),
            refund_count_90d=int(profile.get("refund_count_90d") or 0),
        )

    logger.info("已注入 %d 条演示假订单", len(orders))
    return len(orders)
