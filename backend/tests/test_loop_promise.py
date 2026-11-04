"""「未来承诺」守卫与工具预算打穿的**离线**测试 —— 不连库、不调模型。

来历（2026-10-21 实测）：卖家一句「增加 4 个商品 + 宣传图 + 文案 + 上架」，工具预算 8
被「查 + 建 4 品」用光，后续所有确认卡请求都被拦截，模型只能回一句
「我马上接着为您处理……稍后需要您在弹出卡片上统一确认」——
轮次一结束什么都不会自动发生：没建卡、没任务，卖家干等（截图为证）。

已有的两道守卫拦不住这句：A 守卫收**过去时**（已发起/已弹出），
B 守卫收**进行时**（正在生成中）——「马上接着处理」是**将来时**。
这组测试钉住三层补丁：

  M 组 `_has_promise` / 预算文案：将来时标记 + 打穿后的诚实措辞
  X 组 预算打穿路径：跳过纠正轮、由代码补「发『继续』」的事实
  Y 组 承诺守卫路径：触发纠正轮逼它真调工具建卡
  Z 组 放行路径：本轮真建了卡 → 承诺不算撒谎，不纠正

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_loop_promise.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import actions, loop, registry, tools_goods  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402

S = "default"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


# ============================================================
# 假仓储 / 假闸门 / 脚本桩（与 test_assistant.py 同款，够用即可）
# ============================================================


class FakeAssistantRepo:
    # actions.py 会读仓储类上的状态常量，必须与真实实现同名
    ACTION_PENDING = "pending"
    ACTION_CONFIRMED = "confirmed"
    ACTION_CANCELLED = "cancelled"
    ACTION_FAILED = "failed"
    ACTION_RUNNING = "running"

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
            "status": "pending", "result": None, "seller_id": seller_id,
            "batch_id": batch_id,
        }
        return self._aid

    async def get_action(self, action_id, seller_id):
        a = self.actions.get(action_id)
        return dict(a) if a and a["seller_id"] == seller_id else None

    async def list_actions(self, thread_id, seller_id, status="", limit=50):
        return [dict(a) for a in self.actions.values()
                if a["thread_id"] == thread_id and a["seller_id"] == seller_id
                and (not status or a["status"] == status)]

    async def find_pending(self, thread_id, tool_name, arguments, seller_id):
        for a in self.actions.values():
            if (a["thread_id"] == thread_id and a["seller_id"] == seller_id
                    and a["tool_name"] == tool_name
                    and a["status"] == "pending" and a["arguments"] == arguments):
                return dict(a)
        return None

    async def find_recent_done(self, thread_id, tool_name, arguments, seller_id, minutes=30):
        return None

    async def expire_stale(self, hours=24):
        return 0

    async def append_message(self, thread_id, role, content="", **kw):
        self._mid += 1
        self.messages.append({"id": self._mid, "thread_id": thread_id, "seq": self._mid,
                              "role": role, "content": content})
        return self.messages[-1]

    async def list_messages(self, thread_id, limit=200):
        return [m for m in self.messages if m["thread_id"] == thread_id][-limit:]


class FakeCostGate:
    async def precheck(self, ongoing: bool = True):
        return types.SimpleNamespace(paused=False, message="", reason="")

    async def record_usage(self, *args, **kwargs) -> None:
        return None


class StubModel:
    """脚本化桩：按预设脚本依次返回，零计费。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.calls = 0

    async def step(self, messages, tools):
        from app.chat.llm_adapter import ModelTurn, ToolCall
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        calls = [ToolCall(name=c["name"], arguments=c.get("arguments", {}), id=f"c{self.calls}")
                 for c in item.get("tool_calls", [])]
        return ModelTurn(content=item.get("content", ""), tool_calls=calls,
                         usage={"input_tokens": 10, "output_tokens": 10})


def _setup() -> FakeAssistantRepo:
    fake_repo = FakeAssistantRepo()
    actions.repo = fake_repo
    loop.repo = fake_repo
    loop.cost_gate = FakeCostGate()
    return fake_repo


# ============================================================
# 用例
# ============================================================


def group_m() -> None:
    print("\n【M 组】将来时标记与预算文案")
    observed = ("精品猫粮的价格和库存，以及所有商品的宣传图生成、促销版文案生成和上架操作，"
                "我马上接着为您处理。")
    check("M1 实测原句命中将来时标记", loop._has_promise(observed), observed[:40])
    check("M2 各类承诺说法命中",
          all(loop._has_promise(t) for t in (
              "接下来我会生成宣传图", "随后我会为您上架", "稍后我会处理",
              "马上为您处理文案", "我接着处理剩下的")))
    check("M3 事实性说法不命中",
          not any(loop._has_promise(t) for t in (
              "稍后需要您在卡片上确认",  # 卡已建时的正常提示（守卫还会再查卡）
              "已经全部完成", "还没有开始生成，需要你确认")))


