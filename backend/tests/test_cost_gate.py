"""成本闸门测试（P1.1）—— **全部离线**：用内存假仓储替换 DB 访问，不产生任何模型调用。

覆盖要点：
  G1  计量：按单价折算等效消耗
  G2  记账：成功落库；落库失败 → **内存兜底**（闸门不因库抖动而失效）
  G3  三级判定：预警线 / 月度上限 / 免费额度耗尽
  G4  单价缺失时**不误判金额**，仅以 token 兜底（且状态标记 price_configured=false）
  G5  暂停**首次汇报一次**（不刷日志与事件表）
  G6  precheck 的「新会话 / 进行中」语义
  G7  DB 不可达时降级（degraded=true）但仍能给出判定
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import cost_gate  # noqa: E402
from app.config import settings  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


class FakeRepo:
    """内存假仓储：只实现成本闸门用到的三个函数。"""

    def __init__(self) -> None:
        self.usages: list[dict] = []
        self.guardrails: list[dict] = []
        self.fail_insert = False
        self.fail_sum = False

    async def insert_usage(self, case_id, seq, model, input_tokens, output_tokens, amount):
        if self.fail_insert:
            return False
        self.usages.append(
            {
                "case_id": case_id,
                "seq": seq,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "amount": amount,
                "created_at": datetime.now(),
            }
        )
        return True

    async def sum_usage(self, since=None):
        if self.fail_sum:
            raise RuntimeError("simulated db outage")
        rows = [r for r in self.usages if since is None or r["created_at"] >= since]
        return {
            "amount": sum(r["amount"] for r in rows),
            "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rows),
            "calls": len(rows),
        }

    async def record_guardrail(self, case_id, event_type, detail=None):
        self.guardrails.append({"case_id": case_id, "type": event_type, "detail": detail})
        return True


# 保存原始配置，测试结束后恢复（避免影响其他测试）
_ORIG = {
    "uri": settings.mysql_uri,
    "price_in": settings.price_input_per_1k,
    "price_out": settings.price_output_per_1k,
    "warning": settings.cost_warning_cny,
    "cap": settings.cost_monthly_cap_cny,
    "quota": settings.free_quota_tokens,
    "quota_expire": settings.free_quota_expire_date,
    "quota_baseline": settings.free_quota_used_baseline,
}


def restore() -> None:
    settings.mysql_uri = _ORIG["uri"]
    settings.price_input_per_1k = _ORIG["price_in"]
    settings.price_output_per_1k = _ORIG["price_out"]
    settings.cost_warning_cny = _ORIG["warning"]
    settings.cost_monthly_cap_cny = _ORIG["cap"]
    settings.free_quota_tokens = _ORIG["quota"]
    settings.free_quota_expire_date = _ORIG["quota_expire"]
    settings.free_quota_used_baseline = _ORIG["quota_baseline"]


def setup(
    fake: FakeRepo,
    *,
    price_in: float,
    price_out: float,
    quota: int = 1_000_000,
    baseline: int = 0,
    expire: str = "",
) -> None:
    settings.mysql_uri = "mysql+pymysql://fake:fake@127.0.0.1:3306/fake"
    settings.price_input_per_1k = price_in
    settings.price_output_per_1k = price_out
    settings.cost_warning_cny = 0.1
    settings.cost_monthly_cap_cny = 200.0
    settings.free_quota_tokens = quota
    settings.free_quota_used_baseline = baseline
    settings.free_quota_expire_date = expire
    cost_gate.chat_repo = fake
    cost_gate.reset_fallback()


async def amain() -> int:
    # ============================================================
    # G1 计量
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.002, price_out=0.006)
    amount = cost_gate.compute_amount(1000, 500)
    check("G1 按单价折算等效消耗", abs(amount - 0.005) < 1e-9, f"实得 {amount}")

    settings.price_input_per_1k = 0.0
    settings.price_output_per_1k = 0.0
    check("G1 未配置单价时金额为 0", cost_gate.compute_amount(1000, 500) == 0.0)

    # ============================================================
    # G2 记账：成功 / 失败兜底
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.002, price_out=0.006)
    result = await cost_gate.record_usage(1000, 500, case_id="C1", seq=1)
    check("G2 记账成功落库", result["persisted"] is True and len(fake.usages) == 1,
          f"实得 {result}，库内 {len(fake.usages)} 条")

    fake.fail_insert = True
    cost_gate.reset_fallback()
    result = await cost_gate.record_usage(2000, 0, case_id="C1", seq=2)
    status = await cost_gate.evaluate()
    check("G2 落库失败时转内存兜底（闸门不失效）",
          result["persisted"] is False and status.month_tokens >= 2000,
          f"persisted={result['persisted']} month_tokens={status.month_tokens}")

    # ============================================================
    # G3 三级判定
    # ============================================================
    # 预警线：¥0.1（单价 1 元/1K token 时，100 tokens 即 0.1 元）
    fake = FakeRepo()
    setup(fake, price_in=1.0, price_out=0.0)
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 100, "output_tokens": 0,
         "amount": 0.1, "created_at": datetime.now()}
    )
    status = await cost_gate.evaluate()
    check("G3 达预警线 → 暂停（cost_warning）",
          status.paused and status.reason == cost_gate.GATE_COST_WARNING,
          f"实得 paused={status.paused} reason={status.reason} amount={status.month_amount}")

    # 月度上限：¥200
    fake = FakeRepo()
    setup(fake, price_in=1.0, price_out=0.0)
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 200_000, "output_tokens": 0,
         "amount": 200.0, "created_at": datetime.now()}
    )
    status = await cost_gate.evaluate()
    check("G3 达月度上限 → 暂停（cost_cap）",
          status.paused and status.reason == cost_gate.GATE_COST_CAP,
          f"实得 reason={status.reason} amount={status.month_amount}")

    # 免费额度耗尽（全量 token）
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, quota=1000)
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 1200, "output_tokens": 0,
         "amount": 0.0, "created_at": datetime.now()}
    )
    status = await cost_gate.evaluate()
    check("G3 免费额度耗尽 → 暂停（quota_exhausted）",
          status.paused and status.reason == cost_gate.GATE_QUOTA_EXHAUSTED,
          f"实得 reason={status.reason} total_tokens={status.total_tokens}")

    # ============================================================
    # G4 单价缺失：不误判金额，仅 token 兜底
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, quota=1_000_000)
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 5000, "output_tokens": 0,
         "amount": 0.0, "created_at": datetime.now()}
    )
    status = await cost_gate.evaluate()
    check("G4 单价缺失时不误判为暂停", status.paused is False,
          f"实得 paused={status.paused} reason={status.reason}")
    check("G4 状态标记 price_configured=false", status.price_configured is False)

    # ============================================================
    # G5 暂停首次汇报一次
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=1.0, price_out=0.0)
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 100, "output_tokens": 0,
         "amount": 0.1, "created_at": datetime.now()}
    )
    await cost_gate.precheck(ongoing=False)
    await cost_gate.precheck(ongoing=False)
    await cost_gate.precheck(ongoing=True)
    warns = [g for g in fake.guardrails if g["type"] == cost_gate.GATE_COST_WARNING]
    check("G5 暂停只汇报一次（不刷事件表）", len(warns) == 1, f"实得 {len(warns)} 条")

    # ============================================================
    # G6 precheck 语义
    # ============================================================
    status_new = await cost_gate.precheck(ongoing=False)
    status_ongoing = await cost_gate.precheck(ongoing=True)
    check("G6 新会话与进行中会话均返回 paused（由调用方决定行为）",
          status_new.paused and status_ongoing.paused,
          f"新会话={status_new.paused} 进行中={status_ongoing.paused}")

    # 正常情况：新会话可创建
    fake = FakeRepo()
    setup(fake, price_in=0.002, price_out=0.006)
    ok_new = await cost_gate.precheck(ongoing=False)
    check("G6 正常时闸门 active", ok_new.paused is False, f"实得 {ok_new.reason}")

    # ============================================================
    # G7 DB 不可达时降级
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.002, price_out=0.006)
    fake.fail_sum = True
    status = await cost_gate.evaluate()
    check("G7 DB 不可达 → degraded=true 且不抛异常", status.degraded is True,
          f"实得 degraded={status.degraded}")

    # ============================================================
    # G8 外部基线抵扣（官方控制台已用量，非本项目消耗）
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, baseline=26_950)
    status = await cost_gate.evaluate()
    check("G8 已用量含外部基线", status.quota_used == 26_950, f"实得 {status.quota_used}")
    check("G8 剩余额度 = 总量 - 基线", status.quota_remaining == 1_000_000 - 26_950,
          f"实得 {status.quota_remaining}")

    # 本项目再消耗后，剩余额度继续下降
    fake.usages.append(
        {"case_id": "C", "seq": 1, "model": "m", "input_tokens": 500, "output_tokens": 0,
         "amount": 0.0, "created_at": datetime.now()}
    )
    status = await cost_gate.evaluate()
    check("G8 本地累计与基线叠加", status.quota_used == 26_950 + 500,
          f"实得 {status.quota_used}")

    # ============================================================
    # G9 额度**到期** → 暂停（与耗尽同等对待：到期后转为按量付费）
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, expire="2020-01-01")  # 早已到期
    status = await cost_gate.evaluate()
    check("G9 额度到期 → 暂停（quota_expired）",
          status.paused and status.reason == cost_gate.GATE_QUOTA_EXPIRED,
          f"实得 paused={status.paused} reason={status.reason}")

    # 到期优先于其他判定：即便额度还很多、单价未配置，也应暂停
    check("G9 到期优先于额度耗尽判断", status.quota_remaining > 0,
          f"剩余={status.quota_remaining}")

    # ============================================================
    # G10 到期日解析：未配置 / 非法格式 / 正常
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, expire="")
    status = await cost_gate.evaluate()
    check("G10 未配置到期日 → 不因到期暂停，days=-1",
          status.paused is False and status.days_to_expire == -1,
          f"实得 paused={status.paused} days={status.days_to_expire}")

    setup(fake, price_in=0.0, price_out=0.0, expire="2026/10/31")  # 格式非法
    status = await cost_gate.evaluate()
    check("G10 到期日格式非法 → 容错不暂停，days=-1",
          status.paused is False and status.days_to_expire == -1,
          f"实得 paused={status.paused} days={status.days_to_expire}")

    # 官方控制台核对值：2026-10-31，自 2026-10-14 起剩 17 天
    setup(fake, price_in=0.0, price_out=0.0, expire="2026-10-31")
    expire_day, days = cost_gate.quota_expire_date(today=date(2026, 10, 14))
    check("G10 到期日解析正确（2026-10-31 → 距 2026-10-14 共 17 天）",
          expire_day == date(2026, 10, 31) and days == 17,
          f"实得 {expire_day} 剩 {days} 天")

    status = await cost_gate.evaluate()
    check("G10 到期日未到 → 不暂停", status.paused is False,
          f"实得 paused={status.paused} reason={status.reason}")

    # ============================================================
    # G11 额度耗尽（含基线）→ 暂停
    # ============================================================
    fake = FakeRepo()
    setup(fake, price_in=0.0, price_out=0.0, quota=1000, baseline=1600)
    status = await cost_gate.evaluate()
    check("G11 基线已将额度吃满 → quota_exhausted",
          status.paused and status.reason == cost_gate.GATE_QUOTA_EXHAUSTED,
          f"实得 reason={status.reason} used={status.quota_used}")
    check("G11 剩余额度为负（已透支）", status.quota_remaining < 0,
          f"实得 {status.quota_remaining}")

    restore()
    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
