"""演示夹具测试（买家界面「演示模式」）—— **全部离线**：无 IO、无模型调用。

覆盖要点：
  D1  确定性：同一订单号恒得同一份事实（sha256 派生，不用带随机种子的内置 hash）
  D2  覆盖度：取值池跨越关键业务阈值（金额 ¥50/¥300、信用分 650、退款率 0.20、时效 15 天、频次 6 笔）
  D3  注入生效：注入后订单 / 物流 / 画像三者都能取到，且金额与夹具一致
  D4  幂等：重复注入不覆盖既有事实（reason=already_exists）
  D5  边界：空订单号 / 已接入真实 API 时**拒绝生成**，且 `demo_active()` 为 False
  D6  不伪装：返回体一律带 `synthetic` 标记（生成与已存在两条分支都要有）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import demo_seed  # noqa: E402
from app.config import settings  # noqa: E402
from app.tools.logistics import fetch_logistics  # noqa: E402
from app.tools.order import fetch_order, seed_order  # noqa: E402
from app.tools.profile import fetch_profile  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


#: 用于覆盖度统计的订单号样本（够多才能把取值池走遍）
_SAMPLE_IDS = [f"COVER-{i:03d}" for i in range(120)]


async def amain() -> int:
    _orig = {
        "order_api_base": settings.order_api_base,
        "logistics_api_base": settings.logistics_api_base,
        "demo_any_order": settings.demo_any_order,
    }
    settings.order_api_base = ""
    settings.logistics_api_base = ""
    settings.demo_any_order = True

    try:
        # ---------- D1 确定性 ----------
        a = demo_seed.build_demo_facts("TEST-001")
        b = demo_seed.build_demo_facts("TEST-001")
        check("D1a 同一订单号恒得同一份事实（可复现）", a == b)
        check("D1b 不同订单号得到不同事实",
              demo_seed.build_demo_facts("TEST-001") != demo_seed.build_demo_facts("TEST-002"))

        # ---------- D2 覆盖度（跨关键阈值） ----------
        facts = [demo_seed.build_demo_facts(oid) for oid in _SAMPLE_IDS]
        amounts = {f["pay_amount"] for f in facts}
        check("D2a 金额跨越 R2/R4 小额线（¥50）",
              any(v < 50 for v in amounts) and any(50 < v < 300 for v in amounts),
              f"最小 {min(amounts)} 最大 {max(amounts)}")
        check("D2b 金额跨越 R1 金额闸门（¥300）", any(v >= 300 for v in amounts),
              f"最大 {max(amounts)}")
        credits = {f["profile"]["buyer_credit_score"] for f in facts}
        check("D2c 信用分跨越 R4 门槛（650）",
              any(v < 650 for v in credits) and any(v >= 650 for v in credits), f"实得 {sorted(credits)}")
        rates = {f["profile"]["buyer_refund_rate_90d"] for f in facts}
        check("D2d 退款率跨越 R4/R5 门槛（0.20）",
              any(v < 0.20 for v in rates) and any(v > 0.20 for v in rates), f"实得 {sorted(rates)}")
        days = {f["signed_days"] for f in facts}
        check("D2e 签收后时长跨越 R11 时效（15 天）",
              any(v < 15 for v in days) and any(v > 15 for v in days), f"实得 {sorted(days)}")
        counts = {f["profile"]["refund_count_90d"] for f in facts}
        check("D2f 退款频次跨越 R5 门槛（6 笔）",
              any(v < 6 for v in counts) and any(v >= 6 for v in counts), f"实得 {sorted(counts)}")
        statuses = {f["logistics_status"] for f in facts}
        check("D2g 物流状态覆盖签收与非签收", statuses == {"signed", "not_signed", "in_transit"},
              f"实得 {sorted(statuses)}")

        # ---------- D3 注入生效 ----------
        oid = "COVER-000"
        check("D3a 注入前取不到订单（真实链路语义）", await fetch_order(oid) is None)
        res = demo_seed.ensure_demo_order(oid)
        check("D3b 首次注入 created=True", res["created"] is True and res["reason"] == "generated",
              f"实得 {res.get('reason')}")
        order = await fetch_order(oid)
        logistics = await fetch_logistics(oid)
        profile = await fetch_profile(oid)
        expected = demo_seed.build_demo_facts(oid)
        check("D3c 订单可取到且金额与夹具一致",
              order is not None and order.pay_amount == expected["pay_amount"],
              f"实得 {order.pay_amount if order else None}")
        check("D3d 物流可取到且状态与夹具一致",
              logistics is not None
              and getattr(logistics.logistics_status, "value", logistics.logistics_status)
              == expected["logistics_status"])
        check("D3e 画像可取到且信用分与夹具一致",
              profile is not None and profile.buyer_credit_score == expected["profile"]["buyer_credit_score"])

        # ---------- D4 幂等 ----------
        res2 = demo_seed.ensure_demo_order(oid)
        check("D4a 重复注入不覆盖既有事实",
              res2["created"] is False and res2["reason"] == "already_exists",
              f"实得 {res2.get('reason')}")
        order2 = await fetch_order(oid)
        check("D4b 重复注入后金额不变", order2 is not None and order2.pay_amount == expected["pay_amount"])

        # ---------- D4c 已存在分支必须回显**真实**事实，不得重新合成 ----------
        # 场景来源：DEMO002 实际是「手机壳 ¥199」（config/demo_orders.yaml），
        # 而界面曾显示「家居日用 ¥19.9」—— 因为该分支用 build_demo_facts() 现算了一份。
        seed_order("SEEDED-1", item_category="手机壳", pay_amount=199.0, logistics_status="signed")
        res3 = demo_seed.ensure_demo_order("SEEDED-1")
        o3 = res3.get("order") or {}
        check("D4c 已存在分支回显真实事实（非合成值）",
              o3.get("item_category") == "手机壳" and o3.get("pay_amount") == 199.0,
              f"实得 {o3}")

        # ---------- D5 边界 ----------
        empty = demo_seed.ensure_demo_order("   ")
        check("D5a 空订单号 → 拒绝生成",
              empty["created"] is False and empty["reason"] == "empty_order_id", f"实得 {empty}")

        settings.order_api_base = "http://127.0.0.1:1/orders"
        real = demo_seed.ensure_demo_order("COVER-999")
        check("D5b 已接入真实 API → 拒绝生成（不污染真实链路）",
              real["created"] is False and real["reason"] == "real_api_configured", f"实得 {real}")
        check("D5c 已接入真实 API → demo_active 为 False", demo_seed.demo_active() is False)
        check("D5d 已接入真实 API → 不产生合成事实", await fetch_order("COVER-999") is None)
        settings.order_api_base = ""

        settings.demo_any_order = False
        check("D5e 开关关闭 → demo_active 为 False", demo_seed.demo_active() is False)
        settings.demo_any_order = True
        check("D5f 开关打开且无真实 API → demo_active 为 True", demo_seed.demo_active() is True)

        # ---------- D6 不伪装 ----------
        check("D6a 生成分支带 synthetic 标记", res["synthetic"] is True)
        check("D6b 已存在分支带 synthetic 标记", res2["synthetic"] is True)
        check("D6c 拒绝分支不宣称 synthetic", empty["synthetic"] is False and real["synthetic"] is False)
    finally:
        settings.order_api_base = _orig["order_api_base"]
        settings.logistics_api_base = _orig["logistics_api_base"]
        settings.demo_any_order = _orig["demo_any_order"]

    print("-" * 68)
    print(f"结果：{'全部通过' if not failures else '存在失败'}")
    if failures:
        for item in failures:
            print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
