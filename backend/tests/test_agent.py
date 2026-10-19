"""对话循环与编排测试（P1.2）—— **全部离线**：脚本化模型桩 + 假仓储，**零模型调用**。

覆盖要点：
  A1  正常闭环：模型自主「查订单 → 查物流 → 提交判定 → 收尾」
  A2  缺必填 → 提交被拒 → 模型追问（**不猜测、不提交**）
  A3  模型直接回复（无工具调用）
  A4  取数失败：返回 available=false，**不编造**
  A5  单轮工具预算耗尽 → 强制收口
  A6  模型自主转人工 → 终态
  A7  成本闸门暂停 → 停止循环
  A8  模型不可用（None）→ 降级
  A9  未授权工具名 → 白名单拒绝（**不依赖模型守规矩**）
  A10 编排端到端：handle_buyer_message（含落库与状态迁移；**结论不被模型覆盖**）
  A11 编排降级：模型不可用 → 兜底话术 + 转人工
  A12 接管守卫：管理员接管后智能体**不再自动应答**（接管必须有阻断力）
  A13 人工发言进入模型上下文（加 `[人工客服]` 前缀，不改落库原文）
  A14 诉求方向接线：`claim_direction=补发` → 处置结果为 `reship`（补发可达）
  A15 人工接管时终态豁免：会话已 ESCALATED 时买家消息仍被接收（否则人接不上话）
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import agent, cost_gate, orchestrator, session, state, tools  # noqa: E402
from app.chat.llm_adapter import ModelTurn, ToolCall  # noqa: E402
from app.chat.tool_spec import TOOL_SPECS  # noqa: E402
from app.config import settings  # noqa: E402
from app.tools.logistics import seed_logistics  # noqa: E402
from app.tools.order import seed_order  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


# ============================================================
# 测试替身
# ============================================================


class ScriptedChatModel:
    """脚本化模型桩：按序返回预设 `ModelTurn`，并记录入参以便断言。

    ⚠️ 这是"离线跑通 agentic 循环"的关键——**不产生任何真实调用**。
    """

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self.seen_message_counts: list[int] = []
        self.seen_tool_specs: list[list] = []

    async def step(self, messages, tools):
        self.seen_message_counts.append(len(messages))
        self.seen_tool_specs.append(tools)
        if not self._turns:
            return ModelTurn(content="(脚本已耗尽)")
        turn = self._turns.pop(0)
        return turn(messages) if callable(turn) else turn


def tc(name: str, **arguments) -> ToolCall:
    return ToolCall(name=name, arguments=arguments, id=f"call_{name}")


def turn_tool(*calls: ToolCall) -> ModelTurn:
    return ModelTurn(content="", tool_calls=list(calls), usage={"input_tokens": 100, "output_tokens": 20})


def turn_text(text: str) -> ModelTurn:
    return ModelTurn(content=text, usage={"input_tokens": 80, "output_tokens": 15})


class FakeChatRepo:
    """内存假仓储（读接口返回**副本**，与真库语义一致）。"""

    def __init__(self) -> None:
        self.cases: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.slots: list[dict] = []
        self.guardrails: list[dict] = []
        self.usages: list[dict] = []

    async def create_case(self, case_id, order_id, timeout_at=None):
        if case_id in self.cases:
            return False
        self.cases[case_id] = {"case_id": case_id, "order_id": order_id, "status": state.CREATED,
                               "round": 0, "last_seq": 0, "escalated_reason": None, "timeout_at": timeout_at}
        return True

    async def get_case(self, case_id):
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

    async def append_message(self, case_id, seq, role, content=None, attachments=None,
                             tool_calls=None, guardrail_event=None):
        if any(m["case_id"] == case_id and m["seq"] == seq for m in self.messages):
            return False
        self.messages.append({"case_id": case_id, "seq": seq, "role": role, "content": content,
                              "tool_calls": tool_calls, "guardrail_event": guardrail_event})
        return True

    async def list_messages(self, case_id, limit=None):
        rows = [m for m in self.messages if m["case_id"] == case_id]
        return rows[:limit] if limit else rows

    async def upsert_slot(self, case_id, slot, value, confidence=None, source_seq=None):
        self.slots.append({"case_id": case_id, "slot": slot, "value": value, "source_seq": source_seq})
        return True

    async def list_slots(self, case_id):
        return [s for s in self.slots if s["case_id"] == case_id]

    async def record_guardrail(self, case_id, event_type, detail=None):
        self.guardrails.append({"case_id": case_id, "type": event_type, "detail": detail})
        return True

    async def list_guardrails(self, case_id, limit=50):
        return [g for g in self.guardrails if g["case_id"] == case_id][:limit]

    async def count_guardrail_since(self, event_type, since):
        return len([g for g in self.guardrails if g["type"] == event_type])

    async def insert_usage(self, case_id, seq, model, input_tokens, output_tokens, amount):
        self.usages.append({"case_id": case_id, "amount": amount, "created_at": datetime.now()})
        return True

    async def sum_usage(self, since=None):
        rows = [u for u in self.usages if since is None or u["created_at"] >= since]
        return {"amount": sum(u["amount"] for u in rows), "tokens": 0, "calls": len(rows)}


_ORIG = {
    "uri": settings.mysql_uri,
    "max_rounds": settings.max_rounds,
    "max_tools": settings.max_tools_per_round,
    "price_in": settings.price_input_per_1k,
    "price_out": settings.price_output_per_1k,
    "quota_expire": settings.free_quota_expire_date,
    "quota_baseline": settings.free_quota_used_baseline,
    "chat_enabled": settings.chat_enabled,
    "llm_enabled": settings.llm_enabled,
}


def restore() -> None:
    settings.mysql_uri = _ORIG["uri"]
    settings.max_rounds = _ORIG["max_rounds"]
    settings.max_tools_per_round = _ORIG["max_tools"]
    settings.price_input_per_1k = _ORIG["price_in"]
    settings.price_output_per_1k = _ORIG["price_out"]
    settings.free_quota_expire_date = _ORIG["quota_expire"]
    settings.free_quota_used_baseline = _ORIG["quota_baseline"]
    settings.chat_enabled = _ORIG["chat_enabled"]
    settings.llm_enabled = _ORIG["llm_enabled"]


def wire(fake: FakeChatRepo) -> FakeChatRepo:
    """把假仓储接到全部持有模块级引用的模块。

    ⚠️ **离线保证**：这里强制把 `llm_enabled` 置 False。否则 `model=None` 的用例
    会经由 `llm_adapter.get_chat_model()` 构造出**真实模型**并发出计费调用——
    自动化测试必须零计费，模型调用的验证由使用者自行运行。
    """
    settings.mysql_uri = "mysql+pymysql://fake:fake@127.0.0.1:3306/fake"
    settings.price_input_per_1k = 0.0
    settings.price_output_per_1k = 0.0
    settings.free_quota_expire_date = ""
    settings.free_quota_used_baseline = 0
    settings.llm_enabled = False
    session.repo = fake
    tools.repo = fake
    orchestrator.repo = fake
    cost_gate.chat_repo = fake
    cost_gate.reset_fallback()
    return fake


async def fake_decision_runner(order_id, request):
    """桩判定 runner：不连判定图，返回固定结论（供离线验证链路）。"""
    return (
        {
            "conclusion": "auto_pass",
            "hit_rules": ["R2"],
            "hit_details": ["申请金额低于小额线，符合自动通过条件"],
            "risk_level": "low",
            "status": "completed",
        },
        False,
    )


def make_ctx(case_id: str = "O1", **kwargs) -> tools.ToolContext:
    return tools.ToolContext(case_id=case_id, order_id=case_id, last_seq=1, **kwargs)


HISTORY = [{"role": "system", "content": "sys"}, {"role": "user", "content": "包裹破了"}]


async def amain() -> int:
    fake = wire(FakeChatRepo())
    await fake.create_case("O1", "O1", None)
    await fake.update_case("O1", status=state.CLARIFYING, last_seq=1)
    seed_order("O1", item_category="日用百货", pay_amount=39.0, logistics_status="signed")
    seed_logistics("O1", logistics_status="signed", traces=["已揽收", "已签收"])

    # ---------- A1 正常闭环 ----------
    model = ScriptedChatModel([
        turn_tool(tc("query_order")),
        turn_tool(tc("query_logistics")),
        turn_tool(tc("submit_for_decision", issue_reason="未收到货", issue_object="物流", claim_direction="仅退款")),
        turn_text("好的，已为你处理。"),
    ])
    final = await agent.run_agent(model=model, ctx=make_ctx(decision_runner=fake_decision_runner),
                                  history=HISTORY, persist_usage=False)
    check("A1a 工具被按模型意愿调用 3 次", final["tools_used"] == 3, f"实得 {final['tools_used']}")
    check("A1b 三次工具均成功", all(a["ok"] for a in final["audits"]), f"{final['audits']}")
    check("A1c 产出结论", bool(final["decision"]) and final["decision"].get("conclusion") == "auto_pass")
    check("A1d 模型收尾文本保留", final["draft"] == "好的，已为你处理。")
    check("A1e 正常结束", final["stop_reason"] == "", f"实得 {final['stop_reason']!r}")
    check("A1f 每次调用都带工具规格", all(s == TOOL_SPECS for s in model.seen_tool_specs))
    check("A1g 上下文随工具结果增长", model.seen_message_counts[-1] > model.seen_message_counts[0])

    # ---------- A2 缺必填 → 提交被拒 → 追问 ----------
    model = ScriptedChatModel([
        turn_tool(tc("submit_for_decision", issue_reason="未收到货")),
        turn_text("请问问题出在哪里？"),
    ])
    final = await agent.run_agent(model=model, ctx=make_ctx(decision_runner=fake_decision_runner),
                                  history=HISTORY, persist_usage=False)
    check("A2a 提交被拒（缺必填）",
          final["audits"][0]["ok"] is False and final["audits"][0]["error"] == "slots_missing",
          f"{final['audits'][0]}")
    check("A2b 未产出结论", final["decision"] is None)
    check("A2c 模型追问文本返回", final["draft"] == "请问问题出在哪里？")

    # ---------- A3 直接回复 ----------
    model = ScriptedChatModel([turn_text("你好，请描述遇到的问题。")])
    final = await agent.run_agent(model=model, ctx=make_ctx(), history=HISTORY, persist_usage=False)
    check("A3 无工具调用也可收尾", final["tools_used"] == 0 and final["stop_reason"] == "")

    # ---------- A4 取数失败不编造 ----------
    model = ScriptedChatModel([turn_tool(tc("query_order")), turn_text("我没查到订单信息。")])
    ctx = make_ctx(case_id="O_MISSING")
    ctx.order_id = "O_MISSING"
    final = await agent.run_agent(model=model, ctx=ctx, history=HISTORY, persist_usage=False)
    check("A4 取不到订单 → 工具仍成功但无数据", final["audits"][0]["ok"] is True)

    # ---------- A5 工具预算耗尽 ----------
    model = ScriptedChatModel([turn_tool(tc("query_order")), turn_tool(tc("query_logistics")),
                               turn_tool(tc("query_case_progress"))])
    final = await agent.run_agent(model=model, ctx=make_ctx(), history=HISTORY, tool_budget=2,
                                  persist_usage=False)
    check("A5a 工具用量被限在预算内", final["tools_used"] == 2, f"实得 {final['tools_used']}")
    check("A5b 预算耗尽即收口", final["stop_reason"] == "budget", f"实得 {final['stop_reason']!r}")

    # ---------- A6 模型自主转人工 ----------
    model = ScriptedChatModel([turn_tool(tc("escalate_to_human", reason_code="buyer_request"))])
    final = await agent.run_agent(model=model, ctx=make_ctx(), history=HISTORY, persist_usage=False)
    check("A6a 标记已转人工", final["escalated"] is True)
    check("A6b 会话进入终态 ESCALATED", (await fake.get_case("O1"))["status"] == state.ESCALATED)

    # ---------- A7 成本闸门暂停 ----------
    _orig_precheck = cost_gate.precheck

    async def _paused_precheck(*, ongoing=False):
        return cost_gate.GateStatus(
            paused=True, reason="cost_warning", message="暂停",
            month_amount=0.2, month_tokens=1000, month_calls=1, total_tokens=1000,
            free_quota_tokens=1_000_000, price_configured=False,
        )

    cost_gate.precheck = _paused_precheck  # type: ignore[assignment]
    model = ScriptedChatModel([turn_text("不应被调用")])
    final = await agent.run_agent(model=model, ctx=make_ctx(), history=HISTORY, persist_usage=False)
    check("A7a 闸门暂停 → 停止循环", final["stop_reason"] == "gate_paused", f"实得 {final['stop_reason']!r}")
    check("A7b 模型未被调用", len(model.seen_message_counts) == 0)
    cost_gate.precheck = _orig_precheck  # type: ignore[assignment]

    # ---------- A8 模型不可用 ----------
    final = await agent.run_agent(model=None, ctx=make_ctx(), history=HISTORY, persist_usage=False)
    check("A8 模型不可用 → 降级收口", final["stop_reason"] == "model_unavailable")

    # ---------- A9 未授权工具 ----------
    model = ScriptedChatModel([turn_tool(tc("refund", amount=99999)), turn_text("好的")])
    final = await agent.run_agent(model=model, ctx=make_ctx(), history=HISTORY, persist_usage=False)
    check("A9 未授权工具被拒绝",
          final["audits"][0]["ok"] is False and final["audits"][0]["error"] == "tool_not_allowed",
          f"{final['audits'][0]}")

    # ---------- A10 编排端到端 ----------
    fake2 = wire(FakeChatRepo())
    await fake2.create_case("O2", "O2", None)
    await fake2.update_case("O2", status=state.CLARIFYING, last_seq=1)
    await fake2.append_message("O2", 1, "agent", "你好")
    seed_order("O2", item_category="日用百货", pay_amount=39.0, logistics_status="signed")
    seed_logistics("O2", logistics_status="signed", traces=["已签收"])

    _orig_runner = tools._default_decision_runner
    tools._default_decision_runner = fake_decision_runner  # type: ignore[assignment]
    model = ScriptedChatModel([
        turn_tool(tc("query_order")),
        turn_tool(tc("submit_for_decision", issue_reason="未收到货", issue_object="物流", claim_direction="仅退款")),
        turn_text("（模型话术不应覆盖结论）"),
    ])
    result = await orchestrator.handle_buyer_message("O2", "我没收到货", model=model, persist_usage=False)
    check("A10a 消息被接收", result["accepted"] is True)
    check("A10b 类型为结论", result["kind"] == "decision", f"实得 {result['kind']}")
    check("A10c 回复由确定性模板渲染", "符合处理条件" in result["reply"], f"回复：{result['reply'][:40]}")
    check("A10d 模型话术未覆盖结论", "不应覆盖" not in result["reply"])
    check("A10e 状态推进到 DECIDED", result["status"] == state.DECIDED, f"实得 {result['status']}")
    check("A10f 工具审计已落库", any(m.get("tool_calls") for m in fake2.messages))
    check("A10g 槽位带来源轮次", any(s.get("source_seq") is not None for s in fake2.slots))
    tools._default_decision_runner = _orig_runner  # type: ignore[assignment]

    # ---------- A11 编排降级 ----------
    fake3 = wire(FakeChatRepo())
    await fake3.create_case("O3", "O3", None)
    await fake3.update_case("O3", status=state.CLARIFYING, last_seq=1)
    result = await orchestrator.handle_buyer_message("O3", "在吗", model=None, persist_usage=False)
    check("A11a 模型不可用 → 类型 model_unavailable", result["kind"] == "model_unavailable",
          f"实得 {result['kind']}")
    check("A11b 已转人工", (await fake3.get_case("O3"))["status"] == state.ESCALATED)

    # ---------- A12 接管守卫（人工接管后智能体不得再自动应答） ----------
    fake4 = wire(FakeChatRepo())
    await fake4.create_case("O4", "O4", None)
    await fake4.update_case("O4", status=state.CLARIFYING, last_seq=1,
                            workflow_status="escalated", assignee=state.ASSIGNEE_MANUAL)
    seed_order("O4", item_category="日用百货", pay_amount=39.0, logistics_status="signed")
    guard_model = ScriptedChatModel([turn_text("智能体不应说话")])
    result = await orchestrator.handle_buyer_message("O4", "在吗", model=guard_model, persist_usage=False)
    check("A12a 接管后类型为 human_takeover", result["kind"] == "human_takeover", f"实得 {result['kind']}")
    check("A12b 智能体未被调用", len(guard_model.seen_message_counts) == 0)
    check("A12c 不产生智能体回复", result["reply"] == ""
          and not any(m["role"] == "agent" for m in fake4.messages))
    check("A12d 买家消息仍落库（人工必须看得到）",
          any(m["role"] == "buyer" for m in fake4.messages))
    check("A12e 接管提示只给界面、不落库",
          bool(result["notice"]) and not any(m.get("content") == result["notice"] for m in fake4.messages))
    check("A12f 留有接管守卫审计",
          any(g["type"] == "human_takeover" for g in fake4.guardrails))

    # ---------- A13 人工发言进入模型上下文 ----------
    history = orchestrator.build_history("O5", [
        {"role": "buyer", "content": "我要退货"},
        {"role": "human", "content": "我是人工客服，正在为你处理"},
    ])
    check("A13a 人工发言映射为 assistant", history[-1]["role"] == "assistant",
          f"实得 {[h['role'] for h in history]}")
    check("A13b 人工发言带前缀标记", history[-1]["content"].startswith(orchestrator.HUMAN_PREFIX))
    check("A13c 买家仍映射为 user", history[-2]["role"] == "user")

    # ---------- A14 诉求方向接线：补发必须可达 ----------
    fake5 = wire(FakeChatRepo())
    await fake5.create_case("O6", "O6", None)
    await fake5.update_case("O6", status=state.CLARIFYING, last_seq=1)
    await fake5.append_message("O6", 1, "agent", "你好")
    seed_order("O6", item_category="日用百货", pay_amount=39.0, logistics_status="signed")
    seed_logistics("O6", logistics_status="signed", traces=["已签收"])
    tools._default_decision_runner = fake_decision_runner  # type: ignore[assignment]
    reship_model = ScriptedChatModel([
        turn_tool(tc("submit_for_decision", issue_reason="错发漏发",
                     issue_object="商品本身", claim_direction="补发")),
        turn_text("已为你提交补发申请。"),
    ])
    result = await orchestrator.handle_buyer_message("O6", "发错货了，我要补发",
                                                    model=reship_model, persist_usage=False)
    check("A14a 结论已产出", result["kind"] == "decision", f"实得 {result['kind']}")
    case6 = await fake5.get_case("O6") or {}
    check("A14b 处置结果为 reship（补发可达）", case6.get("decision_result") == "reship",
          f"实得 {case6.get('decision_result')}")
    tools._default_decision_runner = _orig_runner  # type: ignore[assignment]

    # ---------- A15 人工接管时，会话终态不得把买家挡在门外 ----------
    fake6 = wire(FakeChatRepo())
    await fake6.create_case("O7", "O7", None)
    await fake6.update_case("O7", status=state.ESCALATED, last_seq=1,
                            workflow_status="escalated", assignee=state.ASSIGNEE_MANUAL)
    result = await orchestrator.handle_buyer_message("O7", "人工在吗", model=None, persist_usage=False)
    check("A15a 终态 + 人工接管 → 消息仍被接收",
          result["kind"] == "human_takeover", f"实得 {result['kind']}")
    check("A15b 买家消息落库（人要看得到）",
          any(m["role"] == "buyer" and m.get("content") == "人工在吗" for m in fake6.messages))
    check("A15c 人工接管不消耗轮次",
          int((await fake6.get_case("O7")).get("round") or 0) == 0)

    fake7 = wire(FakeChatRepo())
    await fake7.create_case("O8", "O8", None)
    await fake7.update_case("O8", status=state.ESCALATED, last_seq=1)
    result = await orchestrator.handle_buyer_message("O8", "在吗", model=None, persist_usage=False)
    check("A15d 无人接管时终态仍拒收（守卫未被放松）",
          result["kind"] == "guardrail" and result.get("code") == "closed",
          f"实得 {result.get('kind')}/{result.get('code')}")

    restore()
    print("-" * 68)
    print(f"结果：{'全部通过' if not failures else '存在失败'}")
    if failures:
        for item in failures:
            print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
