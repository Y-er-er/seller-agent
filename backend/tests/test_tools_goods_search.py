"""商品检索工具的**「搜不到」说法**测试 —— 假仓储，零外部依赖。

来历（2026-10-21 实测）：`list_goods` 关键词查空时，旧文案是笼统的
「没有符合条件的商品」，模型据此对卖家断言「店里没有售卖这款商品」——
而库里明明有（只是卖家少说了「RTX」）。这是「检索不到」的第三种混说：
**「这个词没命中」被说成了「库里没有」**。

  F 组 `_list_goods` 空结果：关键词查空必须明说"只是词没命中，别断言没有"；
       不带关键词查空才允许说"店里一件都没有"。

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_tools_goods_search.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import tools_goods  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


CTX = AssistantContext(seller_id="s", thread_id="t", view="admin", selected_goods_no="")


async def main() -> None:
    # F1 带关键词查空 → 必须点破「词没命中 ≠ 没有」，且**不能**出现旧的笼统说法
    original = tools_goods.repo.list_goods

    async def empty_list(seller_id, **kw):
        return []

    async def one_item(seller_id, **kw):
        return [{"goods_no": "G-0012", "title": "英伟达RTX5090显卡", "price": 8500.0,
                 "status": "on_shelf", "stock": 5, "category": "数码配件"}]

    tools_goods.repo.list_goods = empty_list
    try:
        r0 = await tools_goods._list_goods(CTX, keyword="英伟达5090显卡")
        text0 = r0.content
        check("F1 关键词查空点破「不等于没有」", "不等于店里没有" in text0, text0[:160])
        check("F2 不再用笼统的「没有符合条件的商品」", "没有符合条件的商品" not in text0, text0[:160])
        check("F3 说明是关键词没命中", "这个词没命中" in text0, text0[:160])

        r1 = await tools_goods._list_goods(CTX, keyword="")
        check("F4 不带关键词查空才说「一件都没有」", "一件商品都没有" in r1.content, r1.content[:120])
    finally:
        tools_goods.repo.list_goods = original

    # F5 非空结果照常列出（不因改造而丢数据）
    tools_goods.repo.list_goods = one_item
    try:
        r2 = await tools_goods._list_goods(CTX, keyword="5090")
        check("F5 命中结果照常列出", "G-0012" in r2.content and "8500.00" in r2.content,
              r2.content[:160])
    finally:
        tools_goods.repo.list_goods = original

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
