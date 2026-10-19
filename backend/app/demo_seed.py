"""演示假订单预置 —— 本地 mock 模式下批量注入取数事实。

⚠️ **边界声明（与 `dev_seed.py` 同口径）**：
- 本模块**不是业务能力**，只在**本地 mock 模式**（未配置真实业务 API）下提供数据，
  用途是让「买家 H5 扮演买家 + 卖家智能体自演演练」有可查询的订单 / 物流 / 画像事实；
- 已配置真实业务 API（`ORDER_API_BASE` / `LOGISTICS_API_BASE`）时**跳过注入**，
  避免演示数据混入真实链路；
- 生产部署可删除本模块、`config/demo_orders.yaml`，并移除 `main.py` lifespan 中的调用。

数据来源：
1. `config/demo_orders.yaml`（批量预置，启动时注入，见 `load_demo_orders`）；
2. **按需合成**（见 `ensure_demo_order`）——买家界面开启「演示模式」后，任意订单号都能受理，
   事实由订单号**确定性**派生（同一订单号恒得同一份事实，保证可复现）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.dev_seed import real_api_configured
from app.tools import seed_logistics, seed_order, seed_profile
from app.tools.order import is_seeded, peek_order

logger = logging.getLogger(__name__)

# 项目根目录下的演示假订单配置（相对本文件向上三级：app/demo_seed.py → backend → 项目根）
_DEMO_ORDERS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "demo_orders.yaml"
)


def _real_api_configured() -> bool:
    """是否已接入真实业务 API —— 此时禁止注入演示数据。

    **单一真相**：判定口径委托给 `dev_seed.real_api_configured()`，不在此处重写一份，
    避免两处口径漂移导致"一处放行、一处拒绝"。
    """
    return real_api_configured()


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
            goods_no=str(spec.get("goods_no") or ""),
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


# ============================================================
# 按需合成夹具（买家界面「演示模式」）
# ============================================================
#
# 目的：让**任意订单号**都能走完售后链路，便于把智能体测透。
# 三个不可让步的边界：
#   1. **不伪装**：生成的数据一律带 `synthetic=True`，接口与界面都要如实标注"合成数据"；
#      这不是"编造数据"——工具层的红线是"取不到就说取不到"，此处是**显式声明的演示夹具**；
#   2. **不污染真实链路**：已接入真实业务 API 时**整体失效**（与 `load_demo_orders` 同口径）；
#   3. **不覆盖既有事实**：订单已存在则原样返回，不重写（幂等）。

#: 金额池：刻意跨 R1 金额闸门（¥300）与 R2/R4 小额线（¥50）三档，换个订单号即换一档
_DEMO_AMOUNTS: tuple[float, ...] = (19.9, 35.0, 48.0, 68.0, 129.0, 259.0, 328.0, 899.0)

#: 类目池
_DEMO_CATEGORIES: tuple[str, ...] = ("日用百货", "数码配件", "服饰鞋包", "家居日用", "食品饮料")

#: 商品编号池：合成夹具据此把订单挂到**演示商品**上 —— 这是「商品 × 售后联动」的演示桥。
#: ⚠️ 编号必须与库里实际存在的演示商品一致，否则售后统计会显示"无关联工单"（无害，但看不到效果）。
_DEMO_GOODS_NOS: tuple[str, ...] = ("G-0001", "G-0002", "G-0003")

#: 物流池：签收占多数（多数售后规则以"已签收"为前置），保留少量未签收/运输中用于边界验证
_DEMO_LOGISTICS: tuple[str, ...] = ("signed", "signed", "signed", "not_signed", "in_transit")

#: 签收后时长池：覆盖 R11 的时效边界（REFUND_WINDOW_DAYS=15）
_DEMO_SIGNED_DAYS: tuple[int, ...] = (1, 3, 7, 15, 30)

#: 信用分池：覆盖 R4 的 MIN_CREDIT_SCORE=650
_DEMO_CREDIT: tuple[int, ...] = (580, 620, 700, 780)

#: 90 天退款率池：覆盖 R4/R5 的 0.20 门槛
_DEMO_REFUND_RATE: tuple[float, ...] = (0.0, 0.05, 0.18, 0.35)

#: 90 天仅退款笔数池：覆盖 R5 的 HIGH_REFUND_COUNT=6
_DEMO_REFUND_COUNT: tuple[int, ...] = (0, 1, 3, 7)

#: 物流状态 → 面向界面的中文名（演示夹具摘要用）
_LOGISTICS_TEXT = {
    "signed": "已签收",
    "not_signed": "已到货未签收",
    "in_transit": "运输中",
    "not_shipped": "未发货",
}

_LOGI_TRACES = {
    "signed": ["已揽收", "运输中", "派送中", "已签收"],
    "not_signed": ["已揽收", "运输中", "派送中", "待签收"],
    "in_transit": ["已揽收", "运输中"],
    "not_shipped": [],
}


def demo_active() -> bool:
    """演示夹具当前是否生效：开关打开**且**未接入真实业务 API。"""
    return bool(settings.demo_any_order) and not _real_api_configured()


def _pick(pool, digest: bytes, index: int):
    """按摘要的某个字节从池中取值（确定性：同一订单号恒得同一结果）。"""
    return pool[digest[index] % len(pool)]


def build_demo_facts(order_id: str) -> dict[str, Any]:
    """由订单号**确定性**派生一组演示事实。

    为什么用 sha256 而不是内置 `hash()`：内置哈希带进程随机种子，重启即变，
    会让"同一订单号同结论"这条项目铁律失效。
    """
    digest = hashlib.sha256(order_id.encode("utf-8")).digest()
    return {
        "order_id": order_id,
        "item_category": _pick(_DEMO_CATEGORIES, digest, 1),
        "goods_no": _pick(_DEMO_GOODS_NOS, digest, 7),
        "pay_amount": float(_pick(_DEMO_AMOUNTS, digest, 0)),
        "logistics_status": _pick(_DEMO_LOGISTICS, digest, 2),
        "signed_days": int(_pick(_DEMO_SIGNED_DAYS, digest, 3)),
        "profile": {
            "buyer_id": "DEMO-" + digest[:4].hex().upper(),
            "buyer_credit_score": int(_pick(_DEMO_CREDIT, digest, 4)),
            "buyer_refund_rate_90d": float(_pick(_DEMO_REFUND_RATE, digest, 5)),
            "refund_count_90d": int(_pick(_DEMO_REFUND_COUNT, digest, 6)),
        },
    }


def _order_summary(facts: dict[str, Any]) -> dict[str, Any]:
    """把**合成的**夹具事实裁成界面可直接展示的摘要（不含内部字段）。"""
    status = facts["logistics_status"]
    return {
        "order_id": facts["order_id"],
        "item_category": facts["item_category"],
        "goods_no": facts.get("goods_no", ""),
        "pay_amount": facts["pay_amount"],
        "logistics_status": status,
        "logistics_text": _LOGISTICS_TEXT.get(status, status),
        "signed_days": facts["signed_days"],
    }


def _summary_from_seeded(order_id: str) -> dict[str, Any] | None:
    """从**已注入的事实**生成摘要（用于"订单已存在"分支）。

    ⚠️ 刻意不重新合成：界面必须显示**规则实际使用的那份事实**。
    曾用 `build_demo_facts()` 现算，结果 DEMO002 实际是「手机壳 ¥199」（demo_orders.yaml），
    界面却显示「家居日用 ¥19.9」——演示数据自相矛盾，且违背"不伪装"原则。
    """
    row = peek_order(order_id)
    if row is None:
        return None
    status = str(row.get("logistics_status") or "")
    signed_at = row.get("signed_at")
    signed_days = None
    if isinstance(signed_at, datetime):
        signed_days = max(0, (datetime.now() - signed_at).days)
    return {
        "order_id": order_id,
        "item_category": str(row.get("item_category") or ""),
        "goods_no": str(row.get("goods_no") or ""),
        "pay_amount": float(row.get("pay_amount") or 0.0),
        "logistics_status": status,
        "logistics_text": _LOGISTICS_TEXT.get(status, status),
        "signed_days": signed_days,
    }


async def backfill_case_goods() -> int:
    """把**已有工单**的 `goods_no` 按 order_id 从夹具补齐（幂等；仅本地夹具模式）。

    为什么要回填：`goods_no` 是 2026-10-16 才加的列，此前创建的工单没有它 ——
    不回填的话「商品 × 售后」统计对历史工单全为空，功能等于看不见。

    三条边界：只更新 `goods_no IS NULL` 的行；夹具里查不到的订单**原样跳过**（不猜、不编）；
    已接入真实 API 时整体跳过（与 `load_demo_orders` 同口径）。
    """
    if _real_api_configured():
        return 0
    from app.db.connection import connect, run_sync

    def _run() -> int:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT case_id, order_id FROM conversation_case WHERE goods_no IS NULL")
            fixed = 0
            for case_id, order_id in cur.fetchall():
                row = peek_order(str(order_id or ""))
                goods_no = str((row or {}).get("goods_no") or "")
                if not goods_no:
                    continue
                cur.execute(
                    "UPDATE conversation_case SET goods_no=%s "
                    "WHERE case_id=%s AND goods_no IS NULL",
                    (goods_no, case_id),
                )
                fixed += cur.rowcount
            return fixed

    try:
        fixed = await run_sync(_run)
    except Exception as exc:  # noqa: BLE001 —— 回填是旁路，失败不阻断启动
        logger.warning("历史工单商品编号回填失败（已忽略）：%s: %s", type(exc).__name__, exc)
        return 0
    if fixed:
        logger.info("已为 %d 个历史工单补齐商品编号（商品 × 售后联动）", fixed)
    return fixed


def ensure_demo_order(order_id: str) -> dict[str, Any]:
    """确保该订单可被受理（演示 / 测试用），返回夹具摘要。

    返回体固定含 `synthetic` 字段——调用方（接口与界面）**必须如实标注**，不得伪装。
    `reason` 取值：`generated` / `already_exists` / `real_api_configured` / `empty_order_id`。
    """
    order_id = (order_id or "").strip()
    if not order_id:
        return {"created": False, "synthetic": False, "reason": "empty_order_id"}
    if _real_api_configured():
        return {"created": False, "synthetic": False, "reason": "real_api_configured"}
    if is_seeded(order_id):
        # 已存在：**回显真正注入的事实**，绝不重新合成
        return {
            "created": False,
            "synthetic": True,
            "reason": "already_exists",
            "order": _summary_from_seeded(order_id),
        }

    facts = build_demo_facts(order_id)
    status = facts["logistics_status"]
    signed_at = (
        datetime.now() - timedelta(days=facts["signed_days"]) if status == "signed" else None
    )

    seed_order(
        order_id,
        item_category=facts["item_category"],
        goods_no=facts["goods_no"],
        pay_amount=facts["pay_amount"],
        logistics_status=status,
        signed_at=signed_at,
    )
    seed_logistics(
        order_id,
        logistics_status=status,
        signed_at=signed_at,
        traces=list(_LOGI_TRACES.get(status, [])),
    )
    seed_profile(order_id, **facts["profile"])

    logger.info(
        "演示夹具已生成：order_id=%s 金额=%.2f 类目=%s 物流=%s 签收后=%s天",
        order_id, facts["pay_amount"], facts["item_category"], status, facts["signed_days"],
    )
    return {
        "created": True,
        "synthetic": True,
        "reason": "generated",
        "order": _order_summary(facts),
        "profile": facts["profile"],
    }
