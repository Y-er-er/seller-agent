"""运维域的助手工具：**任务 / 操作日志 / 撤销**。

为什么单独成域，而不是塞进商品域：

这三样不是"商品"也不是"售后"，而是**跨域的可观测与补救手段**。
助手必须能回答「我的图生成好没有」「我上一步干了什么」「上一步能不能退回去」——
否则它是在黑箱里操作，出了问题也无从解释。

⚠️ 这三个能力此前**只有 HTTP 端点、没进注册表**（同类漏接已发生过三次），
本次补齐的同时新增了 `tests/test_tool_coverage.py` 做自动比对，防止再漏。
"""

from __future__ import annotations

from typing import Any

from app.assistant.registry import (
    DOMAIN_OPS,
    AssistantContext,
    ToolResult,
    ToolSpec,
    register,
)
from app.db import goods_repo
from app.db import task_repo
from app.goods import service
from app.goods.service import GoodsError

_TASK_STATUS = {
    "queued": "排队中", "running": "执行中", "done": "已完成",
    "failed": "失败", "interrupted": "已中断",
}

_ACTOR = {"human": "卖家", "ai": "AI", "system": "系统"}


def _fail(message: str) -> ToolResult:
    return ToolResult(content=f"操作未成功：{message}", ok=False)


# ============================================================
# 任务
# ============================================================


async def _retry_task(ctx: AssistantContext, task_id: int) -> ToolResult:
    """重试失败任务（仅白名单类型；当前只支持 AI 生图）。"""
    from app.goods.service import GoodsError
    from app.tasks import retry as retry_mod

    try:
        out = await retry_mod.retry(ctx.seller_id, int(task_id))
    except GoodsError as exc:
        return _fail(exc.message)

    return ToolResult(
        f"已重新提交：{out.get('title') or '任务'}（新任务 #{out.get('id')}），"
        f"进度可以在任务中心看。",
        {"task_id": out.get("id"), "title": out.get("title")},
    )


async def _scan_issues(ctx: AssistantContext, explain: bool = False) -> ToolResult:
    """经营巡检（`explain=True` 时只说明检查项，不执行扫描）。"""
    from app.assistant import scan as scanner

    if explain:
        info = scanner.describe_rules()
        rules = "\n".join(f"· {r['label']}：{r['rule']}（{r['level']}）" for r in info["rules"])
        gaps = "\n".join(f"· {x}" for x in info["not_covered"])
        return ToolResult(
            f"我会检查这 {info['count']} 类问题：\n{rules}\n\n"
            f"**不检查**（这些要靠你自己判断）：\n{gaps}",
            info,
        )

    result = await scanner.scan(ctx.seller_id)
    if not result["count"]:
        return ToolResult("巡了一遍，没有需要你处理的事。", result)

    names = {
        "low_stock": "低库存", "draft_incomplete": "草稿不完整",
        "aftersale_waiting": "售后待接手", "task_failed": "任务失败",
        "trash_expiring": "回收站到期",
    }
    lines: list[str] = []
    for item in result["issues"][:12]:
        flag = "⚠️" if item["level"] == "high" else "·"
        lines.append(f"{flag} [{names.get(item['kind'], item['kind'])}] "
                     f"{item['title']}（{item['target']}）—— {item['detail']}")

    tail = ("\n\n（我**不会自动改任何东西**。你说要处理哪件，我再动手；"
            "涉及改数据或产生费用的操作，仍然会让你确认。）")
    return ToolResult(
        f"扫描到 {result['count']} 件事需要你关注：\n" + "\n".join(lines) + tail, result
    )


