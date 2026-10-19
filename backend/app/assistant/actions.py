"""待确认动作 —— 破坏性操作的**人在环**机制。

为什么它必须是服务端机制：模型会犯错，而删除、批量改价这类动作**不可逆**。
"确认"若只做在界面上的装饰，就等于没有 —— 本模块让它在服务端成立：
**标记为破坏性的工具，在卖家点头之前，它的执行函数根本不会被调用。**

流程：

```
模型提出破坏性工具
   → 循环「不执行」，落一条 pending_action（含影响明细）
   → 界面出确认卡
   → 卖家确认 → 这里才真正调用工具 → 结果写回对话
   → 卖家取消 → 动作作废，数据零改动
```
"""

from __future__ import annotations

import logging
from typing import Any

from app.assistant import registry
from app.assistant.registry import AssistantContext, AssistantError
from app.db import assistant_repo as repo

logger = logging.getLogger(__name__)


async def create_pending(
    ctx: AssistantContext, spec: registry.ToolSpec, args: dict[str, Any]
) -> dict[str, Any]:
    """把一个破坏性调用落成待确认项，返回确认卡数据。

    ⚠️ **本函数不执行工具** —— 这是整个机制的关键：执行发生在 `confirm()` 里。
    """
    summary = spec.describe_action(args)
    detail = ""
    if spec.impact_builder is not None:
        try:
            detail = await spec.impact_builder(ctx, args)
        except Exception as exc:  # noqa: BLE001 —— 预览失败不影响"停下来等确认"这件事
            logger.warning("生成影响预览失败（%s）：%s", spec.name, exc)

    action_id = await repo.create_action(
        ctx.thread_id, spec.name, args, summary, detail, seller_id=ctx.seller_id
    )
    return {
        "id": action_id,
        "tool_name": spec.name,
        "summary": summary,
        "detail": detail,
        "arguments": args,
        "status": repo.ACTION_PENDING,
        # 供界面选措辞：destructive = "不可撤销" / billable = "会产生费用"
        "kind": spec.confirm_kind,
    }


async def confirm(ctx: AssistantContext, action_id: int) -> dict[str, Any]:
    """卖家确认 → **此时才真正执行**，并把结果写回对话。"""
    action = await repo.get_action(action_id, ctx.seller_id)
    if action is None:
        raise AssistantError("action_not_found", f"待确认项不存在：{action_id}", 404)
    if action.get("status") != repo.ACTION_PENDING:
        raise AssistantError(
            "action_decided", f"该操作已被处理（当前状态：{action.get('status')}）", 409
        )
    spec = registry.get(str(action.get("tool_name") or ""))
    if spec is None:
        # 工具可能因版本变更被移除：明确作废，而不是留一个永远点不动的卡片
        await repo.decide_action(action_id, repo.ACTION_CANCELLED, "工具已不存在", ctx.seller_id)
        raise AssistantError("unknown_tool", f"工具已不存在：{action.get('tool_name')}", 409)

    args = dict(action.get("arguments") or {})
    try:
        result = await spec.handler(ctx, **args)
        content = result.content
        ok = result.ok
    except Exception as exc:  # noqa: BLE001 —— 执行异常不能吞：要让卖家看到失败
        logger.warning("确认后执行失败（%s）：%s: %s", spec.name, type(exc).__name__, exc)
        content = f"执行失败：{type(exc).__name__}: {exc}"
        ok = False

    if not await repo.decide_action(action_id, repo.ACTION_CONFIRMED, content, ctx.seller_id):
        raise AssistantError("action_decided", "该操作已被处理", 409)

    # 结果写回对话 —— 否则界面上会停在"等待确认"，卖家不知道到底成没成
    await repo.append_message(
        ctx.thread_id, "assistant", content,
        tool_name=spec.name, tool_args=args, seller_id=ctx.seller_id,
    )
    return {"id": action_id, "ok": ok, "content": content, "tool_name": spec.name}


async def cancel(ctx: AssistantContext, action_id: int) -> dict[str, Any]:
    """卖家取消 → 动作作废，**数据零改动**。"""
    action = await repo.get_action(action_id, ctx.seller_id)
    if action is None:
        raise AssistantError("action_not_found", f"待确认项不存在：{action_id}", 404)
    if action.get("status") != repo.ACTION_PENDING:
        raise AssistantError("action_decided", "该操作已被处理", 409)

    if not await repo.decide_action(action_id, repo.ACTION_CANCELLED, "卖家取消", ctx.seller_id):
        raise AssistantError("action_decided", "该操作已被处理", 409)

    content = f"已取消，未做任何改动。（{action.get('summary')}）"
    await repo.append_message(ctx.thread_id, "assistant", content, seller_id=ctx.seller_id)
    return {"id": action_id, "ok": True, "content": content}


async def list_pending(ctx: AssistantContext) -> list[dict[str, Any]]:
    """当前待卖家确认的动作（界面据此渲染确认卡）。"""
    rows = await repo.list_actions(ctx.thread_id, ctx.seller_id, status=repo.ACTION_PENDING)
    return [
        {
            "id": r["id"], "tool_name": r["tool_name"], "summary": r["summary"],
            "detail": r.get("detail") or "", "arguments": r.get("arguments") or {},
            "status": r["status"], "created_at": r.get("created_at"),
        }
        for r in rows
    ]
