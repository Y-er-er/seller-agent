"""AI 助手测试 —— **假仓储 + 假商品服务，零外部依赖**。

覆盖重点是那条**最不能出事的不变量**：
**破坏性动作在卖家确认之前，绝不被执行。**

  A 组 工具注册表：数量 / 破坏性标记 / 能力分组 / 注册期校验 / 文案渲染 / 上下文注入
  B 组 待确认机制：提出→未执行 / 取消→零改动 / 确认→才执行 / 不可重复决定 / 新对话作废未决
  C 组 对话循环：模型不可用时的降级、未知工具拒绝、破坏性工具转待确认（不执行）、
      非破坏性工具正常执行、步数收口

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_assistant.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import actions, loop, registry, tools_goods  # noqa: E402
from app.assistant.registry import AssistantContext, AssistantError, ToolSpec, ToolResult  # noqa: E402

S = "test-seller"
THREAD = "test-thread"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


# ============================================================
# 假仓储 / 假商品服务
# ============================================================


class FakeAssistantRepo:
    ACTION_PENDING = "pending"
    ACTION_CONFIRMED = "confirmed"
    ACTION_CANCELLED = "cancelled"
    ACTION_FAILED = "failed"      # 与真实仓储保持一致（执行失败 ≠ 已完成）
    ACTION_RUNNING = "running"    # 抢占标记（确认动作 pending → running → 终态）

    def __init__(self) -> None:
        self.actions: dict[int, dict] = {}
        self.messages: list[dict] = []
        self._aid = 0
        self._mid = 0

    async def create_action(self, thread_id, tool_name, arguments, summary, detail="",
                            seller_id=S, batch_id=""):
        self._aid += 1
        self.actions[self._aid] = {
            "id": self._aid, "thread_id": thread_id, "tool_name": tool_name,
            "arguments": arguments, "summary": summary, "detail": detail,
            "status": self.ACTION_PENDING, "result": None, "seller_id": seller_id,
            "batch_id": batch_id,
        }
        return self._aid

    async def get_action(self, action_id, seller_id):        # ⚠️ seller_id 必传
        a = self.actions.get(action_id)
        return dict(a) if a and a["seller_id"] == seller_id else None

    async def list_actions(self, thread_id, seller_id, status="", limit=50):
        return [dict(a) for a in self.actions.values()
                if a["thread_id"] == thread_id and a["seller_id"] == seller_id
                and (not status or a["status"] == status)]

    async def find_pending(self, thread_id, tool_name, arguments, seller_id):
        """同工具 + 同参数的未决项（幂等键）。"""
        for a in self.actions.values():
            if (a["thread_id"] == thread_id and a["seller_id"] == seller_id
                    and a["tool_name"] == tool_name
                    and a["status"] == self.ACTION_PENDING
                    and a["arguments"] == arguments):
                return dict(a)
        return None

    async def expire_stale(self, hours=24):
        """真实实现会把超时项标 expired；假仓储里视作已处理，返回 0。"""
        return 0

    async def find_recent_done(self, thread_id, tool_name, arguments, seller_id, minutes=30):
        """假仓储不模拟时间窗：同参数的已确认动作都算"近期"。"""
        for a in self.actions.values():
            if (a["thread_id"] == thread_id and a["seller_id"] == seller_id
                    and a["tool_name"] == tool_name
                    and a["status"] == self.ACTION_CONFIRMED
                    and a["arguments"] == arguments):
                return dict(a)
        return None

    async def decide_action(self, action_id, status, result, seller_id):   # ⚠️ seller_id 必传
        a = self.actions.get(action_id)
        if not a or a["seller_id"] != seller_id or a["status"] != self.ACTION_PENDING:
            return False
        a["status"], a["result"] = status, result
        a["decided_at"] = datetime.now()      # "近期已完成"的判断依赖它
        return True

    async def claim_action(self, action_id, seller_id):
        """CAS 抢占：只有 pending 能变 running。第二次抢同一张会失败。"""
        a = self.actions.get(action_id)
        if not a or a["seller_id"] != seller_id or a["status"] != self.ACTION_PENDING:
            return False
        a["status"] = self.ACTION_RUNNING
        a["decided_at"] = datetime.now()
        return True

    async def finish_action(self, action_id, seller_id, status, result):
        a = self.actions.get(action_id)
        if not a or a["seller_id"] != seller_id or a["status"] != self.ACTION_RUNNING:
            return False
        a["status"], a["result"] = status, result
        a["decided_at"] = datetime.now()
        return True

    async def release_action(self, action_id, seller_id, reason):
        a = self.actions.get(action_id)
        if not a or a["seller_id"] != seller_id or a["status"] != self.ACTION_RUNNING:
            return False
        a["status"], a["result"] = self.ACTION_PENDING, reason
        return True

    async def list_batch(self, batch_id, seller_id=S, status="pending"):
        return [dict(a) for a in self.actions.values()
                if a.get("batch_id") == batch_id and a["seller_id"] == seller_id
                and (not status or a["status"] == status)]

    async def append_message(self, thread_id, role, content="", **kw):
        self._mid += 1
        self.messages.append({"id": self._mid, "thread_id": thread_id, "seq": self._mid,
                              "role": role, "content": content})
        return self.messages[-1]

    async def list_messages(self, thread_id, limit=200):
        # 与真实仓储一致：给 limit 时取**最近** limit 条（`[-limit:]`），不是最早的
        return [m for m in self.messages if m["thread_id"] == thread_id][-limit:]


class FakeGoods:
    """够 `impact_builder` 与破坏性工具用的极简版。"""

    def __init__(self, goods: dict[str, dict]) -> None:
        self.goods = goods
        self.deleted: list[str] = []

    async def find_by_no(self, seller_id, goods_no):
        g = self.goods.get(str(goods_no))
        return dict(g) if g else None

    async def delete_goods(self, seller_id, goods_id, **kw):
        for no, g in list(self.goods.items()):
            if g["id"] == goods_id:
                self.goods.pop(no)
                self.deleted.append(no)
                return {"goods_id": goods_id, "media_removed": len(g.get("media") or [])}
        raise AssertionError("商品不存在")


class FakeCostGate:
    """假成本闸门：测试不碰 `llm_usage` 表。"""

    async def precheck(self, ongoing: bool = True):
        return types.SimpleNamespace(paused=False, message="", reason="")

    async def record_usage(self, *args, **kwargs) -> None:
        return None


class StubModel:
    """脚本化桩：按预设脚本依次返回，**零计费**。

    `seen` 记录每次调用时模型实际收到的消息 —— 用来断言
    「卖家这一句到底进没进模型输入」（F 组）。
    """
    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def step(self, messages, tools):
        from app.chat.llm_adapter import ModelTurn, ToolCall
        self.seen.append([dict(m) for m in messages])
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        calls = [ToolCall(name=c["name"], arguments=c.get("arguments", {}), id=f"c{self.calls}")
                 for c in item.get("tool_calls", [])]
        # ⚠️ ModelTurn 的字段是 `usage`（input_tokens 是派生属性），传错会在循环里变成 model_error
        return ModelTurn(content=item.get("content", ""), tool_calls=calls,
                         usage={"input_tokens": 10, "output_tokens": 10})


# ============================================================
# 用例
# ============================================================


def group_a() -> None:
    print("【A 组】工具注册表")
    loop.ensure_tools_registered()
    loop.ensure_tools_registered()          # 幂等
    specs = registry.all_specs()
    check("A1 工具注册（幂等）", len(specs) == 34, f"{len(specs)} 个")
    check("A2 破坏性工具标记正确",
          {s.name for s in specs if s.destructive} ==
          {"delete_goods", "bulk_apply", "resume_aftersale_case", "close_aftersale_case",
           "undo_log", "delete_knowledge_doc"},
          str([s.name for s in specs if s.destructive]))
    # 计费工具（阶段五补）：能生成图/文案/定价 —— 此前只有端点、没进注册表，
    # 导致助手只能说"系统不支持直接生成图片"
    check("A2b 计费工具已注册（生成图/文案/定价 / 重试生图）",
          {s.name for s in specs if s.billable} ==
          {"generate_cover", "generate_copy", "suggest_price", "retry_task"},
          str([s.name for s in specs if s.billable]))
    check("A2c 需确认 = 不可逆 ∪ 计费",
          {s.name for s in specs if s.needs_confirm} ==
          {s.name for s in specs if s.destructive or s.billable})
    check("A2d 计费工具的确认卡理由标记为 billable",
          registry.get("generate_cover").confirm_kind == "billable"
          and registry.get("delete_goods").confirm_kind == "destructive")
    check("A3 能力清单按域分组",
          {g["domain"] for g in registry.capabilities()} ==
          {"goods", "display", "aftersale", "ops", "knowledge", "orders", "service"},
          str([g["domain"] for g in registry.capabilities()]))
    check("A4 模型规格为 OpenAI 格式",
          all(s["type"] == "function" and "parameters" in s["function"] for s in registry.model_specs()))
    check("A5 未知工具返回 None", registry.get("nope") is None)

    try:
        registry.register(ToolSpec(name="bad", domain="goods", description="x",
                                   parameters={"type": "object", "properties": {}},
                                   handler=_noop, destructive=True))
        check("A6 破坏性工具缺说明被注册期拒绝", False, "竟然通过")
    except ValueError:
        check("A6 破坏性工具缺说明被注册期拒绝", True)
    try:
        registry.register(ToolSpec(name="list_goods", domain="goods", description="x",
                                   parameters={"type": "object", "properties": {}}, handler=_noop))
        check("A7 重名工具被拒", False, "竟然通过")
    except ValueError:
        check("A7 重名工具被拒", True)

    check("A8 单件文案渲染（含回收站语义）",
          registry.get("delete_goods").describe_action({"goods_no": "G-0001"})
          == "删除商品 G-0001（移入回收站）",
          registry.get("delete_goods").describe_action({"goods_no": "G-0001"}))
    check("A9 批量文案做值映射",
          registry.get("bulk_apply").describe_action(
              {"goods_nos": ["a", "b"], "action": "price_delta"}) == "批量调价（2 件）")
    prompt = loop.build_system_prompt(
        AssistantContext(S, THREAD, view="admin", selected_goods_no="G-0001"))
    check("A10 上下文注入 system prompt", "商品管理" in prompt and "G-0001" in prompt)
    check("A11 无上下文时不注入该段",
          "当前界面上下文" not in loop.build_system_prompt(AssistantContext(S, THREAD)))

    # --- 防"答过期数据"（实测踩到：卖家改了商品后，助手把自己上一轮的旧结论抄了一遍）---
    prompt_rules = loop.build_system_prompt(AssistantContext(S, THREAD))
    check("A12 system prompt 含「涉及当前状态必须实时查询」硬规则",
          "必须" in prompt_rules and "实时查询" in prompt_rules and "过期" in prompt_rules,
          prompt_rules[:220])

    hist = loop._to_model_messages([
        {"role": "seller", "content": "我有哪些商品"},
        {"role": "assistant", "content": "你有 3 件商品…", "tool_name": "list_goods"},
        {"role": "tool", "content": "共 3 件…"},
        {"role": "assistant", "content": "好的"},
    ])
    check("A13 跳过 role=tool 的行", len(hist) == 3, str(len(hist)))
    check("A14 由工具结果写回的历史消息被打上「可能已过期」标记",
          "可能已过期" in hist[1]["content"], hist[1]["content"][:90])
    check("A15 普通助手消息不加过期标记",
          "可能已过期" not in hist[2]["content"], hist[2]["content"][:60])

    # --- 让"生成并写入"一步做完（实测踩到：生成完候选就停住了，卖家还得再说一遍）---
    # 实现已从「生成后立即调用 update_goods 写回」（模型续跑）改为 generate_copy(apply=true) 一步原子写入
    check("A16 system prompt 要求「写文案一步完成（apply=true 直接写入）」",
          "apply=true" in prompt_rules and "直接写入商品详情" in prompt_rules, prompt_rules[-220:])
    # A17 已移除：原先靠「工具描述」引导"生成后继续写入"，但实测发现这会让模型
    # 误以为可以跳过生成直接写；引导已移到 `continue_after_action` 的系统通知里，
    # 由 C8—C10 覆盖验证。工具描述现在只说清"生成候选、需确认"。
    check("A17 generate_copy 描述不再夹带后续指令（避免干扰模型判断）",
          "update_goods" not in registry.get("generate_copy").description,
          registry.get("generate_copy").description)
    check("A18 system prompt 要求「多件事一次做完」",
          "要把它们做完" in prompt_rules, prompt_rules[:260])

    # --- 防"只说不做"（实测踩到：模型说"已弹出确认提示"，实际没调工具、没建卡）---
    check("A19 prompt 明确要求「动作类请求必须先调用工具」",
          "必须先调用" in prompt_rules and "不调用工具就什么都不会发生" in prompt_rules,
          prompt_rules[:400])
    check("A20 prompt 禁止模型自称「已弹出确认提示」",
          "不要自己说" in prompt_rules and "已弹出确认提示" in prompt_rules,
          prompt_rules[:400])

    # --- 失败策略（B3）：只读可重试，有副作用绝不重试 ---
    check("A21 只读工具可重试、写/计费工具不重试",
          registry.get("list_goods").failure_policy == "retryable"
          and registry.get("get_goods_aftersale").failure_policy == "retryable"
          and registry.get("update_goods").failure_policy == "fail_fast"
          and registry.get("generate_cover").failure_policy == "fail_fast",
          str({s.name: s.failure_policy for s in specs if s.name in
               ("list_goods", "update_goods", "generate_cover")}))
    check("A22 有副作用的工具不会被标成可重试",
          all(s.failure_policy != "retryable" for s in specs if s.needs_confirm))
    try:
        registry.register(ToolSpec(
            name="bad_retry", domain="goods", description="x",
            parameters={"type": "object", "properties": {}},
            handler=_noop, destructive=True, summary_template="删除 {goods_no}",
            policy="retryable",
        ))
        check("A23 破坏性工具被标 retryable 会被注册期拒绝", False, "竟然注册成功")
    except ValueError:
        check("A23 破坏性工具被标 retryable 会被注册期拒绝", True)

    # --- 内部错误不念给卖家（实测：模型把 TypeError 原文念出来了）---
    check("A24 prompt 要求「内部错误只说人话、不念技术报错」",
          "不要念技术报错" in prompt_rules and "TypeError" in prompt_rules,
          prompt_rules[:600])


async def _noop(**kwargs) -> ToolResult:
    return ToolResult("ok")


async def group_b() -> None:
    print("\n【B 组】待确认机制")
    fake_repo = FakeAssistantRepo()
    fake_goods = FakeGoods({"G-0001": {"id": 1, "goods_no": "G-0001", "title": "测试品",
                                       "price": 88.0, "media": [{"id": 1}, {"id": 2}]}})
    actions.repo = fake_repo
    tools_goods.service = fake_goods
    ctx = AssistantContext(S, THREAD)
    spec = registry.get("delete_goods")

    card = await actions.create_pending(ctx, spec, {"goods_no": "G-0001"})
    check("B1 提出后**未执行**（商品仍在）", "G-0001" in fake_goods.goods,
          "商品已被删除 —— 破坏性动作被执行了！")
    check("B2 确认卡含影响明细", "无法恢复" in card["detail"] and "2 张图片" in card["detail"],
          card["detail"])
    check("B3 确认卡主文案是人话（删除 → 移入回收站）",
          card["summary"] == "删除商品 G-0001（移入回收站）", card["summary"])

    await actions.cancel(ctx, card["id"])
    check("B4 取消后数据零改动",
          "G-0001" in fake_goods.goods and not fake_goods.deleted, str(fake_goods.deleted))
    try:
        await actions.cancel(ctx, card["id"])
        check("B5 已决定的动作不可重复取消", False, "竟然通过")
    except AssistantError as exc:
        check("B5 已决定的动作不可重复取消", exc.code == "action_decided", exc.code)

    card2 = await actions.create_pending(ctx, spec, {"goods_no": "G-0001"})
    check("B6 再次提出后仍未执行", "G-0001" in fake_goods.goods)
    result = await actions.confirm(ctx, card2["id"])
    check("B7 确认后**才**真正执行", "G-0001" not in fake_goods.goods and result["ok"])
    check("B8 结果写回对话", any("已删除" in m["content"] for m in fake_repo.messages))

    try:
        await actions.confirm(ctx, card2["id"])
        check("B9 已确认的动作不可重复确认", False, "竟然通过")
    except AssistantError as exc:
        check("B9 已确认的动作不可重复确认", exc.code == "action_decided", exc.code)

    check("B10 待确认列表只含未决项", not await actions.list_pending(ctx),
          str(await actions.list_pending(ctx)))

    # --- 幂等：同工具同参数不重复建卡（实测踩到：一次生成文案点了 3 次 → 计费 3 次）---
    # 用独立编号：B1—B10 已经 confirm 过 G-0001 的删除，会命中"近期已完成"规则
    c1 = await actions.create_pending(ctx, spec, {"goods_no": "G-9001"})
    c2 = await actions.create_pending(ctx, spec, {"goods_no": "G-9001"})
    check("B11 同工具同参数复用同一张卡（不重复建卡）",
          c1["id"] == c2["id"], f"{c1['id']} vs {c2['id']}")
    check("B12 复用时会标注 reused", c2.get("reused") is True, str(c2.get("reused")))
    c3 = await actions.create_pending(ctx, spec, {"goods_no": "G-9002"})
    check("B13 参数不同则新建一张", c3["id"] != c1["id"], f"{c3['id']} vs {c1['id']}")
    check("B14 重复建卡不会让未决数虚增",
          len(await actions.list_pending(ctx)) == 2, str(len(await actions.list_pending(ctx))))
    for item in (c1, c3):
        await actions.cancel(ctx, item["id"])

    # 过期项不该出现在待确认列表（否则入口角标会永远亮着）
    fake_repo.actions[999] = {
        "id": 999, "thread_id": THREAD, "seller_id": S, "tool_name": "x",
        "arguments": {}, "summary": "超时未处理的项", "detail": "",
        "status": "expired", "result": "超过 24 小时未处理，已自动作废",
    }
    check("B15 已过期 / 已取消的卡不出现在待确认列表",
          all(x["id"] != 999 for x in await actions.list_pending(ctx)))

    # 近期**成功**完成 → 不再建卡（防"续跑那轮重复提交"造出多余的卡）
    # 直接注入一条"已成功确认"的记录 —— 用 confirm 真执行的话，得先让工具成功；
    # 而假商品 G-9003 不存在（删除会失败 → 正确标 failed，反而不该命中）。
    fake_repo.actions[9003] = {
        "id": 9003, "thread_id": THREAD, "seller_id": S, "tool_name": "delete_goods",
        "arguments": {"goods_no": "G-9003"}, "status": fake_repo.ACTION_CONFIRMED,
        "result": "已删除 G-9003", "summary": "删除商品 G-9003（移入回收站）",
        "detail": "", "decided_at": datetime.now(),
    }
    count_before = len(fake_repo.actions)
    c6 = await actions.create_pending(ctx, spec, {"goods_no": "G-9003"})
    check("B16 近期**成功**完成的动作不再重复建卡",
          c6.get("already_done") is True and len(fake_repo.actions) == count_before,
          f"already_done={c6.get('already_done')} 卡数 {count_before}→{len(fake_repo.actions)}")
    check("B17 复用已完成结果时会带上执行结果",
          bool(c6.get("result")), str(c6.get("result"))[:80])

    # --- 幂等键按"身份"而不是"全参数"（实测踩到：同商品两张文案卡）---
    spec_copy = registry.get("generate_copy")
    k1 = spec_copy.identity_key({"goods_no": "G-0007"})
    k2 = spec_copy.identity_key({"goods_no": "G-0007", "style": "口语"})
    k3 = spec_copy.identity_key({"goods_no": "G-0008", "style": "口语"})
    check("B18 身份键忽略次要参数（style）", k1 == k2, f"{k1} vs {k2}")
    check("B19 身份键区分不同商品", k1 != k3)

    x1 = await actions.create_pending(ctx, spec_copy, {"goods_no": "G-9004"})
    x2 = await actions.create_pending(ctx, spec_copy, {"goods_no": "G-9004", "style": "专业"})
    check("B20 同商品不同 style 只建一张卡（不再冒出第二张）",
          x1["id"] == x2["id"], f"{x1['id']} vs {x2['id']}")
    await actions.cancel(ctx, x1["id"])

    # --- 执行失败的动作**不算"已完成"**（实测踩到：生图失败后再也试不了）---
    orig_handler = spec.handler

    async def boom(ctx_, **kw):
        raise RuntimeError("模拟执行失败")

    object.__setattr__(spec, "handler", boom)
    try:
        c9 = await actions.create_pending(ctx, spec, {"goods_no": "G-9801"})
        res9 = await actions.confirm(ctx, c9["id"])
        check("B21 执行失败的动作标为 failed（不标 confirmed）",
              res9["ok"] is False and fake_repo.actions[c9["id"]]["status"] == fake_repo.ACTION_FAILED,
              str(fake_repo.actions[c9["id"]]["status"]))
        c10 = await actions.create_pending(ctx, spec, {"goods_no": "G-9801"})
        check("B22 上次失败的，再请求要**重新建卡**（不能判成「刚做完」）",
              not c10.get("already_done") and c10["id"] != c9["id"],
              f"already_done={c10.get('already_done')} id={c10['id']} vs {c9['id']}")
    finally:
        object.__setattr__(spec, "handler", orig_handler)


async def group_c() -> None:
    print("\n【C 组】对话循环")
    fake_repo = FakeAssistantRepo()
    fake_goods = FakeGoods({"G-0002": {"id": 2, "goods_no": "G-0002", "title": "循环测试品",
                                       "price": 10.0, "media": []}})
    actions.repo = fake_repo
    loop.repo = fake_repo                # ⚠️ 循环里的消息落库也要走假仓储，否则测试写真库
    tools_goods.service = fake_goods
    loop.cost_gate = FakeCostGate()      # 成本闸门也换成假的：测试不碰 llm_usage 表
    ctx = AssistantContext(S, "c-thread")

    # C1 模型不可用 → 明确告知，不假装在思考
    original = loop.get_chat_model
    loop.get_chat_model = lambda: None
    try:
        out = await loop.run_turn(ctx, "你好")
        check("C1 模型不可用时明确告知", out["stop_reason"] == "model_unavailable"
              and "不可用" in out["reply"], str(out)[:100])
    finally:
        loop.get_chat_model = original

    # C2 未知工具 → 拒绝，不执行
    stub = StubModel([
        {"tool_calls": [{"name": "drop_database", "arguments": {}}]},
        {"content": "抱歉，我没法做这个。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = await loop.run_turn(ctx, "帮我删库")
        check("C2 未知工具被拒绝",
              out["trace"] and not out["trace"][0]["ok"] and "未知工具" in out["trace"][0]["summary"],
              str(out["trace"])[:120])
    finally:
        loop.get_chat_model = original

    # C3 破坏性工具 → 转待确认，**不执行**
    stub = StubModel([
        {"tool_calls": [{"name": "delete_goods", "arguments": {"goods_no": "G-0002"}}]},
        {"content": "这项操作需要你确认后才能执行。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = await loop.run_turn(ctx, "把 G-0002 删了")
        check("C3 破坏性工具转待确认且**未执行**",
              len(out["pending"]) == 1 and "G-0002" in fake_goods.goods,
              f"pending={len(out['pending'])} 商品还在={('G-0002' in fake_goods.goods)}")
        check("C4 助手回复未谎报完成",
              "确认" in out["reply"] and "已删除" not in out["reply"], out["reply"][:80])
    finally:
        loop.get_chat_model = original

    # C5 非破坏性工具 → 正常执行
    called: list[str] = []

    async def fake_list(ctx_, **kw):
        called.append("list_goods")
        return ToolResult("共 1 件商品：G-0002 · 循环测试品 · ¥10.00 · 已上架 · 库存 0",
                          {"count": 1})

    spec = registry.get("list_goods")
    original_handler = spec.handler
    object.__setattr__(spec, "handler", fake_list)   # frozen dataclass
    stub = StubModel([
        {"tool_calls": [{"name": "list_goods", "arguments": {}}]},
        {"content": "你现在有 1 件商品。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = await loop.run_turn(ctx, "我有哪些商品")
        check("C5 非破坏性工具正常执行", called == ["list_goods"] and out["tools_used"] == 1, str(out["tools_used"]))
    finally:
        object.__setattr__(spec, "handler", original_handler)
        loop.get_chat_model = original

    # C6 步数收口：模型一直要调工具也不会无限循环
    # 用一个**不存在的工具名**来制造"永远有 tool_calls"——这样整段不碰数据库
    stub = StubModel([{"tool_calls": [{"name": "no_such_tool", "arguments": {}}]}])
    loop.get_chat_model = lambda: stub
    try:
        out = await loop.run_turn(ctx, "循环测试")
        check("C6 步数收口（不无限循环）", out["stop_reason"] == "max_steps", out["stop_reason"])
    finally:
        loop.get_chat_model = original

    # C7 对话已落库（卖家问 + 助手答）
    check("C7 对话已落库", any(m["role"] == "seller" for m in fake_repo.messages)
          and any(m["role"] == "assistant" for m in fake_repo.messages))

    # --- C8 确认后**续跑对话**（实测踩到：确认完生成了文案，却不写进商品）---
    written: list[dict] = []

    async def fake_update(ctx_, **kw):
        written.append(kw)
        return ToolResult("已更新商品文案", {"ok": True})

    spec_update = registry.get("update_goods")
    orig_update_handler = spec_update.handler
    object.__setattr__(spec_update, "handler", fake_update)

    stub2 = StubModel([
        {"tool_calls": [{"name": "update_goods",
                         "arguments": {"goods_no": "G-0002", "description": "夏天出门就穿它"}}]},
        {"content": "文案已写入商品。"},
    ])
    loop.get_chat_model = lambda: stub2
    try:
        follow = await loop.continue_after_action(ctx, "generate_copy", "生成了文案候选：夏天出门就穿它")
        check("C8 确认后续跑会驱动模型继续操作（把文案写进商品）",
              bool(written) and written[0].get("description") == "夏天出门就穿它",
              str(written))
        check("C9 续跑会产出给卖家的回复", bool(follow.get("reply")), str(follow.get("reply"))[:60])
        check("C10 续跑不产生假的卖家消息（internal 模式）",
              sum(1 for m in fake_repo.messages if m["role"] == "seller"
                  and "系统通知" in (m.get("content") or "")) == 0)
    finally:
        object.__setattr__(spec_update, "handler", orig_update_handler)
        loop.get_chat_model = original

    # --- C11 并发保护：同线程只允许一轮在跑（实测踩到上下文互相污染 + 重复计费）---
    lock = loop._thread_lock(ctx.thread_id)
    await lock.acquire()          # 模拟"另一轮正在跑"
    try:
        busy = await loop.run_turn(ctx, "并发测试")
        check("C11 线程忙碌时直接拒绝（不排队干等）", busy.get("stop_reason") == "busy", str(busy)[:90])
        check("C12 被拒时给出明确提示", "处理中" in str(busy.get("reply") or ""), busy.get("reply"))
        check("C13 被拒时不产出待确认项", not busy.get("pending"))
    finally:
        lock.release()

    # --- C14 同轮重复的有副作用调用被拦截（防重复执行 / 重复计费）---
    stub3 = StubModel([
        {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0002"}}]},
        {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0002"}}]},
        {"content": "好的，已提交。"},
    ])
    loop.get_chat_model = lambda: stub3
    try:
        out = await loop.run_turn(ctx, "给 G-0002 生成宣传图")
        check("C14 同轮重复调用只建 1 张卡（不重复计费）",
              len(out["pending"]) == 1, f"建了 {len(out['pending'])} 张")
        check("C15 trace 里标注了「重复调用已拦截」",
              any("重复调用" in (t.get("summary") or "") for t in out["trace"]),
              str(out["trace"])[:160])
        check("C14b 本轮已建卡时声称「已提交」**不**触发纠正轮（声称属实，放行）",
              stub3.calls == 3, f"模型被调了 {stub3.calls} 次")
    finally:
        loop.get_chat_model = original

    # --- C17 「嘴说-事实」守卫（实测两次复发：模型说"已弹出确认提示"却一次工具没调）---
    # 用独立线程：守卫会查 list_pending，别让 C3 遗留的卡把守卫短路掉
    ctx_guard = AssistantContext(S, "c-guard-thread")
    stub4 = StubModel([
        {"content": "已经为 G-0002 发起了文案生成请求，系统弹出了确认提示，您点一下确认就会开始生成。"},
        {"tool_calls": [{"name": "generate_copy", "arguments": {"goods_no": "G-0002"}}]},
        {"content": "文案生成会产生一点费用，已生成待确认项，请确认。"},
    ])
    loop.get_chat_model = lambda: stub4
    try:
        out = await loop.run_turn(ctx_guard, "给 G-0002 写宣传文案")
        check("C17 回复声称已发起但无卡 → 触发纠正轮并真正建卡",
              stub4.calls == 3 and len(out["pending"]) >= 1,
              f"模型被调 {stub4.calls} 次（应 3），pending={len(out['pending'])}")
        check("C18 纠正后的回复如实转达待确认（不再谎报已弹出）",
              "待确认" in out["reply"], out["reply"][:80])
    finally:
        loop.get_chat_model = original

    # --- C16 建档未指定类目：先查现有类目，再带着类目创建（工具链可组合）---
    calls: list[str] = []

    async def fake_list_cats(ctx_):
        calls.append("list_categories")
        return ToolResult("现有 2 个类目：厨房小电、数码配件", {"count": 2})

    async def fake_create(ctx_, **kw):
        calls.append("create_goods:" + str(kw.get("category")))
        return ToolResult("已新建商品草稿", {"goods_no": "G-9999"})

    spec_cats = registry.get("list_categories")
    spec_create = registry.get("create_goods")
    orig_cats, orig_create = spec_cats.handler, spec_create.handler
    object.__setattr__(spec_cats, "handler", fake_list_cats)
    object.__setattr__(spec_create, "handler", fake_create)

    stub4 = StubModel([
        {"tool_calls": [{"name": "list_categories", "arguments": {}}]},
        {"tool_calls": [{"name": "create_goods",
                         "arguments": {"title": "宠物饮水机", "category": "数码配件"}}]},
        {"content": "已归入「数码配件」。"},
    ])
    loop.get_chat_model = lambda: stub4
    try:
        await loop.run_turn(ctx, "建个新商品：宠物饮水机")
        check("C16 建档前先查类目、再带类目创建（工具链可组合）",
              calls == ["list_categories", "create_goods:数码配件"], str(calls))
    finally:
        object.__setattr__(spec_cats, "handler", orig_cats)
        object.__setattr__(spec_create, "handler", orig_create)
        loop.get_chat_model = original


async def group_d() -> None:
    """「一句话里有多件事」= 一张卡、一次确认。

    对应实测场景（2026-10-20）：

        「增加商品：电竞椅，定价1150，库存21，生成宣传图、文案，暂不上架」

    → 界面上冒出多张确认卡 → 只点了一张 → 模型说"文案已更新" →
    另一张卡还在（卖家读成"两件都做完了"）→ 任务中心没有生图任务、商品也没有图。
    """
    print("\n【D 组】多步骤 = 一次确认（批次）")
    fake_repo = FakeAssistantRepo()
    actions.repo = fake_repo
    loop.repo = fake_repo
    loop.cost_gate = FakeCostGate()
    ctx = AssistantContext(S, "d-thread")

    executed: list[str] = []

    async def fake_cover(ctx_, **kw):
        executed.append("cover:" + str(kw.get("goods_no")))
        return ToolResult("已把「测试品」的宣传图生成放到后台了（任务 #1，约 30—60 秒）。",
                          {"task_id": 1})

    async def fake_copy(ctx_, **kw):
        executed.append("copy:" + str(kw.get("goods_no")))
        return ToolResult("为「测试品」生成了文案：夏日必备款", {"copy": "夏日必备款"})

    spec_cover = registry.get("generate_cover")
    spec_copy = registry.get("generate_copy")
    orig_cover, orig_copy = spec_cover.handler, spec_copy.handler
    object.__setattr__(spec_cover, "handler", fake_cover)
    object.__setattr__(spec_copy, "handler", fake_copy)
    original_model = loop.get_chat_model

    try:
        # ---- D1—D5：一句话两步 → 一次确认 ----
        stub = StubModel([
            {"tool_calls": [
                {"name": "generate_copy", "arguments": {"goods_no": "G-3001"}},
                {"name": "generate_cover", "arguments": {"goods_no": "G-3001"}},
            ]},
            {"content": "共 2 步需要你在卡片上确认，点一次就都执行。"},
        ])
        loop.get_chat_model = lambda: stub

        out = await loop.run_turn(ctx, "生成宣传图、文案")
        batch_ids = {c.get("batch_id") for c in out["pending"]}
        check("D1 同一句话里的两步共享一个批次号",
              len(out["pending"]) == 2 and len(batch_ids) == 1 and "" not in batch_ids,
              f"pending={len(out['pending'])} batch_ids={batch_ids}")
        check("D2 建卡阶段一步都没执行（确认前零副作用）", executed == [], str(executed))
        check("D3 卡上带「点确认之后会发生什么」的说明",
              all(c.get("confirm_note") for c in out["pending"]),
              str([c.get("confirm_note") for c in out["pending"]]))

        bid = out["pending"][0]["batch_id"]
        res = await actions.confirm_batch(ctx, bid)
        check("D4 一次确认执行整批（2 步都做了）",
              res["ok_count"] == 2 and res["fail_count"] == 0 and len(executed) == 2,
              f"ok={res.get('ok_count')} executed={executed}")
        check("D5 执行完不留残余卡（不会再冒出第二张）",
              not await actions.list_pending(ctx), str(await actions.list_pending(ctx)))
        check("D6 **只续跑一次**对话（不是每步各跑一轮）", stub.calls == 3,
              f"模型被调 {stub.calls} 次（应为 3：建卡 2 次 + 续跑 1 次）")
        check("D7 结果汇总成一条消息写回对话",
              sum(1 for m in fake_repo.messages if "第 1 步（共 2 步）" in (m.get("content") or "")) == 1,
              str([m["content"][:30] for m in fake_repo.messages])[-160:])

        # ---- D8—D9：逐项跳过 ----
        ctx2 = AssistantContext(S, "d-skip")
        executed.clear()
        stub2 = StubModel([
            {"tool_calls": [
                {"name": "generate_copy", "arguments": {"goods_no": "G-3002"}},
                {"name": "generate_cover", "arguments": {"goods_no": "G-3002"}},
            ]},
            {"content": "两步都要确认。"},
        ])
        loop.get_chat_model = lambda: stub2
        out2 = await loop.run_turn(ctx2, "生成宣传图和文案")
        first_id = out2["pending"][0]["id"]
        res2 = await actions.confirm_batch(ctx2, out2["pending"][0]["batch_id"],
                                           skip_ids=[first_id])
        skipped = [r for r in res2["items"] if r.get("skipped")]
        check("D8 卡上划掉的项被跳过（不执行、标作废）",
              len(skipped) == 1 and fake_repo.actions[first_id]["status"] == "cancelled"
              and "copy:" not in "".join(executed),
              f"skipped={len(skipped)} executed={executed}")
        check("D9 其余项照常执行", "cover:G-3002" in executed, str(executed))

        # ---- D10—D11：连点两下只执行一次（抢占） ----
        ctx3 = AssistantContext(S, "d-race")
        executed.clear()
        gate = asyncio.Event()

        async def slow_cover(ctx_, **kw):
            executed.append("cover")
            await gate.wait()            # 制造真实并发窗口
            return ToolResult("生图任务已投递", {"task_id": 9})

        object.__setattr__(spec_cover, "handler", slow_cover)
        stub3 = StubModel([
            {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-3003"}}]},
            {"content": "请确认。"},
        ])
        loop.get_chat_model = lambda: stub3
        try:
            out3 = await loop.run_turn(ctx3, "给 G-3003 生图")
            bid3 = out3["pending"][0]["batch_id"]
            task_a = asyncio.create_task(actions.confirm_batch(ctx3, bid3))
            task_b = asyncio.create_task(actions.confirm_batch(ctx3, bid3))
            await asyncio.sleep(0.05)     # 让两个请求都跑起来
            gate.set()
            results = await asyncio.gather(task_a, task_b, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            check("D10 连点两下只执行一次（另一个被 409 挡下）",
                  executed == ["cover"] and len(errors) == 1,
                  f"executed={executed} errors={[type(e).__name__ for e in errors]}")
            check("D11 被挡下的那次不报成功",
                  all(getattr(e, "status_code", 0) == 409 for e in errors),
                  str([getattr(e, "status_code", None) for e in errors]))
        finally:
            object.__setattr__(spec_cover, "handler", fake_cover)

        # ---- D12：已处理的批次不能再执行 ----
        try:
            await actions.confirm_batch(ctx3, bid3)
            check("D12 已处理的批次再确认被拒", False, "竟然又执行了一次")
        except AssistantError as exc:
            check("D12 已处理的批次再确认被拒", exc.status_code == 409, exc.code)

        # ---- D13：点了其中一张，回复必须明说"还有几步在等确认" ----
        ctx4 = AssistantContext(S, "d-left")
        executed.clear()
        stub4 = StubModel([
            {"tool_calls": [
                {"name": "generate_copy", "arguments": {"goods_no": "G-3004"}},
                {"name": "generate_cover", "arguments": {"goods_no": "G-3004"}},
            ]},
            {"content": "两步需要确认。"},
            # 单卡确认 → 续跑轮：模型只说"文案已更新"，**没提**宣传图（图2 的真实回复）
            {"content": "文案已经帮您更新到商品详情里了。"},
        ])
        loop.get_chat_model = lambda: stub4
        out4 = await loop.run_turn(ctx4, "生成宣传图和文案")
        copy_card = next(c for c in out4["pending"] if c["tool_name"] == "generate_copy")
        one = await actions.confirm(ctx4, copy_card["id"])
        check("D13 只点一张时，系统在回复末尾补出「还有 1 步在等你确认」",
              "还有 1 步在等你确认" in str(one.get("follow_up", {}).get("reply") or ""),
              str(one.get("follow_up", {}).get("reply"))[-120:])

        # ---- D14：复用卡留在原批次（不因再次提交被算成新批次） ----
        ctx5 = AssistantContext(S, "d-reuse")
        a1 = await actions.create_pending(ctx5, spec_copy, {"goods_no": "G-3005"}, batch_id="b-1")
        a2 = await actions.create_pending(ctx5, spec_copy, {"goods_no": "G-3005"}, batch_id="b-2")
        check("D14 复用的旧卡保留原批次号（同一件事不会出现在两个批次里）",
              a2["reused"] is True and a2["batch_id"] == "b-1", f"{a2.get('batch_id')}")
        check("D15 未指定批次时默认落空批（界面按单卡处理）",
              a1["batch_id"] == "b-1", str(a1.get("batch_id")))
    finally:
        object.__setattr__(spec_cover, "handler", orig_cover)
        object.__setattr__(spec_copy, "handler", orig_copy)
        loop.get_chat_model = original_model


async def group_e() -> None:
    """「在后台生成」必须真有后台任务。

    对应实测（2026-10-20）：
        卖家：「给电竞椅生成促销文案、以及宣传图」
        助手：「宣传图也在后台生成中，完成后会自动加到商品图片里。」
    但那一轮**一次工具都没调用**，ops_task 里也没有这条 —— 任务中心空着，
    卖家问「显示在后台生成，为什么任务中没有显示？」。
    来源是它自己上一轮的话（"还在等您确认"）被复述成了"在生成中"。
    """
    print("\n【E 组】「在后台生成」必须真有任务")
    fake_repo = FakeAssistantRepo()
    actions.repo = fake_repo
    loop.repo = fake_repo
    loop.cost_gate = FakeCostGate()
    original_model = loop.get_chat_model
    original_count = loop._unfinished_count

    counts = {"value": 0}

    async def fake_count(_seller_id):
        return counts["value"]

    loop._unfinished_count = fake_count

    spec_cover = registry.get("generate_cover")

    async def fake_cover(ctx_, **kw):
        return ToolResult("已把宣传图生成放到后台了（任务 #7，约 30—60 秒）。", {"id": 7})

    orig_cover = spec_cover.handler
    object.__setattr__(spec_cover, "handler", fake_cover)
    try:
        # ---- E1：说了"在后台生成中"，但既无任务也无工具调用 → 纠正轮真的去建卡 ----
        ctx1 = AssistantContext(S, "e-claim")
        stub1 = StubModel([
            {"content": "文案已经更新好了 ✅ 宣传图也在后台生成中，完成后会自动加到商品图片里。"},
            {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0015"}}]},
            {"content": "文案已更新；宣传图还没开始生成 —— 需要你点一下确认。"},
        ])
        loop.get_chat_model = lambda: stub1
        counts["value"] = 0
        out1 = await loop.run_turn(ctx1, "给电竞椅生成促销文案、以及宣传图")
        check("E1 声称「后台生成中」但无任务 → 触发纠正轮并真建出确认卡",
              stub1.calls == 3 and len(out1["pending"]) == 1,
              f"模型被调 {stub1.calls} 次（应 3），pending={len(out1['pending'])}")
        check("E2 纠正后不再谎报「后台生成中」",
              "后台生成中" not in out1["reply"] and "生成中" not in out1["reply"],
              out1["reply"][:80])

        # ---- E3：纠正轮也救不回来（模型坚持原话、不调工具）→ 由代码说出事实 ----
        ctx2 = AssistantContext(S, "e-stubborn")
        stubborn = {"content": "宣传图正在后台生成中，请稍等。"}
        stub2 = StubModel([dict(stubborn), dict(stubborn), dict(stubborn), dict(stubborn)])
        loop.get_chat_model = lambda: stub2
        out2 = await loop.run_turn(ctx2, "宣传图呢？")
        check("E3 模型坚持瞎说时，由代码追加「没有正在执行的后台任务」",
              "没有正在执行的后台任务" in out2["reply"], out2["reply"][-90:])
        check("E4 兜底说明同时点明「还没开始生成」并要求重新发起",
              "还没开始生成" in out2["reply"] and "重新发起" in out2["reply"],
              out2["reply"][-90:])

        # ---- E5：**真的有任务在跑**时不能误伤（真实的"在生成中"要放行）----
        ctx3 = AssistantContext(S, "e-truth")
        stub3 = StubModel([{"content": "宣传图还在后台生成中，大概再等半分钟。"}])
        loop.get_chat_model = lambda: stub3
        counts["value"] = 1
        out3 = await loop.run_turn(ctx3, "图好了吗？")
        check("E5 有任务在跑时「在后台生成中」直接放行（不触发纠正轮）",
              stub3.calls == 1 and "后台生成中" in out3["reply"],
              f"模型被调 {stub3.calls} 次（应 1）")
        counts["value"] = 0

        # ---- E6：事实注入系统提示（源头掐掉记忆复述）----
        prompt_none = loop.build_system_prompt(AssistantContext(S, "e-p"), unfinished_tasks=0)
        prompt_some = loop.build_system_prompt(AssistantContext(S, "e-p"), unfinished_tasks=2)
        check("E6 无任务时系统提示明说「没有正在执行的后台任务」",
              "当前后台任务" in prompt_none and "没有**正在执行的后台任务" in prompt_none,
              prompt_none[-120:])
        check("E7 有任务时系统提示给出数量（并指向 list_tasks）",
              "2 个任务正在后台跑" in prompt_some and "list_tasks" in prompt_some,
              prompt_some[-120:])

        # ---- E8：工具未就绪时**不作废**卡（旧行为会永久吞掉这张卡）----
        ctx4 = AssistantContext(S, "e-missing")
        await actions.create_pending(ctx4, spec_cover, {"goods_no": "G-7777"}, batch_id="b-e8")
        target = next(a for a in fake_repo.actions.values()
                      if a["thread_id"] == "e-missing" and a["status"] == "pending")
        saved = registry._REGISTRY.pop("generate_cover")      # 模拟"注册表里查不到这个工具"
        try:
            await actions.confirm(ctx4, target["id"])
            check("E8 工具查不到时应报 409", False, "竟然执行成功了")
        except AssistantError as exc:
            check("E8 工具查不到时返回可重试的 409（不是永久作废）",
                  exc.status_code == 409 and exc.code == "tool_not_ready", exc.code)
        finally:
            registry._REGISTRY["generate_cover"] = saved
        row = fake_repo.actions[target["id"]]
        check("E9 卡仍停在 pending（重启/刷新后可重新点）", row["status"] == "pending",
              str(row["status"]))
        said = "".join(m["content"] for m in fake_repo.messages
                       if m["thread_id"] == "e-missing" and m["role"] == "assistant")
        check("E10 作废/失败必须留一句人话（不再静默消失）",
              "没有执行" in said and "刷新" in said, said[:90])
    finally:
        object.__setattr__(spec_cover, "handler", orig_cover)
        loop.get_chat_model = original_model
        loop._unfinished_count = original_count


async def group_f() -> None:
    """上下文窗口必须含**本轮卖家消息**。

    对应实测（2026-10-20）：卖家连续四轮问「售后政策中哪些不予受理？」，
    助手每轮都答「电竞椅的文案已经成功更新…」，重启前后都一样。
    根因不在提示词：`list_messages` 用 `ORDER BY seq ASC LIMIT 40` 取的是**最早**的 40 条，
    会话涨到 54 条后窗口仍是 seq 1–40（末尾是"电竞椅文案"那条工具消息），
    卖家新消息压根没进模型 —— 模型只能接着窗口最后一句助手消息往下写。
    """
    print("\n【F 组】上下文窗口必须含本轮卖家消息")
    fake_repo = FakeAssistantRepo()
    actions.repo = fake_repo
    loop.repo = fake_repo
    loop.cost_gate = FakeCostGate()
    original_model = loop.get_chat_model
    original_list = fake_repo.list_messages
    try:
        # ---- F1/F2：窗口被截断（只回了最早那批，本轮问题不在里面）----
        ctx1 = AssistantContext(S, "f-window")
        for i in range(60):
            await fake_repo.append_message(
                "f-window", "seller" if i % 2 == 0 else "assistant", f"旧消息 {i}"
            )
        all_msgs = list(fake_repo.messages)

        async def oldest_only(thread_id, limit=200):
            return [m for m in all_msgs if m["thread_id"] == thread_id][:limit]

        fake_repo.list_messages = oldest_only
        stub1 = StubModel([{"content": "好的。"}])
        loop.get_chat_model = lambda: stub1
        question = "售后政策中哪些不予受理？"
        await loop.run_turn(ctx1, question)
        sent = stub1.seen[0]
        check("F1 历史被截断时，本轮卖家消息仍是模型输入的**最后一条**",
              sent[-1]["role"] == "user" and sent[-1]["content"] == question,
              f"最后一条={sent[-1]['role']}:{sent[-1]['content'][:30]}")
        check("F2 且只出现一次（不重复回灌同一句）",
              sum(1 for m in sent if m["content"] == question) == 1,
              f"出现 {sum(1 for m in sent if m['content'] == question)} 次")

        # ---- F3：窗口从一段助手自述开头 → 必须裁到第一条卖家发言 ----
        ctx2 = AssistantContext(S, "f-lead")
        await fake_repo.append_message("f-lead", "assistant", "（上半段被截掉了的助手自述）")
        await fake_repo.append_message("f-lead", "assistant", "（又一句助手自述）")
        stub2 = StubModel([{"content": "好的。"}])
        loop.get_chat_model = lambda: stub2
        await loop.run_turn(ctx2, "这件商品多少钱？")
        sent2 = stub2.seen[0]
        check("F3 上下文从卖家发言开始（不以下半截助手自述开头）",
              len(sent2) > 1 and sent2[0]["role"] == "system" and sent2[1]["role"] == "user",
              f"共 {len(sent2)} 条，第 2 条="
              f"{sent2[1]['role'] if len(sent2) > 1 else '(缺失)'}")

        # ---- F4：正常窗口（本轮消息已在历史里）→ 不得重复追加 ----
        fake_repo.list_messages = original_list
        ctx3 = AssistantContext(S, "f-normal")
        stub3 = StubModel([{"content": "好的。"}])
        loop.get_chat_model = lambda: stub3
        await loop.run_turn(ctx3, "第一句")
        await loop.run_turn(ctx3, "第二句")
        sent3 = stub3.seen[-1]
        check("F4 本轮消息已在历史末尾时只出现一次（不重复）",
              sent3[-1]["content"] == "第二句"
              and sum(1 for m in sent3 if m["content"] == "第二句") == 1,
              f"最后={sent3[-1]['content'][:20]}，"
              f"共 {sum(1 for m in sent3 if m['content'] == '第二句')} 次")

        # ---- F5/F6：窗口 SQL 静态契约（回退成 `ASC LIMIT` 会立刻失败）----
        from app.db import assistant_repo as _arepo
        from app.db import chat_repo as _crepo
        check("F5 助手历史窗口 = 先倒序截断再正序",
              "seq DESC" in _arepo.SQL_RECENT_MESSAGES
              and "ORDER BY t.seq ASC" in _arepo.SQL_RECENT_MESSAGES,
              _arepo.SQL_RECENT_MESSAGES[:60])
        check("F6 售后历史窗口 = 先倒序截断再正序（同一类坑）",
              "seq DESC" in _crepo.SQL_RECENT_MESSAGES
              and "ORDER BY t.seq ASC" in _crepo.SQL_RECENT_MESSAGES,
              _crepo.SQL_RECENT_MESSAGES[:60])
    finally:
        fake_repo.list_messages = original_list
        loop.get_chat_model = original_model


async def main() -> int:
    # ⚠️ 全局禁用真模型：`confirm` 现在会"续跑对话"，若走真模型会产生计费调用。
    # 各用例需要模型时自行覆盖 `loop.get_chat_model`。
    loop.get_chat_model = lambda: None

    group_a()
    await group_b()
    await group_c()
    await group_d()
    await group_e()
    await group_f()
    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
