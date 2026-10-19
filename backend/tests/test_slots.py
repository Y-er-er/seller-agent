"""槽位定义与装配测试（P1.2）—— **全部离线**，纯逻辑，无 IO、无模型调用。

覆盖要点：
  T1  清洗：枚举越界丢弃、未知键丢弃、超长截断
  T2  必填校验：缺任一必填 → missing（**不猜测**）
  T3  装配：权威字段（金额 / 类目）以**订单**为准
  T4  金额收敛：主张金额超过实付 → 收敛为实付；未主张 → 默认全额
  T5  冲突检测：买家陈述 vs 权威数据冲突 → 记录且**以权威为准**
  T6  物理隔离：槽位键**不在**规则事实字典（FACT_FIELDS）
  T7  诉求方向接线：`claim_direction` → `RefundRequest.claim_type`（补发必须可达）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import slots  # noqa: E402
from app.models import ClaimType, LogisticsInfo, LogisticsStatus, OrderSnapshot, RefundReason  # noqa: E402
from app.rules.facts import FACT_FIELDS  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


def _order(pay: float = 300.0, cat: str = "日用百货") -> OrderSnapshot:
    return OrderSnapshot(order_id="O1", item_category=cat, pay_amount=pay)


def _logistics(status: LogisticsStatus) -> LogisticsInfo:
    return LogisticsInfo(order_id="O1", logistics_status=status)


def main() -> None:
    print("=" * 68)
    print("槽位定义与装配（P1.2）")
    print("=" * 68)

    # ---------- T1 清洗 ----------
    cleaned = slots.clean_slots({
        "issue_reason": "未收到货",
        "claim_type": "not_a_slot",             # 旧键名 → 未知键，丢弃
        "issue_object": "不存在的对象",          # 枚举越界 → 丢弃
        "claim_direction": "仅退款",
        "symptom": " 破了一个角 ",
        "note": "x" * 200,                      # 超长 → 截断
    })
    check("T1a 未知键被丢弃", "claim_type" not in cleaned)
    check("T1b 枚举越界被丢弃", "issue_object" not in cleaned, f"实得 {cleaned.get('issue_object')!r}")
    check("T1c 正常值保留", cleaned.get("issue_reason") == "未收到货")
    check("T1d 空白被裁剪", cleaned.get("symptom") == "破了一个角")
    check("T1e 超长被截断", len(cleaned.get("note", "")) == slots.MAX_SLOT_VALUE_CHARS)

    # ---------- T2 必填校验 ----------
    missing = slots.missing_required({"issue_reason": "无理由"})
    check("T2a 缺两项必填", set(missing) == {"issue_object", "claim_direction"}, f"实得 {missing}")
    check("T2b 齐备时为空", slots.missing_required({
        "issue_reason": "无理由", "issue_object": "商品本身", "claim_direction": "仅退款"
    }) == [])

    # ---------- T3 装配（权威字段） ----------
    good = {"issue_reason": "无理由", "issue_object": "商品本身", "claim_direction": "仅退款"}
    asm = slots.assemble_request(
        order_id="O1", slots=good, order=_order(pay=88.5, cat="数码"),
        logistics=_logistics(LogisticsStatus.IN_TRANSIT),
    )
    check("T3a 装配成功", asm.ok and asm.request is not None)
    check("T3b 金额以订单为准（未主张→全额）",
          bool(asm.request) and asm.request.pay_amount == 88.5 and asm.request.claim_amount == 88.5)
    check("T3c 类目以订单为准", bool(asm.request) and asm.request.item_category == "数码")
    check("T3d 诉求映射到原因码", bool(asm.request) and asm.request.refund_reason_code == RefundReason.NO_REASON)

    # ---------- T4 金额收敛 ----------
    asm2 = slots.assemble_request(
        order_id="O1", slots=good, order=_order(pay=100.0), logistics=None, requested_amount=999.0
    )
    check("T4 主张超过实付 → 收敛为实付",
          bool(asm2.request) and asm2.request.claim_amount == 100.0,
          f"实得 {asm2.request.claim_amount if asm2.request else None}")

    # ---------- T5 冲突检测 ----------
    conflict_slots = {"issue_reason": "未收到货", "issue_object": "物流", "claim_direction": "仅退款"}
    asm3 = slots.assemble_request(
        order_id="O1", slots=conflict_slots, order=_order(), logistics=_logistics(LogisticsStatus.SIGNED)
    )
    check("T5a 未收到货 vs 已签收 → 记录冲突", len(asm3.conflicts) == 1, f"实得 {asm3.conflicts}")
    check("T5b 冲突以权威数据为准", bool(asm3.conflicts) and asm3.conflicts[0]["resolve"] == "以物流为准")
    asm4 = slots.assemble_request(
        order_id="O1", slots=conflict_slots, order=_order(), logistics=_logistics(LogisticsStatus.IN_TRANSIT)
    )
    check("T5c 无冲突时为空", asm4.conflicts == [])

    # ---------- T6 物理隔离 ----------
    overlap = {
        slots.SLOT_ISSUE_REASON, slots.SLOT_ISSUE_OBJECT, slots.SLOT_CLAIM_DIRECTION,
        slots.SLOT_SYMPTOM, slots.SLOT_AFFECTED_QTY, slots.SLOT_SIGNED_DAYS,
        slots.SLOT_EVIDENCE, slots.SLOT_NOTE,
    } & set(FACT_FIELDS)
    check("T6 槽位不进规则事实字典", not overlap, f"重叠：{overlap}")

    # ---------- T7 诉求方向接线（补发可达的**代码级**保证） ----------
    check("T7a 仅退款 → REFUND",
          slots.direction_to_claim_type("仅退款") == ClaimType.REFUND)
    check("T7b 补发 → RESHIP",
          slots.direction_to_claim_type("补发") == ClaimType.RESHIP)
    check("T7c 换货 → EXCHANGE",
          slots.direction_to_claim_type("换货") == ClaimType.EXCHANGE)
    check("T7d 未知/缺失 → REFUND（显式回落）",
          slots.direction_to_claim_type("") == ClaimType.REFUND
          and slots.direction_to_claim_type(None) == ClaimType.REFUND)

    reship_slots = {"issue_reason": "错发漏发", "issue_object": "商品本身", "claim_direction": "补发"}
    asm5 = slots.assemble_request(
        order_id="O1", slots=reship_slots, order=_order(), logistics=_logistics(LogisticsStatus.SIGNED)
    )
    check("T7e 装配写入诉求方向（补发）",
          bool(asm5.request) and asm5.request.claim_type == ClaimType.RESHIP,
          f"实得 {asm5.request.claim_type if asm5.request else None}")

    # 方向也可由显式参数覆盖（供编排层传参）
    asm6 = slots.assemble_request(
        order_id="O1", slots=reship_slots, order=_order(), logistics=None, claim_direction="仅退款"
    )
    check("T7f 显式 claim_direction 优先",
          bool(asm6.request) and asm6.request.claim_type == ClaimType.REFUND)

    print("-" * 68)
    print(f"结果：{'全部通过' if not failures else '存在失败'}")
    if failures:
        for item in failures:
            print(f"  - {item}")
        sys.exit(1)


if __name__ == "__main__":
    main()
