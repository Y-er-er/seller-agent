"""`plan_batch` 批量计划工具的**离线**测试 —— 不连库、不调模型。

来历（2026-10-21 实测）：卖家一句「增加 4 个商品 + 文案 + 宣传图 + 上架」，
模型逐件逐个调工具 → 单轮工具预算打穿 → 后半段确认卡一张没建。`plan_batch` 把
「N 件商品 × 每件多步」压缩成**一次**工具调用：确认类动作展开成同批 pending_action
（一张卡一次确认），非确认动作直接执行。

这组测试钉住：

  T 组 展开：确认类 → N 条同批 pending；非确认 → 直接执行
  U 组 幂等：同轮内同工具+同参数的非确认写操作只执行一次
  K 组 未知工具：跳过并计入失败，不中断整批
  G 组 返回引导：让模型如实说「共 N 步需要确认」

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_plan_batch.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import actions, tools_goods  # noqa: E402
from app.assistant.registry import AssistantContext, ToolResult, ToolSpec  # noqa: E402
import app.assistant.registry as registry_mod  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class FakeRepo:
    """够 `actions.create_pending` 用的极简仓储（不连真库）。"""

    ACTION_PENDING = "pending"
    ACTION_CONFIRMED = "confirmed"
    ACTION_CANCELLED = "cancelled"
    ACTION_FAILED = "failed"
    ACTION_RUNNING = "running"

    def __init__(self) -> None:
        self.actions: list[dict] = []
        self._aid = 0

    async def list_actions(self, thread_id, seller_id, status="", limit=50):
        rows = [a for a in self.actions if a["thread_id"] == thread_id
                and a["seller_id"] == seller_id and (not status or a["status"] == status)]
        return rows[:limit]

    async def create_action(self, thread_id, tool_name, arguments, summary, detail="",
                            seller_id="", batch_id=""):
        self._aid += 1
        a = {"id": self._aid, "thread_id": thread_id, "seller_id": seller_id,
             "tool_name": tool_name, "arguments": arguments, "summary": summary,
             "detail": detail, "batch_id": batch_id, "status": self.ACTION_PENDING,
             "result": None, "decided_at": None}
        self.actions.append(a)
        return self._aid


def _make_specs(calls: list[str]) -> dict[str, ToolSpec]:
    async def _ok(ctx, **kw):
        calls.append(str(kw.get("goods_no") or kw.get("title") or ""))
        return ToolResult("ok")

    async def _fail(ctx, **kw):
        return ToolResult("写不进去", ok=False)

    cover = ToolSpec(name="generate_cover", domain="goods", description="生成宣传图",
                     parameters={}, handler=_ok, billable=True,
                     summary_template="为商品 {goods_no} 生成宣传图",
                     idempotent_by=("goods_no",))
    copy = ToolSpec(name="generate_copy", domain="goods", description="生成文案",
                    parameters={}, handler=_ok, billable=True,
                    summary_template="为商品 {goods_no} 生成文案候选",
                    idempotent_by=("goods_no",))
    create = ToolSpec(name="create_goods", domain="goods", description="新建商品",
                      parameters={}, handler=_ok)
    update = ToolSpec(name="update_goods", domain="goods", description="改商品",
                      parameters={}, handler=_fail)
    return {"generate_cover": cover, "generate_copy": copy,
            "create_goods": create, "update_goods": update}


def _setup(calls: list[str]) -> FakeRepo:
    fake_repo = FakeRepo()
    specs = _make_specs(calls)
    registry_mod.get = lambda name: specs.get(name)   # noqa: ARG005 —— 测试桩
    actions.repo = fake_repo
    return fake_repo


def test_expand() -> None:
    print("\n【T 组】展开：确认类 → 同批 pending；非确认 → 直接执行")
    calls: list[str] = []
    fake_repo = _setup(calls)
    ctx = AssistantContext("s", "t-thread")
    out = asyncio.run(tools_goods._plan_batch(ctx, [
        {"tool": "generate_cover", "args": {"goods_no": "G-0001"}},
        {"tool": "generate_copy", "args": {"goods_no": "G-0001"}},
        {"tool": "create_goods", "args": {"title": "新品"}},
    ]))
    pending = [a for a in fake_repo.actions if a["status"] == "pending"]
    batch_ids = {a["batch_id"] for a in pending}
    check("T1 确认类落成 2 条待确认", len(pending) == 2, f"pending={len(pending)}")
    check("T2 两条同属一个批次（一张卡）", len(batch_ids) == 1 and batch_ids != {""},
          str(batch_ids))
    check("T3 非确认类直接执行了 1 次", calls == ["新品"], str(calls))
    check("T4 计费项计数正确", out.data["billable_count"] == 2, str(out.data))
    check("T5 待确认数正确", out.data["pending_count"] == 2, str(out.data))


def test_dedup() -> None:
    print("\n【U 组】同轮内同工具+同参数的非确认写操作只执行一次")
    calls: list[str] = []
    _setup(calls)
    ctx = AssistantContext("s", "t-thread")
    out = asyncio.run(tools_goods._plan_batch(ctx, [
        {"tool": "create_goods", "args": {"title": "新品A"}},
        {"tool": "create_goods", "args": {"title": "新品A"}},   # 重复
        {"tool": "create_goods", "args": {"title": "新品B"}},
    ]))
    check("U1 重复的非确认写只执行一次", calls == ["新品A", "新品B"], str(calls))
    check("U2 直接完成数只算不重复的", out.data["done"] == 2, str(out.data))


def test_unknown() -> None:
    print("\n【K 组】未知工具跳过，不中断整批")
    calls: list[str] = []
    fake_repo = _setup(calls)
    ctx = AssistantContext("s", "t-thread")
    out = asyncio.run(tools_goods._plan_batch(ctx, [
        {"tool": "no_such_tool", "args": {}},
        {"tool": "generate_cover", "args": {"goods_no": "G-0002"}},
    ]))
    check("K1 未知工具被记入失败", "不存在" in out.content, out.content)
    check("K2 其余项照常落卡", len([a for a in fake_repo.actions
                                    if a["status"] == "pending"]) == 1)


def test_fail_isolated() -> None:
    print("\n【F 组】非确认动作执行失败如实记入，不影响整批")
    calls: list[str] = []
    fake_repo = _setup(calls)
    ctx = AssistantContext("s", "t-thread")
    out = asyncio.run(tools_goods._plan_batch(ctx, [
        {"tool": "update_goods", "args": {"goods_no": "G-0001"}},   # _fail
        {"tool": "generate_cover", "args": {"goods_no": "G-0003"}},
    ]))
    check("F1 失败被记入 failures", "写不进去" in out.content, out.content)
    check("F2 后续确认项照常落卡", len([a for a in fake_repo.actions
                                        if a["status"] == "pending"]) == 1)


def test_guidance() -> None:
    print("\n【G 组】返回内容引导模型如实收尾")
    calls: list[str] = []
    _setup(calls)
    ctx = AssistantContext("s", "t-thread")
    out = asyncio.run(tools_goods._plan_batch(ctx, [
        {"tool": "generate_cover", "args": {"goods_no": "G-0001"}},
    ]))
    check("G1 提示「共 N 步需要确认」", "共 1 步需要他" in out.content, out.content)
    check("G2 不许说已全部完成", "不要逐步说「已发起」" in out.content, out.content)


def main() -> None:
    test_expand()
    test_dedup()
    test_unknown()
    test_fail_isolated()
    test_guidance()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
