"""商家售后指挥台服务测试 —— 假仓储，零外部调用。

覆盖：工单状态流转（queued → agent_running → awaiting_confirm → resolved / escalated）、
      list 过滤、get_detail 404、dispatch 非法状态 409、confirm 落确认、reject 转人工。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import console  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class FakeRepo:
    """假仓储：内存字典，语义贴近真库（读返回副本，避免污染快照）。"""

    def __init__(self) -> None:
        self.cases: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.slots: list[dict] = []
        self.guardrails: list[dict] = []

    def seed_case(self, case_id: str, order_id: str = "O1", workflow_status: str = "queued", **extra) -> None:
        self.cases[case_id] = {
            "case_id": case_id, "order_id": order_id, "status": "CLARIFYING",
            "round": 0, "last_seq": 1, "escalated_reason": None, "timeout_at": None,
            "workflow_status": workflow_status, "assignee": None,
            "decision_result": None, "decision_amount": None,
            "confirmed_by": None, "confirmed_at": None,
            **extra,
        }

    async def get_case(self, case_id: str):
        c = self.cases.get(case_id)
        return dict(c) if c else None

    async def update_case(self, case_id: str, **fields):
        c = self.cases.get(case_id)
        if c is None:
            return False
        for k, v in fields.items():
            if v is None:
                continue
            if k == "round_delta":
                c["round"] = int(c.get("round", 0)) + int(v)
            else:
                c[k] = v
        return True

    async def list_cases_by_workflow(self, workflow_status=None, limit=50):
        rows = list(self.cases.values())
        if workflow_status:
            rows = [c for c in rows if c.get("workflow_status") == workflow_status]
        return [dict(c) for c in rows][:limit]

    async def list_messages(self, case_id, limit=None):
        return [m for m in self.messages if m["case_id"] == case_id]

    async def list_slots(self, case_id):
        return [s for s in self.slots if s["case_id"] == case_id]

    async def list_guardrails(self, case_id, limit=50):
        return [g for g in self.guardrails if g["case_id"] == case_id]

    async def record_guardrail(self, case_id, event_type, detail=None):
        self.guardrails.append({"case_id": case_id, "type": event_type, "detail": detail})
        return True


async def main() -> int:
    fake = FakeRepo()
    console.repo = fake  # 注入假仓储

    # ---- C1 list 过滤 ----
    fake.seed_case("C1", workflow_status="queued")
    fake.seed_case("C2", workflow_status="agent_running")
    fake.seed_case("C3", workflow_status="resolved")
    items = await console.list_cases("queued")
    check("C1 收件箱只返回 queued", [i["case_id"] for i in items] == ["C1"], str(items))
    items_all = await console.list_cases()
    check("C1 不传状态返回全部", len(items_all) == 3)

    # ---- C2 get_detail 404 + 详情 ----
    try:
        await console.get_detail("NOPE")
        check("C2 不存在工单 → 404", False, "未抛异常")
    except console.ConsoleError as exc:
        check("C2 不存在工单 → 404", exc.status_code == 404 and exc.code == "case_not_found")

    fake.messages.append({"case_id": "C1", "role": "buyer", "content": "包裹破损", "seq": 1})
    detail = await console.get_detail("C1")
    check("C2 详情含完整消息", len(detail["messages"]) == 1 and detail["case"]["case_id"] == "C1")

    # ---- C3 dispatch 非法状态（resolved 不可指派）----
    try:
        await console.dispatch("C3")
        check("C3 终态不可指派 → 409", False, "未抛异常")
    except console.ConsoleError as exc:
        check("C3 终态不可指派 → 409", exc.status_code == 409)

    # ---- C4 dispatch 正常：标记 agent_running + 调 process_case ----
    dispatched = {}
    async def stub_process_case(case_id, **kw):
        dispatched["case_id"] = case_id
        return {"accepted": True, "kind": "model_unavailable", "reply": "降级"}
    _orig_process = console.orchestrator.process_case
    console.orchestrator.process_case = stub_process_case
    try:
        await console.dispatch("C1")
    finally:
        console.orchestrator.process_case = _orig_process
    c1 = await fake.get_case("C1")
    check("C4 指派后 workflow_status=agent_running", c1["workflow_status"] == "agent_running", str(c1))
    check("C4 指派后 assignee=agent", c1["assignee"] == "agent")
    check("C4 指派调用了 process_case", dispatched.get("case_id") == "C1")

    # ---- C5 confirm 非法状态（非 awaiting_confirm 不可确认）----
    try:
        await console.confirm("C1")
        check("C5 非待确认不可确认 → 409", False, "未抛异常")
    except console.ConsoleError as exc:
        check("C5 非待确认不可确认 → 409", exc.status_code == 409)

    # ---- C6 confirm 正常：resolved + 确认人/时间 ----
    fake.seed_case("C4", workflow_status="awaiting_confirm", decision_result="refund", decision_amount=199.0)
    r = await console.confirm("C4", reviewer="admin1")
    c4 = await fake.get_case("C4")
    check("C6 确认后 resolved", r["workflow_status"] == "resolved" and c4["workflow_status"] == "resolved")
    check("C6 确认人已记录", c4["confirmed_by"] == "admin1")
    check("C6 确认时间已记录", c4["confirmed_at"] is not None)
    check("C6 落护栏事件", any(g["type"] == "confirmed" for g in fake.guardrails))

    # ---- C7 reject → escalated + assignee=manual ----
    fake.seed_case("C5", workflow_status="awaiting_confirm", decision_result="reject")
    await console.reject("C5", reason="依据不足")
    c5 = await fake.get_case("C5")
    check("C7 驳回后 escalated", c5["workflow_status"] == "escalated" and c5["assignee"] == "manual")
    check("C7 驳回落护栏", any(g["type"] == "rejected" for g in fake.guardrails))

    # ---- C8 intervene → escalated + manual ----
    fake.seed_case("C6", workflow_status="agent_running")
    await console.intervene("C6")
    c6 = await fake.get_case("C6")
    check("C8 介入后 escalated + manual", c6["workflow_status"] == "escalated" and c6["assignee"] == "manual")

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    if _failures:
        for f in _failures:
            print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
