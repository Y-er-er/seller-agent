"""P1「批量计划进度 + 失败重试」的**离线**测试 —— 不连库、不调模型。

来历（2026-10-21）：`confirm_batch` 执行后，失败的步骤标 `failed` 就彻底消失了 ——
卖家只能重新跟助手说一遍（会把成功项也重新提交）。P1 补两条：
  1. `retry_failed`：失败项重置回待确认，让卖家再点一次确认（计费项重试仍需点头）；
  2. `list_batch_status`：模型能查批量计划的进度（成功/失败/待确认各多少）。

这组测试钉住：

  R 组 `actions.retry_failed`：重置数正确 + 无失败项时报 409
  B 组 `tools_ops._list_batch_status`：进度渲染 / 空批次话术

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_batch_progress.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import actions, tools_ops  # noqa: E402
from app.assistant.registry import AssistantContext, AssistantError  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def test_retry_failed_reset() -> None:
    print("\n【R 组】失败项重试")
    calls: dict[str, tuple] = {}

    class FakeRepo:
        async def reset_failed(self, batch_id, seller_id):
            calls["reset"] = (batch_id, seller_id)
            return 3

        async def append_message(self, *a, **k):
            return {}

    original = actions.repo
    actions.repo = FakeRepo()
    try:
        out = asyncio.run(actions.retry_failed(AssistantContext("s", "t-thread"), "b-1"))
        check("R1 重置数正确", out["reset"] == 3 and out["ok"] is True, str(out))
        check("R2 批次与卖家传参正确", calls["reset"] == ("b-1", "s"), str(calls))
    finally:
        actions.repo = original


def test_retry_failed_nothing() -> None:
    class FakeRepo:
        async def reset_failed(self, batch_id, seller_id):
            return 0

    original = actions.repo
    actions.repo = FakeRepo()
    try:
        try:
            asyncio.run(actions.retry_failed(AssistantContext("s", "t-thread"), "b-2"))
            check("R3 无失败项时应报错", False, "竟然成功返回")
        except AssistantError as exc:
            check("R3 无失败项时报 409", exc.code == "nothing_to_retry", exc.code)
    finally:
        actions.repo = original


def test_list_batch_status() -> None:
    print("\n【B 组】批量计划进度渲染")
    original = actions.recent_batches

    async def fake_recent(ctx, limit=5):
        return [{
            "batch_id": "b-1", "total": 4, "pending": 0, "running": 0,
            "confirmed": 2, "failed": 2, "cancelled": 0, "expired": 0,
        }]

    actions.recent_batches = fake_recent
    try:
        out = asyncio.run(tools_ops._list_batch_status(AssistantContext("s", "t-thread")))
        check("B1 渲染成功数", "2 成功" in out.content, out.content)
        check("B2 渲染失败数并提示可重试", "2 失败（可重试）" in out.content, out.content)
        check("B3 渲染总步数", "共 4 步" in out.content, out.content)
    finally:
        actions.recent_batches = original


def test_list_batch_status_empty() -> None:
    original = actions.recent_batches

    async def fake_recent(ctx, limit=5):
        return []

    actions.recent_batches = fake_recent
    try:
        out = asyncio.run(tools_ops._list_batch_status(AssistantContext("s", "t-thread")))
        check("B4 无批次时如实说没有", "没有" in out.content, out.content)
    finally:
        actions.recent_batches = original


def main() -> None:
    test_retry_failed_reset()
    test_retry_failed_nothing()
    test_list_batch_status()
    test_list_batch_status_empty()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
