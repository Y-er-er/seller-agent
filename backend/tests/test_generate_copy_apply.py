"""`generate_copy` 的 `apply` 写入行为的**离线**测试 —— 不连库、不调模型。

来历（2026-10-21 P2）：旧设计里「生成文案」和「写入商品」是两步 —— 生成靠确认卡执行，
写入靠确认后的**模型续跑**再调 update_goods 补。这非确定性：模型可能停手不写
（于是要 `_warn_copy_not_applied` 兜底）、可能重复写，数十件商品时续跑写回还会撞预算。
P2 把「生成并写入」做成 `generate_copy(apply=true)` 一步原子完成。

这组测试钉住：

  A 组 apply=true：生成后**直接写回**商品
  N 组 apply=false：只生成候选、不写回（保持「先看看」能力）

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_generate_copy_apply.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import tools_goods  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402
import app.goods.ai as goods_ai  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class FakeService:
    """够 `_generate_copy` 用的极简商品服务（find + 写回记录）。"""

    def __init__(self) -> None:
        self.goods = {"G-0001": {"id": 1, "goods_no": "G-0001", "title": "测试品",
                                 "description": ""}}
        self.updated: list[dict] = []

    async def find_by_no(self, seller_id, goods_no):
        g = self.goods.get(goods_no)
        return dict(g) if g else None

    async def update_goods(self, seller_id, goods_id, *, actor="", **fields):
        self.updated.append({"goods_id": goods_id, "actor": actor, **fields})
        if "description" in fields:
            self.goods["G-0001"]["description"] = fields["description"]
        return self.goods["G-0001"]


async def _fake_generate_copy(seller_id, goods_id, style):
    return {"copy": "这是生成的文案", "style": style}


def test_apply_true() -> None:
    print("\n【A 组】apply=true：生成后直接写回")
    svc = FakeService()
    orig_svc, orig_gen = tools_goods.service, goods_ai.generate_copy
    tools_goods.service = svc
    goods_ai.generate_copy = _fake_generate_copy
    try:
        out = asyncio.run(
            tools_goods._generate_copy(AssistantContext("s", "t"), "G-0001", "口语", True)
        )
        check("A1 结果文案说明已写入", "已写入商品详情" in out.content, out.content)
        check("A2 写回发生且文案正确",
              len(svc.updated) == 1 and svc.updated[0].get("description") == "这是生成的文案",
              str(svc.updated))
        check("A3 写回走 AI 身份（操作日志可追溯到助手）",
              svc.updated[0].get("actor") == tools_goods.repo.ACTOR_AI, str(svc.updated[0]))
    finally:
        tools_goods.service, goods_ai.generate_copy = orig_svc, orig_gen


def test_apply_false() -> None:
    print("\n【N 组】apply=false：只生成候选，不写回")
    svc = FakeService()
    orig_svc, orig_gen = tools_goods.service, goods_ai.generate_copy
    tools_goods.service = svc
    goods_ai.generate_copy = _fake_generate_copy
    try:
        out = asyncio.run(
            tools_goods._generate_copy(AssistantContext("s", "t"), "G-0001", "口语", False)
        )
        check("N1 不写回商品", len(svc.updated) == 0, str(svc.updated))
        check("N2 结果文案不声称已写入", "已写入" not in out.content, out.content)
        check("N3 候选内容可见", "这是生成的文案" in out.content, out.content)
    finally:
        tools_goods.service, goods_ai.generate_copy = orig_svc, orig_gen


def main() -> None:
    test_apply_true()
    test_apply_false()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
