"""会话状态机与会话服务测试（P1.1）—— **全部离线**：内存假仓储替换 DB，无模型调用。

覆盖要点：
  S1  状态机：合法/非法迁移、终态判定
  S2  迁移守卫：终态 / 成本暂停 / 轮数耗尽
  S3  字数裁决：首次超长只提示（**不计轮次**）、再犯转人工
  S4  会话服务：创建（含幂等重入）、开场白、状态迁移
  S5  轮数与序号：正常累加；超长**不**消耗轮次
  S6  六个升级触发源全部可落库并进入终态
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import cost_gate, session, state  # noqa: E402
from app.config import settings  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


class FakeChatRepo:
    """内存假仓储：实现 session / cost_gate 用到的全部接口。"""

    def __init__(self) -> None:
        self.cases: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.slots: list[dict] = []
        self.guardrails: list[dict] = []
        self.usages: list[dict] = []

    # ---- 会话 ----
    async def create_case(self, case_id, order_id, goods_no="", timeout_at=None):
        if case_id in self.cases:
            return False
        self.cases[case_id] = {
            "case_id": case_id, "order_id": order_id, "status": state.CREATED,
            "round": 0, "last_seq": 0, "escalated_reason": None, "timeout_at": timeout_at,
            "goods_no": goods_no,
        }
        return True

    async def get_case(self, case_id):
        # ⚠️ 返回**副本**：真实 DB 每次查询都返回独立行对象；若直接返回内部字典，
        #    调用方的就地修改会污染其持有的快照，使测试结果与生产语义不一致（曾因此误报）。
        case = self.cases.get(case_id)
        return dict(case) if case else None

    async def update_case(self, case_id, status=None, escalated_reason=None,
                          round_delta=0, last_seq=None, timeout_at=None,
                          workflow_status=None, assignee=None,
                          decision_result=None, decision_amount=None,
                          confirmed_by=None, confirmed_at=None):
        case = self.cases.get(case_id)
        if case is None:
            return False
        if status is not None:
            case["status"] = status
        if escalated_reason is not None:
            case["escalated_reason"] = escalated_reason
        if round_delta:
            case["round"] += round_delta
        if last_seq is not None:
            case["last_seq"] = max(case["last_seq"], last_seq)
        if workflow_status is not None:
            case["workflow_status"] = workflow_status
        if assignee is not None:
            case["assignee"] = assignee
        if decision_result is not None:
            case["decision_result"] = decision_result
        if decision_amount is not None:
            case["decision_amount"] = decision_amount
        if confirmed_by is not None:
            case["confirmed_by"] = confirmed_by
        if confirmed_at is not None:
            case["confirmed_at"] = confirmed_at
        return True

    async def list_cases_by_status(self, status, limit=50):
        return [c for c in self.cases.values() if c["status"] == status][:limit]

    # ---- 消息 ----
    async def append_message(self, case_id, seq, role, content=None, attachments=None,
                             tool_calls=None, guardrail_event=None):
        if any(m["case_id"] == case_id and m["seq"] == seq for m in self.messages):
            return False
        self.messages.append({
            "case_id": case_id, "seq": seq, "role": role, "content": content,
            "tool_calls": tool_calls, "guardrail_event": guardrail_event,
            "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        })
        return True

    async def list_messages(self, case_id, limit=None):
        rows = [m for m in self.messages if m["case_id"] == case_id]
        return rows[:limit] if limit else rows

    # ---- 槽位 ----
    async def upsert_slot(self, case_id, slot, value, confidence=None, source_seq=None):
        self.slots.append({"case_id": case_id, "slot": slot, "value": value,
                           "source_seq": source_seq})
        return True

    async def list_slots(self, case_id):
        return [s for s in self.slots if s["case_id"] == case_id]

    # ---- 护栏 ----
    async def record_guardrail(self, case_id, event_type, detail=None):
        self.guardrails.append({"case_id": case_id, "type": event_type, "detail": detail})
        return True

    async def list_guardrails(self, case_id, limit=50):
        return [g for g in self.guardrails if g["case_id"] == case_id][:limit]

    async def count_guardrail_since(self, event_type, since):
        return len([g for g in self.guardrails if g["type"] == event_type])

    # ---- 用量 ----
    async def insert_usage(self, case_id, seq, model, input_tokens, output_tokens, amount):
        self.usages.append({"case_id": case_id, "amount": amount,
                            "created_at": datetime.now()})
        return True

    async def sum_usage(self, since=None, model=None):
        rows = [u for u in self.usages if since is None or u["created_at"] >= since]
        # `model` 是额度判定用的按模型过滤（成本闸门会传）；本组用例只看金额，返回 0 token
        return {"amount": sum(u["amount"] for u in rows), "tokens": 0, "calls": len(rows)}


_ORIG = {
    "uri": settings.mysql_uri,
    "max_rounds": settings.max_rounds,
    "max_msg_chars": settings.max_msg_chars,
    "price_in": settings.price_input_per_1k,
    "price_out": settings.price_output_per_1k,
    "quota_expire": settings.free_quota_expire_date,
    "quota_baseline": settings.free_quota_used_baseline,
}


def restore() -> None:
    settings.mysql_uri = _ORIG["uri"]
    settings.max_rounds = _ORIG["max_rounds"]
    settings.max_msg_chars = _ORIG["max_msg_chars"]
    settings.price_input_per_1k = _ORIG["price_in"]
    settings.price_output_per_1k = _ORIG["price_out"]
    settings.free_quota_expire_date = _ORIG["quota_expire"]
    settings.free_quota_used_baseline = _ORIG["quota_baseline"]


def wire(fake: FakeChatRepo) -> FakeChatRepo:
    """把假仓储接到 session 与 cost_gate（两者各自持有模块级引用）。"""
    settings.mysql_uri = "mysql+pymysql://fake:fake@127.0.0.1:3306/fake"
    settings.price_input_per_1k = 0.0      # 单价未配置 → 闸门走 token 兜底，测试中不会暂停
    settings.price_output_per_1k = 0.0
    # 隔离真实额度配置：否则闸门状态会随真实到期日 / 外部基线漂移，导致测试结果不确定
    settings.free_quota_expire_date = ""
    settings.free_quota_used_baseline = 0
    session.repo = fake
    cost_gate.chat_repo = fake
    cost_gate.reset_fallback()
    return fake


async def amain() -> int:
    # ============================================================
    # S1 状态机（纯函数）
    # ============================================================
    check("S1 合法迁移 CREATED→CLARIFYING", state.can_transition(state.CREATED, state.CLARIFYING))
    check("S1 合法迁移 CLARIFYING→SUBMITTED", state.can_transition(state.CLARIFYING, state.SUBMITTED))
    check("S1 非法迁移 CLARIFYING→DECIDED",
          state.transition_error(state.CLARIFYING, state.DECIDED) is not None)
    check("S1 非法迁移 CLOSED→CLARIFYING",
          state.transition_error(state.CLOSED, state.CLARIFYING) is not None)
    check("S1 终态判定", state.is_terminal(state.ESCALATED) and state.is_terminal(state.CLOSED))
    check("S1 DECIDED 非终态但只能关闭",
          (not state.is_terminal(state.DECIDED)) and state.TRANSITIONS[state.DECIDED] == (state.CLOSED,))

    # ============================================================
    # S2 迁移守卫
    # ============================================================
    v = state.check_can_accept_message(status=state.CLARIFYING, round_used=0, gate_paused=False)
    check("S2 正常可接收", v.allowed is True, f"实得 {v}")

    v = state.check_can_accept_message(status=state.ESCALATED, round_used=0, gate_paused=False)
    check("S2 终态拒绝并提示会话已结束", v.allowed is False and v.code == "closed", f"实得 {v}")

    v = state.check_can_accept_message(status=state.CLARIFYING, round_used=0, gate_paused=True)
    check("S2 成本暂停拒绝并需转人工",
          v.allowed is False and v.code == "cost_paused"
          and v.escalate_reason == state.ESCALATE_COST_PAUSED, f"实得 {v}")

    v = state.check_can_accept_message(status=state.CLARIFYING, round_used=20, gate_paused=False)
    check("S2 轮数耗尽拒绝并需转人工",
          v.allowed is False and v.code == "round_limit"
          and v.escalate_reason == state.ESCALATE_ROUND_LIMIT, f"实得 {v}")

    # ============================================================
    # S3 字数裁决
    # ============================================================
    short = state.evaluate_message_length("你好", already_warned=False)
    check("S3 正常长度放行", short.ok is True)

    first = state.evaluate_message_length("字" * 250, already_warned=False)
    check("S3 首次超长：只提示、不转人工",
          first.ok is False and first.code == "msg_too_long_first" and first.escalate is False,
          f"实得 {first.code}")

    repeat = state.evaluate_message_length("字" * 250, already_warned=True)
    check("S3 再次超长：转人工",
          repeat.ok is False and repeat.code == "msg_too_long_repeat" and repeat.escalate is True,
          f"实得 {repeat.code}")

    # ============================================================
    # S4 创建会话
    # ============================================================
    fake = wire(FakeChatRepo())
    created = await session.create_session("ORD-1")
    check("S4 创建会话成功", created["resumed"] is False and created["case_id"] == "ORD-1")
    check("S4 开场白已写入且状态为 CLARIFYING",
          fake.cases["ORD-1"]["status"] == state.CLARIFYING
          and any(m["role"] == "agent" and m["seq"] == 1 for m in fake.messages),
          f"状态={fake.cases['ORD-1']['status']} 消息数={len(fake.messages)}")

    again = await session.create_session("ORD-1")
    check("S4 重复创建为幂等重入（不新建工单）",
          again["resumed"] is True and len(fake.cases) == 1, f"实得 {again['resumed']}")

    # ============================================================
    # S5 正常消息：轮次与序号
    # ============================================================
    r1 = await session.append_buyer_message("ORD-1", "包裹破损了")
    check("S5 正常接收买家消息",
          r1["accepted"] is True and r1["round"] == 1 and r1["seq"] == 2,
          f"实得 round={r1.get('round')} seq={r1.get('seq')}")

    # 超长不消耗轮次
    r2 = await session.append_buyer_message("ORD-1", "字" * 250)
    check("S5 首次超长：拒绝且**不计轮次**",
          r2["accepted"] is False and r2["code"] == "msg_too_long_first"
          and fake.cases["ORD-1"]["round"] == 1,
          f"code={r2.get('code')} round={fake.cases['ORD-1']['round']}")

    # 再犯 → 转人工
    r3 = await session.append_buyer_message("ORD-1", "字" * 250)
    check("S5 再次超长：转人工并进入终态",
          r3["accepted"] is False and r3["escalated"] is True
          and fake.cases["ORD-1"]["status"] == state.ESCALATED,
          f"code={r3.get('code')} 状态={fake.cases['ORD-1']['status']}")
    check("S5 转人工原因码正确",
          fake.cases["ORD-1"]["escalated_reason"] == state.ESCALATE_MSG_TOO_LONG,
          f"实得 {fake.cases['ORD-1']['escalated_reason']}")

    # 终态后再发消息 → 拒绝
    r4 = await session.append_buyer_message("ORD-1", "还在吗")
    check("S5 终态拒绝新消息", r4["accepted"] is False and r4["code"] == "closed",
          f"实得 {r4.get('code')}")

    # ============================================================
    # S6 轮数耗尽与成本暂停
    # ============================================================
    fake = wire(FakeChatRepo())
    await session.create_session("ORD-2")
    fake.cases["ORD-2"]["round"] = 20  # 模拟已聊满
    r = await session.append_buyer_message("ORD-2", "再帮我看看")
    check("S6 轮数耗尽 → 拒绝并转人工",
          r["accepted"] is False and r["code"] == "round_limit"
          and fake.cases["ORD-2"]["status"] == state.ESCALATED,
          f"code={r.get('code')} 状态={fake.cases['ORD-2']['status']}")

    fake = wire(FakeChatRepo())
    await session.create_session("ORD-3")
    settings.price_input_per_1k = 1.0  # 配置单价后，塞一条**略高于预警线**的用量
    fake.usages.append(
        {"case_id": None, "amount": settings.cost_warning_cny + 0.1, "created_at": datetime.now()}
    )
    r = await session.append_buyer_message("ORD-3", "在吗")
    check("S6 成本暂停 → 拒绝并转人工",
          r["accepted"] is False and r["code"] == "cost_paused"
          and fake.cases["ORD-3"]["status"] == state.ESCALATED,
          f"code={r.get('code')} 状态={fake.cases['ORD-3']['status']}")
    settings.price_input_per_1k = 0.0

    # ============================================================
    # S7 六个升级触发源
    # ============================================================
    fake = wire(FakeChatRepo())
    for idx, reason in enumerate(state.ESCALATE_REASONS, start=1):
        cid = f"ORD-E{idx}"
        await session.create_session(cid)
        await session.escalate(cid, reason)
        case = fake.cases[cid]
        ok = (case["status"] == state.ESCALATED and case["escalated_reason"] == reason
              and any(g["case_id"] == cid and g["type"] == reason for g in fake.guardrails))
        check(f"S7 升级触发源可落库：{reason}", ok,
              f"状态={case['status']} 原因={case['escalated_reason']}")

    # ============================================================
    # S8 非法迁移被拒（不静默放行）
    # ============================================================
    fake = wire(FakeChatRepo())
    await session.create_session("ORD-4")
    try:
        await session.set_status("ORD-4", state.DECIDED)  # CLARIFYING → DECIDED 非法
        check("S8 非法迁移抛错", False, "未抛错")
    except session.SessionError as exc:
        check("S8 非法迁移抛错（409）", exc.status_code == 409, f"实得 {exc.status_code}: {exc.message}")

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
