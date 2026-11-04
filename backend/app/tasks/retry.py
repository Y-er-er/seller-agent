"""任务重试 —— 用**持久化的 payload** 重建执行体。

为什么需要单独一层：任务的执行体（handler）是**函数引用**，存不进数据库，
进程重启或请求结束后就没了。所以重试不能"再跑一遍那个 handler"，
只能按 `kind` + `payload` **重新构造**。

⚠️ 两条安全边界，都必须守：

1. **只有失败 / 中断的任务能重试** —— 成功的任务再跑 = 重复计费、或重复改动数据；
2. **只有白名单 kind 能重试** —— 批量操作重试会把已成功的部分再做一遍。

所以 `RETRYABLE_KINDS` 是"允许清单"，新任务类型默认**不可重试**，要显式加进来。
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import task_repo as repo

logger = logging.getLogger(__name__)

#: 允许重试的任务类型（**允许清单**，不是禁止清单）。
#: 当前只有 AI 生图：重试 = 再生成一张，语义清晰（但会再计费一次）。
RETRYABLE_KINDS: frozenset[str] = frozenset({"ai_cover"})


async def retry(seller_id: str, task_id: int) -> dict[str, Any]:
    """重试一个失败任务，返回**新任务行**（原任务保留，便于追溯）。

    抛 `GoodsError`（与路由层统一错误处理对齐）：
      - 404 任务不存在
      - 409 状态不允许 / 类型不支持 / 缺 payload
    """
    # 延迟导入：`goods.image_ai` 依赖 `tasks.runner`，此处进口避免循环依赖
    from app.goods.service import GoodsError

    task = await repo.get_task(task_id, seller_id)
    if task is None:
        raise GoodsError("task_not_found", f"任务不存在：{task_id}", 404)

    status = str(task.get("status") or "")
    if status not in (repo.TASK_FAILED, repo.TASK_INTERRUPTED):
        raise GoodsError(
            "task_not_retryable",
            f"只有失败或中断的任务能重试（当前状态：{status}）",
            409,
        )

    kind = str(task.get("kind") or "")
    if kind not in RETRYABLE_KINDS:
        raise GoodsError(
            "task_not_retryable",
            f"「{kind}」类型不支持重试 —— 重试会把已成功的部分再做一遍",
            409,
        )

    payload = task.get("payload") or {}
    if kind == "ai_cover":
        goods_id = payload.get("goods_id")
        if not goods_id:
            raise GoodsError(
                "task_not_retryable",
                "该任务缺少重试参数（改造前创建的旧任务没有 payload）",
                409,
            )
        from app.goods import image_ai

        logger.info("重试任务 #%s（ai_cover, goods_id=%s）", task_id, goods_id)
        return await image_ai.generate_cover(
            seller_id, int(goods_id), hint=str(payload.get("hint") or "")
        )

    raise GoodsError("task_not_retryable", f"未实现的重试类型：{kind}", 409)