async def _list_tasks(ctx: AssistantContext, status: str = "", limit: int = 10) -> ToolResult:
    items = await task_repo.list_tasks(
        ctx.seller_id, status=status or "", limit=min(max(int(limit or 10), 1), 30)
    )
    if not items:
        return ToolResult("目前没有任务记录。", {"count": 0})

    lines: list[str] = []
    for t in items:
        line = f"#{t['id']} [{_TASK_STATUS.get(t['status'], t['status'])}] {t['title']}"
        if t["status"] in ("queued", "running"):
            line += f" · 进行中 {t.get('progress') or 0}%（{t.get('stage') or '排队中'}）"
        elif t.get("result"):
            line += f" · {t['result']}"
        if t.get("error"):
            line += f" · 失败原因：{t['error']}"
        lines.append(line)

    running = [t for t in items if t["status"] in ("queued", "running")]
    head = f"最近 {len(items)} 个任务" + (f"（{len(running)} 个进行中）" if running else "")
    return ToolResult(head + "：\n" + "\n".join(lines), {"count": len(items), "items": items})


# ============================================================
# 批量计划进度（P1）
# ============================================================


async def _list_batch_status(ctx: AssistantContext, limit: int = 5) -> ToolResult:
    """查**批量计划**（`plan_batch` 提交的）的进度。

    与 `list_tasks`（后台异步任务：生图等）不同：这里查的是「一次确认执行整批」的
    待确认动作进度 —— 卖家问「刚才那批上架成了几件 / 失败了几件」时用它。
    """
    from app.assistant import actions

    try:
        batches = await actions.recent_batches(ctx, limit=min(max(int(limit or 5), 1), 10))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"查询批量计划进度失败：{type(exc).__name__}")
    if not batches:
        return ToolResult("目前没有批量计划的进度记录。", {"count": 0})

    lines: list[str] = []
    for b in batches:
        parts = [f"共 {b['total']} 步"]
        if b["pending"]:
            parts.append(f"{b['pending']} 待确认")
        if b["running"]:
            parts.append(f"{b['running']} 执行中")
        if b["confirmed"]:
            parts.append(f"{b['confirmed']} 成功")
        if b["failed"]:
            parts.append(f"{b['failed']} 失败（可重试）")
        if b["cancelled"]:
            parts.append(f"{b['cancelled']} 已取消")
        lines.append("，".join(parts))
    return ToolResult(
        f"最近 {len(batches)} 个批量计划：\n" + "\n".join(lines),
        {"count": len(batches), "items": batches},
    )


# ============================================================
# 操作日志
# ============================================================


async def _list_logs(
    ctx: AssistantContext, actor: str = "", target_id: str = "", limit: int = 10
) -> ToolResult:
    items = await goods_repo.list_operations(
        ctx.seller_id, actor=actor or "", target_id=target_id or "",
        limit=min(max(int(limit or 10), 1), 30),
    )
    if not items:
        return ToolResult("没有符合条件的操作记录。", {"count": 0})

    lines: list[str] = []
    for log in items:
        undoable = log.get("undoable") and not log.get("undone_at")
        line = (f"#{log['id']} {str(log.get('created_at') or '')[:16]} "
                f"{_ACTOR.get(log.get('actor'), log.get('actor') or '?')} · "
                f"{log.get('summary') or log.get('action')}")
        if undoable:
            line += "  ← 可撤销（undo_log）"
        elif log.get("undone_at"):
            line += "  （已撤销过）"
        lines.append(line)

    return ToolResult(
        f"最近 {len(items)} 条操作记录：\n" + "\n".join(lines)
        + "\n（需要撤销某条时，调用 undo_log 并传它的 #编号）",
        {"count": len(items), "items": items},
    )


# ============================================================
# 撤销
# ============================================================


async def _undo_log(ctx: AssistantContext, log_id: int) -> ToolResult:
    try:
        out = await service.undo_operation(ctx.seller_id, int(log_id), actor=goods_repo.ACTOR_AI)
    except GoodsError as exc:
        return _fail(exc.message)
    summary = (out or {}).get("summary") or "已回滚到操作前的状态"
    return ToolResult(f"已撤销操作 #{log_id}：{summary}", out if isinstance(out, dict) else {})


# ============================================================
# 注册
# ============================================================

_REGISTERED: set[str] = set()


