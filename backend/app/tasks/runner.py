"""任务执行器：把长任务放到**后台协程**里跑，调用方立即返回。

为什么不能同步：AI 生图要 30—60 秒。同步执行意味着
① 界面被一个转圈卡住一分钟；② 关掉浮窗/刷新页面就再也看不到结果。
后台跑 + 任务记录，才能做到"**关掉也在跑、回来还能看**"。

⚠️ 代价必须说清：协程随进程走，**服务重启会丢任务**。
所以启动时要 `task_repo.mark_interrupted()` 把遗留的"排队中/执行中"标成「中断」——
如实告诉用户没跑完，比留一个永远不动的进度条好。

两种用法：
- `submit()` —— 长任务（生图/文案/定价）：后台跑，立即返回任务行；
- `run_inline()` —— 毫秒级操作（批量操作）：当场执行并返回结果，但**仍留一条任务记录**，
  让"上次批量删了哪几件、失败哪件"事后可查。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.db import task_repo as repo

logger = logging.getLogger(__name__)

#: 后台协程句柄（持引用避免被 GC 回收；将来做"取消任务"也从这里下手）
_RUNNING: dict[int, asyncio.Task] = {}

#: 进度回调签名：`(百分比, 阶段说明)`
ProgressFn = Callable[[int, str], Awaitable[None]]


async def _noop(pct: int, stage: str) -> None:  # noqa: ARG001 —— 同步任务不需要进度
    return None


async def _run(task_id: int, handler: Callable[..., Awaitable[dict[str, Any]]],
               seller_id: str, kwargs: dict[str, Any]) -> None:
    """后台执行体：跑 handler，把结果/失败如实写回任务行。"""
    started = time.monotonic()
    await repo.update_task(task_id, status=repo.TASK_RUNNING, started_at=datetime.now())

    async def progress(pct: int, stage: str) -> None:
        # 进度更新失败不该让任务本身失败：DB 抖动时宁可少一条进度，也别把活干砸
        try:
            await repo.update_task(
                task_id, progress=max(0, min(100, int(pct))), stage=stage
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("更新任务进度失败（忽略）：%s", exc)

    try:
        result = await handler(progress=progress, seller_id=seller_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 任务失败要落库成"失败"，不能让异常消失在后台
        logger.warning("任务 %d 失败：%s: %s", task_id, type(exc).__name__, exc)
        await repo.update_task(
            task_id, status=repo.TASK_FAILED,
            error=f"{type(exc).__name__}: {exc}"[:500],
            finished_at=datetime.now(),
        )
        return

    await repo.update_task(
        task_id, status=repo.TASK_DONE, progress=100, stage="完成",
        result=(str(result.get("summary") or "")[:1000] or None),
        detail={**result, "elapsed_ms": int((time.monotonic() - started) * 1000)},
        finished_at=datetime.now(),
    )


async def submit(
    kind: str,
    title: str,
    handler: Callable[..., Awaitable[dict[str, Any]]],
    *,
    target: str = "",
    action_id: int | None = None,
    seller_id: str = "default",
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """登记任务并**后台执行**，立即返回任务行（`status=queued`）。

    调用方拿到 id 后即可回复用户"已在后台开始"，之后靠任务中心看进度。
    `payload` 是**重建执行体所需的参数** —— 失败后重试要靠它（handler 存不下来）。
    """
    task_id = await repo.create_task(
        kind, title, target=target, action_id=action_id, seller_id=seller_id,
        payload=payload,
    )
    job = asyncio.create_task(_run(task_id, handler, seller_id, kwargs))
    _RUNNING[task_id] = job
    job.add_done_callback(lambda _job: _RUNNING.pop(task_id, None))

    row = await repo.get_task(task_id, seller_id)
    return row or {
        "id": task_id, "kind": kind, "title": title,
        "status": repo.TASK_QUEUED, "progress": 0,
    }


async def run_inline(
    kind: str,
    title: str,
    handler: Callable[..., Awaitable[dict[str, Any]]],
    *,
    target: str = "",
    action_id: int | None = None,
    seller_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """**当场执行并记录**（毫秒级操作，如批量操作）。返回结果并附带 `task_id`。"""
    task_id = await repo.create_task(
        kind, title, target=target, action_id=action_id,
        status=repo.TASK_RUNNING, seller_id=seller_id,
    )
    started = time.monotonic()
    try:
        result = await handler(progress=_noop, seller_id=seller_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 失败要留痕，然后把异常还给调用方
        await repo.update_task(
            task_id, status=repo.TASK_FAILED,
            error=f"{type(exc).__name__}: {exc}"[:500],
            finished_at=datetime.now(),
        )
        raise
    await repo.update_task(
        task_id, status=repo.TASK_DONE, progress=100, stage="完成",
        result=(str(result.get("summary") or "")[:1000] or None),
        detail={**result, "elapsed_ms": int((time.monotonic() - started) * 1000)},
        finished_at=datetime.now(),
    )
    return {**result, "task_id": task_id}


def running_count() -> int:
    """当前进程内正在跑的后台任务数（调试/自检用）。"""
    return len(_RUNNING)
