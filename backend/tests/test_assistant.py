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

    def __init__(self) -> None:
        self.actions: dict[int, dict] = {}
        self.messages: list[dict] = []
        self._aid = 0
        self._mid = 0

    async def create_action(self, thread_id, tool_name, arguments, summary, detail="", seller_id=S):
        self._aid += 1
        self.actions[self._aid] = {
            "id": self._aid, "thread_id": thread_id, "tool_name": tool_name,
            "arguments": arguments, "summary": summary, "detail": detail,
            "status": self.ACTION_PENDING, "result": None, "seller_id": seller_id,
        }
        return self._aid

    async def get_action(self, action_id, seller_id):        # ⚠️ seller_id 必传
        a = self.actions.get(action_id)
        return dict(a) if a and a["seller_id"] == seller_id else None

    async def list_actions(self, thread_id, seller_id, status="", limit=50):
        return [dict(a) for a in self.actions.values()
                if a["thread_id"] == thread_id and a["seller_id"] == seller_id
                and (not status or a["status"] == status)]

    async def decide_action(self, action_id, status, result, seller_id):   # ⚠️ seller_id 必传
        a = self.actions.get(action_id)
        if not a or a["seller_id"] != seller_id or a["status"] != self.ACTION_PENDING:
            return False
        a["status"], a["result"] = status, result
        return True

    async def append_message(self, thread_id, role, content="", **kw):
        self._mid += 1
        self.messages.append({"id": self._mid, "thread_id": thread_id, "seq": self._mid,
                              "role": role, "content": content})
        return self.messages[-1]

    async def list_messages(self, thread_id, limit=200):
        return [m for m in self.messages if m["thread_id"] == thread_id][:limit]


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
    """脚本化桩：按预设脚本依次返回，**零计费**。"""
    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.calls = 0

    async def step(self, messages, tools):
        from app.chat.llm_adapter import ModelTurn, ToolCall
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
    check("A1 工具注册（幂等）", len(specs) == 16, f"{len(specs)} 个")
    check("A2 破坏性工具标记正确",
          {s.name for s in specs if s.destructive} ==
          {"delete_goods", "bulk_apply", "resume_aftersale_case", "close_aftersale_case"},
          str([s.name for s in specs if s.destructive]))
    # 计费工具（阶段五补）：能生成图/文案/定价 —— 此前只有端点、没进注册表，
    # 导致助手只能说"系统不支持直接生成图片"
    check("A2b 计费工具已注册（生成图/文案/定价）",
          {s.name for s in specs if s.billable} ==
          {"generate_cover", "generate_copy", "suggest_price"},
          str([s.name for s in specs if s.billable]))
    check("A2c 需确认 = 不可逆 ∪ 计费",
          {s.name for s in specs if s.needs_confirm} ==
          {s.name for s in specs if s.destructive or s.billable})
    check("A2d 计费工具的确认卡理由标记为 billable",
          registry.get("generate_cover").confirm_kind == "billable"
          and registry.get("delete_goods").confirm_kind == "destructive")
    check("A3 能力清单按域分组",
          {g["domain"] for g in registry.capabilities()} == {"goods", "display", "aftersale"},
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

    check("A8 单件文案渲染",
          registry.get("delete_goods").describe_action({"goods_no": "G-0001"}) == "删除商品 G-0001")
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
    check("A16 system prompt 要求「生成文案后立即写入」",
          "立即调用" in prompt_rules and "update_goods" in prompt_rules, prompt_rules[-220:])
    copy_desc = registry.get("generate_copy").description
    check("A17 generate_copy 的描述引导「生成后继续调用 update_goods」",
          "继续调用 update_goods" in copy_desc, copy_desc)
    check("A18 system prompt 要求「多件事一次做完」",
          "要把它们做完" in prompt_rules, prompt_rules[:260])


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
    check("B3 确认卡主文案是人话", card["summary"] == "删除商品 G-0001", card["summary"])

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


async def main() -> int:
    group_a()
    await group_b()
    await group_c()
    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