def register_all() -> None:
    """注册运维域工具（幂等）。"""
    specs = [
        ToolSpec(
            name="retry_task", domain=DOMAIN_OPS, billable=True,
            summary_template="重试失败任务 #{task_id}",
            description=(
                "重试一个**失败的**任务（当前只支持 AI 生图；批量操作类不支持，会说明原因）。"
                "⚠️ **会产生费用**（生图按张计费），需卖家确认后才执行。"
                "卖家说「重试一下刚才失败的生图」时用它。"
            ),
            parameters={"type": "object", "properties": {
                "task_id": {"type": "integer", "description": "任务 id（任务中心可以看到）"},
            }, "required": ["task_id"]},
            handler=_retry_task,
            # 同一个任务只该有一张待确认卡
            idempotent_by=("task_id",),
        ),
        ToolSpec(
            name="scan_issues", domain=DOMAIN_OPS,
            description=(
                "**经营巡检**：一次扫出所有需要卖家处理的事（低库存 / 草稿不完整 / 售后待接手 / "
                "任务失败 / 回收站到期）。卖家问「有什么要处理的」「我该做什么」「有没有问题」时用它。"
                "**只读，不会执行任何操作。**"
            ),
            parameters={"type": "object", "properties": {
                "explain": {"type": "boolean",
                            "description": "true = 只说明会检查哪些问题（不执行扫描）；"
                                           "卖家问「你都能检查什么」「巡检规则是什么」时用"},
            }, "required": []},
            handler=_scan_issues,
        ),
        ToolSpec(
            name="list_tasks", domain=DOMAIN_OPS,
            description=(
                "查看最近的任务：AI 生图 / 生成文案 / 定价 / 批量操作都在这里，含进度、结果与失败原因。"
                "卖家问「我的图生成好了吗」「刚才那批操作成了几件」时用它。只读。"
            ),
            parameters={"type": "object", "properties": {
                "status": {"type": "string",
                           "enum": ["queued", "running", "done", "failed", "interrupted"],
                           "description": "按状态筛选（可选）"},
                "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30"},
            }, "required": []},
            handler=_list_tasks,
        ),
        ToolSpec(
            name="list_batch_status", domain=DOMAIN_OPS,
            description=(
                "查看**批量计划**（用 plan_batch 提交的）的进度：每个批次共几步、"
                "多少成功 / 失败 / 待确认。卖家问「刚才那批做得怎么样」「批量操作成了几件、"
                "失败了几件」时用它。只读。"
            ),
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer", "description": "返回批次数，默认 5，最多 10"},
            }, "required": []},
            handler=_list_batch_status,
        ),
        ToolSpec(
            name="list_logs", domain=DOMAIN_OPS,
            description=(
                "查看最近的操作记录（人工与 AI 都记），含「哪些操作可以撤销」。"
                "卖家问「我上一步干了什么」「刚才谁改的」时用它。只读。"
            ),
            parameters={"type": "object", "properties": {
                "actor": {"type": "string", "enum": ["human", "ai", "system"],
                          "description": "按来源筛选（可选）"},
                "target_id": {"type": "string", "description": "按对象筛选（如商品编号 G-0001）"},
                "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30"},
            }, "required": []},
            handler=_list_logs,
        ),
        ToolSpec(
            name="undo_log", domain=DOMAIN_OPS, destructive=True,
            summary_template="撤销操作 #{log_id}",
            description=(
                "按操作记录的 before 快照撤销某次改动（仅字段级修改可撤销，删除不可撤销）。"
                "**会改变数据，需要卖家确认。** 先调用 list_logs 拿到可撤销的 #编号。"
            ),
            parameters={"type": "object", "properties": {
                "log_id": {"type": "integer", "description": "操作记录编号（list_logs 返回的 #号）"},
            }, "required": ["log_id"]},
            handler=_undo_log,
        ),
    ]
    for spec in specs:
        if spec.name in _REGISTERED:
            continue
        register(spec)
        _REGISTERED.add(spec.name)
