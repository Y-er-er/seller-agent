"""待确认动作 —— 破坏性操作的**人在环**机制。

为什么它必须是服务端机制：模型会犯错，而删除、批量改价这类动作**不可逆**。
"确认"若只做在界面上的装饰，就等于没有 —— 本模块让它在服务端成立：
**标记为破坏性的工具，在卖家点头之前，它的执行函数根本不会被调用。**

流程：

```
模型提出破坏性工具
   → 循环「不执行」，落一条 pending_action（含影响明细 + 批次号）
   → 界面出一张确认卡（同一次请求的多个步骤**合成一张**）
   → 卖家确认 → 先 CAS 抢占（pending → running），抢到才真正执行 → 结果写回对话
   → 卖家取消 → 动作作废，数据零改动
```

两个不变量，缺一条都会出事（都实测踩过）：

1. **执行前必须抢占**。`pending` 只是"还没做"，不是"只有我能做" ——
   连点两下 / 两个标签页同时点，两个请求都会读到 pending；若都去执行，
   动作就做了两遍（生图重复计费、删除第二次报"不存在"）。所以是
   `claim → 执行 → finish`，不是 `读 → 执行 → 改状态`。
2. **一次请求 = 一次确认**。同一句要求里的多步动作共享 `batch_id`，
   由 `confirm_batch` 一次执行完、**只续跑一次**对话。逐张点会让卖家只做一半，
   还会触发多次续跑（重复计费 + 上下文互相污染）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.assistant import registry
from app.assistant.registry import AssistantContext, AssistantError
from app.db import assistant_repo as repo

logger = logging.getLogger(__name__)

#: "近期已完成"的时间窗（分钟）—— 超过这个时间的同身份动作允许重新提
RECENT_DONE_MINUTES = 30


def _ensure_registry() -> None:
    """确保工具注册表已就绪（幂等）。

    ⚠️ 不是洁癖：`confirm()` 用 `registry.get()` 判断工具是否存在，查不到就**作废这张卡**
    （防悬空卡）。若注册表还没初始化，一张好好的卡会被判成"工具已不存在"而永久作废 ——
    所以每个入口都先确保注册，不能依赖"恰好先调过一次对话"。
    """
    from app.assistant import loop as assistant_loop   # 延迟导入：loop 依赖本模块

    assistant_loop.ensure_tools_registered()


async def create_pending(
    ctx: AssistantContext, spec: registry.ToolSpec, args: dict[str, Any], *, batch_id: str = ""
) -> dict[str, Any]:
    """把一个破坏性/计费调用落成待确认项，返回确认卡数据。

    ⚠️ **本函数不执行工具** —— 这是整个机制的关键：执行发生在 `confirm()` 里。

    ⚠️ **幂等**：同工具 + 同参数若已有未决卡，直接复用，**不再新建**。
    否则模型反复调用同一工具（卖家重申一句要求就可能触发），会堆出好几张一样的卡；
    卖家逐张点 → 同一件事**重复执行、重复计费**（实测踩到过：一次生成文案点了 3 次）。

    `batch_id`：**同一次卖家请求**里的多个动作共用一个值，界面据此把它们收成
    **一张卡、一次确认**（见 `confirm_batch`）。复用的旧卡保留它原来的批次。
    """
    identity = spec.identity_key(args)

    # ---- ① 已有**同一件事**的待确认卡 → 复用（不新建）----
    # ⚠️ 比的是"身份键"而不是全参数：模型两次调用可能一次带 `style`、一次不带，
    #    全参数比对会判成两件事 → 同一商品冒出两张卡（实测踩到）。
    for row in await repo.list_actions(ctx.thread_id, ctx.seller_id, status=repo.ACTION_PENDING):
        if row.get("tool_name") != spec.name:
            continue
        if spec.identity_key(row.get("arguments") or {}) != identity:
            continue
        logger.info("复用已有待确认项 #%s（%s），未重复建卡", row.get("id"), spec.name)
        return {
            "id": row.get("id"),
            "tool_name": spec.name,
            "summary": row.get("summary") or spec.describe_action(args),
            "detail": row.get("detail") or "",
            "arguments": args,
            "status": repo.ACTION_PENDING,
            "kind": spec.confirm_kind,
            "confirm_note": spec.confirm_note,
            # 复用卡留在它原本的批次里 —— 否则界面会把它算进新批次（同一件事出现两次）
            "batch_id": row.get("batch_id") or batch_id,
            "reused": True,
        }

    # ---- ② 同一件事**刚刚已完成** → 不再建卡（续跑那轮模型会重复提交）----
    for row in await repo.list_actions(
        ctx.thread_id, ctx.seller_id, status=repo.ACTION_CONFIRMED, limit=30
    ):
        if row.get("tool_name") != spec.name:
            continue
        if spec.identity_key(row.get("arguments") or {}) != identity:
            continue
        decided = row.get("decided_at")
        if not isinstance(decided, datetime):
            continue
        if (datetime.now() - decided).total_seconds() > RECENT_DONE_MINUTES * 60:
            continue
        logger.info("该动作近期已完成（#%s），不再重复建卡", row.get("id"))
        return {
            "id": row.get("id"),
            "tool_name": spec.name,
            "summary": row.get("summary") or spec.describe_action(args),
            "detail": "",
            "arguments": args,
            "status": repo.ACTION_CONFIRMED,
            "kind": spec.confirm_kind,
            "batch_id": row.get("batch_id") or batch_id,
            "already_done": True,
            "result": row.get("result") or "",
        }

    summary = spec.describe_action(args)
    detail = ""
    if spec.impact_builder is not None:
        try:
            detail = await spec.impact_builder(ctx, args)
        except Exception as exc:  # noqa: BLE001 —— 预览失败不影响"停下来等确认"这件事
            logger.warning("生成影响预览失败（%s）：%s", spec.name, exc)

    action_id = await repo.create_action(
        ctx.thread_id, spec.name, args, summary, detail,
        seller_id=ctx.seller_id, batch_id=batch_id,
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
        "confirm_note": spec.confirm_note,
        "batch_id": batch_id,
    }


async def _run_action(
    ctx: AssistantContext, action: dict[str, Any], spec: registry.ToolSpec
) -> dict[str, Any]:
    """抢占并执行**一个**已落库的动作。返回执行结果（成功 / 失败都在返回值里）。

    ⚠️ **先抢占（pending → running）再执行**，不是"读完就执行"：
    连点两下 / 两个标签页同时点 时，两个请求都会读到 pending，
    若都去跑 handler，动作就**执行了两遍**（生图重复计费、删除第二次报不存在），
    最后只有一个能改状态、另一个报 409 —— 报错了，但事情已经做了两次。
    """
    action_id = int(action["id"])
    args = dict(action.get("arguments") or {})

    if not await repo.claim_action(action_id, ctx.seller_id):
        raise AssistantError("action_decided", "这项已被处理（可能你刚点过）", 409)

    result: Any = None
    try:
        result = await spec.handler(ctx, **args)
        content = result.content
        ok = result.ok
    except Exception as exc:  # noqa: BLE001 —— 执行异常不能吞：要让卖家看到失败
        logger.warning("确认后执行失败（%s）：%s: %s", spec.name, type(exc).__name__, exc)
        content = f"执行失败：{type(exc).__name__}: {exc}"
        ok = False

    # 失败的动作标 `failed`，**不能标 `confirmed`** ——
    # 否则"近期已完成"会把它当成"已办妥"，卖家再提一次时被判定"刚做过"而不给建卡，
    # 表现就是"失败了却再也试不了"（实测踩到）。
    decided_status = repo.ACTION_CONFIRMED if ok else repo.ACTION_FAILED
    if not await repo.finish_action(action_id, ctx.seller_id, decided_status, content):
        # 动作**已经执行了**，但状态没落下去（例如被别的进程改过）→ 必须留痕：
        # 这张卡会停在 running，30 分钟后由 expire_stale 标成失败，卖家可重新发起。
        logger.warning(
            "动作 #%s 已执行，但终态落库失败（停留 running）：%s", action_id, content[:120]
        )

    return {
        "id": action_id,
        "tool_name": spec.name,
        "summary": action.get("summary") or spec.describe_action(args),
        "ok": ok,
        "content": content,
        "data": getattr(result, "data", None) or {},
        "arguments": args,
    }


async def _warn_copy_not_applied(ctx: AssistantContext, one: dict[str, Any]) -> None:
    """兜底：`generate_copy` 只负责"生成"，写入靠续跑时模型继续调 `update_goods`。

    如果模型选择停手（没写），**如实告知** —— 不要留"生成了却没生效"的悬念。
    """
    if one.get("tool_name") != "generate_copy" or not one.get("ok"):
        return
    try:
        # ⚠️ 走 `tools_goods.service` 这个引用（而不是 `from app.goods import service`）：
        #    测试注入的假商品服务才能覆盖到这条校验，否则单测会偷偷连真库。
        from app.assistant import tools_goods

        copy_text = ""
        if isinstance(one.get("data"), dict):
            copy_text = str(one["data"].get("copy") or "").strip()
        goods = await tools_goods.service.find_by_no(
            ctx.seller_id, str(one.get("arguments", {}).get("goods_no") or "")
        )
        if goods is not None and copy_text and (goods.get("description") or "").strip() != copy_text:
            await repo.append_message(
                ctx.thread_id, "assistant",
                "（提醒）文案已生成，但**还没有写入商品** —— 要我写入吗？",
                seller_id=ctx.seller_id,
            )
    except Exception as exc:  # noqa: BLE001 —— 校验失败不影响已完成的生成
        logger.warning("校验文案是否已写入失败（忽略）：%s", exc)


def _render_results(results: list[dict[str, Any]]) -> str:
    """把批量执行结果拼成**一条**给卖家看的消息。

    ⚠️ 逐条追加 N 条消息会把对话刷屏，并且让"到底哪一步成了"更难看清 ——
    一步一行、标明成败，比 N 个气泡清楚。
    """
    lines: list[str] = []
    total = len(results)
    for index, one in enumerate(results, start=1):
        if one.get("skipped"):
            mark = "⏭"
        else:
            mark = "✅" if one.get("ok") else "⚠️"
        head = f"{mark} 第 {index} 步（共 {total} 步）· {one.get('summary') or one.get('tool_name')}"
        body = str(one.get("content") or "").strip()
        lines.append(head + ("\n" + body if body else ""))
    return "\n\n".join(lines)


async def confirm(ctx: AssistantContext, action_id: int) -> dict[str, Any]:
    """卖家确认**单张卡** → 此时才真正执行，并把结果写回对话。"""
    _ensure_registry()
    action = await repo.get_action(action_id, ctx.seller_id)
    if action is None:
        raise AssistantError("action_not_found", f"待确认项不存在：{action_id}", 404)
    if action.get("status") != repo.ACTION_PENDING:
        raise AssistantError(
            "action_decided", f"该操作已被处理（当前状态：{action.get('status')}）", 409
        )
    spec = registry.get(str(action.get("tool_name") or ""))
    if spec is None:
        # ⚠️ **不再作废这张卡**（2026-10-20 实测，代价很贵）：
        # 这个分支最常见的成因不是"工具被下线"，而是**进程里注册表还没跟上代码**
        # （改了后端没重启 / 重启后首个请求）。作废是不可恢复的：卡直接消失、无任何对话痕迹，
        # 卖家看到的是"我点了确认，然后什么都没有，任务中心也没有"。
        # 留卡 + 说清楚，重启或刷新后重试即可；真被下线的工具由 24 小时过期清理收口。
        await repo.append_message(
            ctx.thread_id, "assistant",
            "（系统提示）这一步**没有执行**：服务刚更新过，这个操作暂时还调不起来。"
            "请刷新页面后重试；如果还是不行，重新跟我说一遍要我做什么。",
            seller_id=ctx.seller_id,
        )
        raise AssistantError(
            "tool_not_ready",
            "这一步暂时无法执行（服务刚更新过，操作没运行）。请刷新后重试。",
            409,
        )

    one = await _run_action(ctx, action, spec)

    # 结果写回对话 —— 否则界面上会停在"等待确认"，卖家不知道到底成没成
    await repo.append_message(
        ctx.thread_id, "assistant", one["content"],
        tool_name=spec.name, tool_args=one["arguments"], seller_id=ctx.seller_id,
    )

    # **恢复对话**：让模型知道"它请求的操作已经完成"，从而继续完成卖家最初的要求。
    # 没有这一步，模型会永远停在中间态（例如生成完文案却不写入商品）。
    follow_up: dict[str, Any] | None = None
    try:
        from app.assistant import loop as assistant_loop

        follow_up = await assistant_loop.continue_after_actions(ctx, [one])
    except Exception as exc:  # noqa: BLE001 —— 续跑失败不能影响"操作已成功"这个事实
        logger.warning("确认后续跑对话失败（操作本身已成功）：%s: %s", type(exc).__name__, exc)

    await _warn_copy_not_applied(ctx, one)

    return {
        "id": action_id, "ok": one["ok"], "content": one["content"], "tool_name": spec.name,
        "follow_up": follow_up,
    }


async def confirm_batch(
    ctx: AssistantContext, batch_id: str, *, skip_ids: list[int] | None = None
) -> dict[str, Any]:
    """卖家确认**一整批**（一次请求里的多步）→ 逐项执行，**只续跑一次**对话。

    为什么要有它（实测踩到的三个连环问题，一次请求含「建商品 + 生图 + 写文案」）：

    1. **一次只点一张** → 卖家以为点了就都做了，其实只做了一步；
    2. 剩下的卡留在界面上，看起来像"又弹出来了" / "点了没反应"；
    3. 逐张点会触发**多次续跑**，每次都调模型（重复计费），而且后一次续跑可能
       再把前一步提交一遍 → 多余的卡。

    所以：**一批 = 一次确认 = 一轮执行 = 一次续跑**。

    `skip_ids`：卖家在卡上单独划掉的项 —— 只作废那几项，其余照常执行。
    """
    _ensure_registry()
    rows = await repo.list_batch(batch_id, ctx.seller_id, status=repo.ACTION_PENDING)
    if not rows:
        raise AssistantError(
            "batch_not_found", "这批操作已经处理过了（或已超时作废）", 409
        )

    skip = {int(x) for x in (skip_ids or [])}
    results: list[dict[str, Any]] = []
    for row in rows:
        action_id = int(row["id"])
        spec = registry.get(str(row.get("tool_name") or ""))
        if action_id in skip:
            await repo.decide_action(action_id, repo.ACTION_CANCELLED, "卖家选择跳过这一步", ctx.seller_id)
            results.append({
                "id": action_id, "tool_name": row.get("tool_name"),
                "summary": row.get("summary") or "", "ok": True, "skipped": True,
                "content": "已跳过，未做任何改动。",
            })
            continue
        if spec is None:
            # 同 `confirm()`：**不作废**。整批里只有这一步调不起来，
            # 其余照常执行；这一步留在待确认状态，重启/刷新后可重新点。
            results.append({
                "id": action_id, "tool_name": row.get("tool_name"),
                "summary": row.get("summary") or "", "ok": False,
                "content": "这一步没执行：服务刚更新过，操作暂时调不起来。刷新后重试即可。",
            })
            continue
        try:
            results.append(await _run_action(ctx, row, spec))
        except AssistantError as exc:
            # 被别的请求抢先执行 / 已处理 → 如实记成失败，不中断整批
            results.append({
                "id": action_id, "tool_name": row.get("tool_name"),
                "summary": row.get("summary") or "", "ok": False, "content": exc.message,
            })

    # 一次性写回一条汇总消息（而不是 N 条）
    await repo.append_message(
        ctx.thread_id, "assistant", _render_results(results), seller_id=ctx.seller_id,
    )

    # **只续跑一次** —— 整批已经执行完，让模型据此收尾 / 补完剩余步骤
    follow_up: dict[str, Any] | None = None
    try:
        from app.assistant import loop as assistant_loop

        follow_up = await assistant_loop.continue_after_actions(ctx, results)
    except Exception as exc:  # noqa: BLE001 —— 续跑失败不能影响"动作已执行"这个事实
        logger.warning("批量确认后续跑失败（动作本身已完成）：%s: %s", type(exc).__name__, exc)

    for one in results:
        await _warn_copy_not_applied(ctx, one)

    ok_count = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    fail_count = sum(1 for r in results if not r.get("ok"))
    return {
        "batch_id": batch_id,
        "ok": fail_count == 0,
        "items": results,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "skipped_count": sum(1 for r in results if r.get("skipped")),
        "follow_up": follow_up,
    }


async def cancel_batch(ctx: AssistantContext, batch_id: str) -> dict[str, Any]:
    """整批作废（数据零改动）。"""
    rows = await repo.list_batch(batch_id, ctx.seller_id, status=repo.ACTION_PENDING)
    if not rows:
        raise AssistantError("batch_not_found", "这批操作已经处理过了", 409)
    cancelled = 0
    for row in rows:
        if await repo.decide_action(int(row["id"]), repo.ACTION_CANCELLED, "卖家取消", ctx.seller_id):
            cancelled += 1
    content = f"已取消，未做任何改动（{cancelled} 项）。"
    await repo.append_message(ctx.thread_id, "assistant", content, seller_id=ctx.seller_id)
    return {"batch_id": batch_id, "ok": True, "cancelled": cancelled, "content": content}


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
    """当前待卖家确认的动作（界面据此渲染确认卡）。

    查询前先顺带清理**超时未处理**的项（24 小时）——
    否则卖家关掉页面不再回来时，那条卡会永远挂着、入口角标也永远亮着。

    ⚠️ 每项都补齐 `kind` / `confirm_note` / `batch_id`：
    确认卡要按「会产生费用」还是「不可撤销」选措辞，这两个信息只有注册表里有。
    实测踩到：`/actions` 拉回来的卡**缺 kind** → 连计费项也显示成"需要你确认"，
    卖家看不到"这一步要花钱"，点下去才发现扣费。
    """
    _ensure_registry()
    try:
        expired = await repo.expire_stale()
        if expired:
            logger.info("已作废 %d 个超时未处理的待确认项", expired)
    except Exception as exc:  # noqa: BLE001 —— 清理是旁路，失败不影响正常查询
        logger.warning("清理超时待确认项失败（忽略）：%s", exc)

    rows = await repo.list_actions(ctx.thread_id, ctx.seller_id, status=repo.ACTION_PENDING)
    out: list[dict[str, Any]] = []
    for r in rows:
        spec = registry.get(str(r.get("tool_name") or ""))
        out.append({
            "id": r["id"],
            "tool_name": r["tool_name"],
            "summary": r["summary"],
            "detail": r.get("detail") or "",
            "arguments": r.get("arguments") or {},
            "status": r["status"],
            "created_at": r.get("created_at"),
            "kind": spec.confirm_kind if spec else "",
            "confirm_note": spec.confirm_note if spec else "",
            "destructive": bool(spec.destructive) if spec else False,
            "batch_id": r.get("batch_id") or "",
        })
    return out


# ============================================================
# 批量计划进度 + 失败重试（P1「意图/执行解耦」）
# ============================================================


async def batch_progress(ctx: AssistantContext, batch_id: str) -> dict[str, Any]:
    """查一个批次的进度（按状态计数）。"""
    return await repo.batch_progress(batch_id, ctx.seller_id)


async def recent_batches(ctx: AssistantContext, *, limit: int = 5) -> list[dict[str, Any]]:
    """最近若干批次各自的进度（给模型汇报用，不要求它记住 batch_id）。"""
    return await repo.recent_batches(ctx.seller_id, limit=limit)


async def retry_failed(ctx: AssistantContext, batch_id: str) -> dict[str, Any]:
    """把一批里**失败**的步骤重置回待确认，让卖家重新确认执行。

    为什么不直接执行：失败项可能含计费工具（生图），重试会再花一次钱 ——
    按「计费需点头」红线，重置后仍需卖家再点一次确认，走既有 `confirm_batch`。
    """
    reset = await repo.reset_failed(batch_id, ctx.seller_id)
    if reset == 0:
        raise AssistantError("nothing_to_retry", "这批里没有失败的步骤可重试", 409)
    try:
        await repo.append_message(
            ctx.thread_id, "assistant",
            f"已把 {reset} 步失败的步骤重新放回待确认，请你在卡片上再确认一次。",
            seller_id=ctx.seller_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 消息落库失败不影响"已重置"这个事实
        logger.warning("重试失败项后写回对话失败（忽略）：%s", exc)
    return {"batch_id": batch_id, "ok": True, "reset": reset}