def group_x() -> None:
    print("\n【X 组】预算打穿路径（复现 2026-10-21 现场）")
    fake_repo = _setup()
    ctx = AssistantContext(S, "x-thread")
    original_budget = loop.DEFAULT_TOOL_BUDGET
    original_model = loop.get_chat_model
    # 预算压到 1：第 1 次调用建卡，第 2 次必被打穿 —— 等比缩小复现 8→15 的现场
    loop.DEFAULT_TOOL_BUDGET = 1
    stub = StubModel([
        {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0001"}}]},
        {"content": "好的，稍后我会为您生成宣传图和文案，马上接着处理。",
         "tool_calls": [{"name": "generate_copy", "arguments": {"goods_no": "G-0001"}}]},
        {"content": "好的，稍后我会为您生成宣传图和文案，马上接着处理。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = asyncio.run(loop.run_turn(ctx, "给 G-0001 生成宣传图和文案"))
        check("X1 第 2 次调用被预算拦截",
              any("超出本轮工具预算" in str(t.get("summary")) for t in out["trace"]),
              str(out["trace"])[:150])
        check("X2 只建出 1 张卡（被拦的那次没有建卡）",
              len(fake_repo.actions) == 1 and len(out["pending"]) == 1,
              f"cards={len(fake_repo.actions)}")
        check("X3 预算打穿后不再走纠正轮（不浪费计费）", stub.calls == 3, f"model_calls={stub.calls}")
        check("X4 代码补上「还没开始 + 发继续」的事实",
              "并没有开始" in out["reply"] and "继续" in out["reply"], out["reply"][:160])
    finally:
        loop.DEFAULT_TOOL_BUDGET = original_budget
        loop.get_chat_model = original_model
    import inspect
    src = inspect.getsource(loop)
    check("X6 旧误导文案已从源码移除",
          "已达到本轮工具调用上限，请先用已有信息回复卖家" not in src)
    check("X7 新文案要求如实交代并请卖家发「继续」",
          "请他回复「继续」" in src and "没有执行" in src)


def group_y() -> None:
    print("\n【Y 组】承诺守卫：逼它真调工具建卡")
    fake_repo = _setup()
    ctx = AssistantContext(S, "y-thread")
    original_model = loop.get_chat_model
    stub = StubModel([
        # 主循环：一句承诺收尾，什么工具都没调 —— 复现「嘴上接着处理」
        {"content": "宣传图的生成我马上接着为您处理。"},
        # 纠正轮第 1 步：真去调工具 → 建卡
        {"content": "已为您提交生成宣传图的请求，请在卡片上确认。",
         "tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0001"}}]},
        # 纠正轮第 2 步：如实收尾
        {"content": "宣传图还没有开始生成，共 1 步需要您在卡片上确认。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = asyncio.run(loop.run_turn(ctx, "给 G-0001 生成宣传图"))
        check("Y1 纠正轮被触发（比主循环多调了模型）", stub.calls >= 2, f"model_calls={stub.calls}")
        check("Y2 纠正轮真的建出了卡", len(fake_repo.actions) == 1, f"cards={len(fake_repo.actions)}")
        check("Y3 最终回复不再承诺「接着处理」", "马上接着" not in out["reply"], out["reply"][:100])
        check("Y4 最终回复如实说明待确认", "确认" in out["reply"], out["reply"][:100])
    finally:
        loop.get_chat_model = original_model


def group_y2() -> None:
    print("\n【Y2 组】纠正轮也没救回来 → 代码兜底")
    fake_repo = _setup()
    ctx = AssistantContext(S, "y2-thread")
    original_model = loop.get_chat_model
    stub = StubModel([
        {"content": "宣传图的生成我马上接着为您处理。"},
        # 纠正轮两步都只给文本、不调工具，还在嘴硬
        {"content": "好的，我马上接着为您处理宣传图。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = asyncio.run(loop.run_turn(ctx, "给 G-0001 生成宣传图"))
        check("Y2-1 一张卡都没建（纠正轮失败）", len(fake_repo.actions) == 0,
              f"cards={len(fake_repo.actions)}")
        check("Y2-2 代码补上「还没有开始」的事实",
              "还没有开始" in out["reply"] and "继续" in out["reply"], out["reply"][:160])
    finally:
        loop.get_chat_model = original_model


def group_z() -> None:
    print("\n【Z 组】放行路径：真建了卡 → 承诺不算撒谎")
    fake_repo = _setup()
    ctx = AssistantContext(S, "z-thread")
    original_model = loop.get_chat_model
    stub = StubModel([
        {"tool_calls": [{"name": "generate_cover", "arguments": {"goods_no": "G-0001"}}]},
        {"content": "已提交，您点完卡片后我马上接着为您处理后续。"},
    ])
    loop.get_chat_model = lambda: stub
    try:
        out = asyncio.run(loop.run_turn(ctx, "给 G-0001 生成宣传图"))
        check("Z1 本轮建了卡", len(fake_repo.actions) == 1, f"cards={len(fake_repo.actions)}")
        check("Z2 不触发纠正轮（stub.calls 只有两步）", stub.calls == 2, f"model_calls={stub.calls}")
        check("Z3 承诺原样保留（有卡背书，不算撒谎）", "马上接着" in out["reply"], out["reply"][:100])
    finally:
        loop.get_chat_model = original_model


def main() -> None:
    group_m()
    group_x()
    group_y()
    group_y2()
    group_z()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
