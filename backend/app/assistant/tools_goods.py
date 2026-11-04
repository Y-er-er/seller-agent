"""商品域与陈列域的助手工具。

两条约定：

1. **工具不吞错误**：业务失败会把明确原因写进 `ToolResult.content`（如"上架失败：必须设置大于 0 的价格"），
   让模型**如实转达**，而不是让模型自己编一个模糊的说法。
2. **破坏性工具只声明、不执行**：`destructive=True` 的工具由循环交给待确认机制，
   这里只负责"如果被执行，该做什么"（见 `app/assistant/actions.py`）。
"""

from __future__ import annotations

from typing import Any

from app.assistant.registry import (
    DOMAIN_DISPLAY,
    DOMAIN_GOODS,
    AssistantContext,
    ToolResult,
    ToolSpec,
    register,
)
from app.db import goods_repo as repo
from app.goods import service
from app.goods.service import GoodsError

_STATUS_LABELS = {"draft": "草稿", "on_shelf": "已上架", "off_shelf": "已下架"}


def _p(value: Any) -> str:
    return f"¥{float(value or 0):.2f}"


def _line(g: dict[str, Any]) -> str:
    return (f"{g['goods_no']} · {g['title']} · {_p(g['price'])} · "
            f"{_STATUS_LABELS.get(g['status'], g['status'])} · 库存 {g['stock']}")


def _fail(message: str) -> ToolResult:
    return ToolResult(content=f"操作未成功：{message}", ok=False)


# ============================================================
# 读
# ============================================================


async def _list_goods(
    ctx: AssistantContext, keyword: str = "", status: str = "", low_stock: bool = False,
    category: str = "", price_min: float | None = None, price_max: float | None = None,
    stock_min: int | None = None, stock_max: int | None = None,
    sort: str = "", order: str = "asc",
    created_after: str = "", created_before: str = "",
) -> ToolResult:
    items = await repo.list_goods(
        ctx.seller_id, scope="all", q=keyword or "", status=status or "",
        low_stock_only=bool(low_stock), limit=50,
        category=category or "",
        price_min=price_min, price_max=price_max,
        stock_min=stock_min, stock_max=stock_max,
        sort=sort or "", order=order or "asc",
        created_after=created_after or "", created_before=created_before or "",
    )
    if not items:
        # ⚠️ 「搜不到」有两种，绝不能混成一句（否则就是对卖家说假话）：
        #   ① 关键词没命中 —— 库里可能有，只是这个词匹配不上；
        #   ② 店里确实没有商品 —— 只有**不带关键词**还查空时才成立。
        # 2026-10-21 实测：卖家问「英伟达5090显卡」，库里那件叫「英伟达RTX5090显卡」，
        # 工具回了句笼统的「没有符合条件的商品」，模型据此刻断言「店里没有售卖这款商品」。
        terms = repo.keyword_terms(keyword or "")
        if not terms:
            return ToolResult(
                "店里目前**一件商品都没有**（没有可展示的商品记录）。",
                {"count": 0, "keyword": keyword or "", "terms": []},
            )
        tried = "、".join(f"「{t}」" for t in terms)
        return ToolResult(
            f"按关键词 {tried} 检索，**没有匹配到任何商品**。\n"
            f"⚠️ 这只说明**这个词没命中**，**不等于店里没有这件商品** —— "
            f"请先换一种查法再下结论：去掉修饰词（只留品牌或型号数字），或用 `list_goods` "
            f"不带关键词看一下全部商品。**在你自己再查一次之前，绝对不要对卖家说"
            f"「店里没有这件商品」。**",
            {"count": 0, "keyword": keyword or "", "terms": terms},
        )
    body = "\n".join(_line(g) for g in items)
    return ToolResult(
        f"共 {len(items)} 件商品：\n{body}",
        {"count": len(items), "items": [
            {"goods_no": g["goods_no"], "title": g["title"], "price": g["price"],
             "status": g["status"], "stock": g["stock"], "category": g.get("category") or ""}
            for g in items
        ]},
    )


async def _get_goods(ctx: AssistantContext, goods_no: str) -> ToolResult:
    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    media = len(goods.get("media") or [])
    return ToolResult(
        f"{goods['goods_no']} · {goods['title']}\n"
        f"价格 {_p(goods['price'])} · 成本 {_p(goods['cost_price'])} · 库存 {goods['stock']}"
        f"（预警 {goods['stock_alert']}）· 类目 {goods.get('category') or '未填'} · "
        f"{_STATUS_LABELS.get(goods['status'], goods['status'])} · 图片 {media} 张\n"
        f"文案：{goods.get('description') or '（暂无）'}",
        {"goods": {k: goods.get(k) for k in
                   ("goods_no", "title", "price", "cost_price", "stock", "stock_alert", "status", "category")}},
    )


async def _list_categories(ctx: AssistantContext) -> ToolResult:
    """现有类目清单 —— 建档前判断"能否归入现有类目"。"""
    items = await repo.list_categories(ctx.seller_id)
    if not items:
        return ToolResult(
            "目前还没有任何类目（商品都没分类）。可以按商品特点新建一个贴切的类目名。",
            {"count": 0, "items": []},
        )
    body = "\n".join(f"- {c['category']}（{c['count']} 件）" for c in items)
    return ToolResult(
        f"现有 {len(items)} 个类目：\n{body}\n"
        f"（建商品时**优先归入其中之一**；确实都不合适，才新建一个类目名）",
        {"count": len(items), "items": items},
    )


async def _goods_aftersale(ctx: AssistantContext, goods_no: str) -> ToolResult:
    """商品的售后统计（**商品 × 售后联动**）—— 端点早就有，此前一直没接进注册表。"""
    from app.goods import aftersale_stats

    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        stats = await aftersale_stats.stats_for_goods(ctx.seller_id, int(goods["id"]))
    except GoodsError as exc:
        return _fail(exc.message)

    if not stats.get("total"):
        return ToolResult(
            f"「{goods['title']}」暂无关联的售后工单（订单需带商品编号才会关联）。", stats
        )
    rate = stats.get("refund_rate")
    outcomes = " · ".join(f"{k} {v}" for k, v in (stats.get("outcomes") or {}).items())
    return ToolResult(
        f"「{goods['title']}」售后情况：共 {stats['total']} 单"
        f"（退货率 {'—' if rate is None else str(rate) + '%'}，"
        f"转人工 {stats.get('escalated', 0)} 单，处理中 {stats.get('processing', 0)} 单）。"
        f"结果分布：{outcomes or '—'}",
        stats,
    )


async def _list_trash_goods(ctx: AssistantContext) -> ToolResult:
    """回收站里的商品（恢复之前先看看有什么）。"""
    items = await service.list_trash(ctx.seller_id)
    if not items:
        return ToolResult("回收站是空的。", {"count": 0})
    lines = [
        f"{g['goods_no']} · {g['title']} · {_p(g['price'])}"
        + (f" · 剩余 {g['days_left']} 天可恢复" if g.get("days_left") is not None else "")
        for g in items
    ]
    return ToolResult(
        f"回收站里有 {len(items)} 件商品：\n" + "\n".join(lines),
        {"count": len(items),
         "items": [{"goods_no": g["goods_no"], "title": g["title"]} for g in items]},
    )


async def _restore_goods(ctx: AssistantContext, goods_no: str) -> ToolResult:
    """从回收站恢复商品（删除改成软删之后，AI 也必须有"捞回来"的能力）。"""
    goods = await service.find_by_no(ctx.seller_id, goods_no, include_deleted=True)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品（回收站里也没有）")
    if not goods.get("deleted_at"):
        return ToolResult(f"「{goods['title']}」不在回收站里，无需恢复。", {"goods_no": goods_no})
    try:
        out = await service.restore_goods(ctx.seller_id, int(goods["id"]), actor=repo.ACTOR_AI)
    except GoodsError as exc:
        return _fail(exc.message)
    status_text = _STATUS_LABELS.get(str(out.get("status") or ""), str(out.get("status") or ""))
    return ToolResult(
        f"已把「{goods['title']}」从回收站恢复（原状态：{status_text}）。",
        out,
    )


# ============================================================
# 写（非破坏性）
# ============================================================


async def _create_goods(
    ctx: AssistantContext, title: str, price: float = 0, description: str = "",
    category: str = "", stock: int = 0, stock_alert: int = 0,
) -> ToolResult:
    try:
        goods = await service.create_goods(
            ctx.seller_id, title=title, price=price, description=description,
            category=category, stock=stock, stock_alert=stock_alert,
            actor=repo.ACTOR_AI, status=repo.STATUS_DRAFT,
        )
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(
        f"已新建商品草稿：{goods['goods_no']} · {goods['title']}。"
        f"当前为**草稿**状态，需补齐价格与文案后才能上架。",
        {"goods_no": goods["goods_no"], "id": goods["id"]},
    )


async def _update_goods(
    ctx: AssistantContext, goods_no: str, title: str | None = None, description: str | None = None,
    price: float | None = None, category: str | None = None,
    stock: int | None = None, stock_alert: int | None = None,
) -> ToolResult:
    fields: dict[str, Any] = {}
    # ⚠️ 用 `is not None` 而非真值判断：价格设为 0 是合法意图，不能被当成"没传"
    for key, value in (("title", title), ("description", description), ("price", price),
                       ("category", category), ("stock", stock), ("stock_alert", stock_alert)):
        if value is not None:
            fields[key] = value
    if not fields:
        return _fail("没有提供任何要修改的字段")
    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        updated = await service.update_goods(ctx.seller_id, goods["id"], actor=repo.ACTOR_AI, **fields)
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(f"已更新 {updated['goods_no']}：{_line(updated)}", {"goods_no": updated["goods_no"]})


async def _set_status(ctx: AssistantContext, goods_no: str, status: str) -> ToolResult:
    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        updated = await service.set_status(ctx.seller_id, goods["id"], status, actor=repo.ACTOR_AI)
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(
        f"「{updated['title']}」已{'上架' if status == 'on_shelf' else '下架' if status == 'off_shelf' else '转回草稿'}。",
        {"goods_no": updated["goods_no"], "status": updated["status"]},
    )


async def _reorder(ctx: AssistantContext, goods_nos: list[str]) -> ToolResult:
    ids: list[int] = []
    for no in goods_nos or []:
        goods = await service.find_by_no(ctx.seller_id, no)
        if goods is None:
            return _fail(f"找不到编号为 {no} 的商品")
        ids.append(int(goods["id"]))
    if not ids:
        return _fail("没有提供要排序的商品编号")
    try:
        result = await service.reorder(ctx.seller_id, ids, actor=repo.ACTOR_AI)
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(f"已按你给的顺序调整 {result['updated']} 件商品的陈列位置。", result)


async def _set_pinned(ctx: AssistantContext, goods_no: str, pinned: bool) -> ToolResult:
    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    updated = await service.update_goods(
        ctx.seller_id, goods["id"], actor=repo.ACTOR_AI, pinned=bool(pinned)
    )
    return ToolResult(
        f"「{updated['title']}」已{'置顶' if pinned else '取消置顶'}。",
        {"goods_no": updated["goods_no"], "pinned": updated["pinned"]},
    )


# ============================================================
# 写（**破坏性**，只声明不执行）
# ============================================================


async def _delete_goods(ctx: AssistantContext, goods_no: str) -> ToolResult:
    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    result = await service.delete_goods(ctx.seller_id, goods["id"], actor=repo.ACTOR_AI)
    days = int(result.get("restore_days") or 0)
    return ToolResult(
        f"已删除「{goods['title']}」（{goods['goods_no']}）→ **移入回收站**"
        + (f"，{days} 天内可恢复" if days > 0 else "，可恢复")
        + "。图片也一并保留着。",
        {"goods_no": goods["goods_no"], **result},
    )


async def _bulk_apply(
    ctx: AssistantContext, goods_nos: list[str], action: str, price_delta: float = 0
) -> ToolResult:
    ids: list[int] = []
    for no in goods_nos or []:
        goods = await service.find_by_no(ctx.seller_id, no)
        if goods is None:
            return _fail(f"找不到编号为 {no} 的商品")
        ids.append(int(goods["id"]))
    if not ids:
        return _fail("没有提供要处理的商品编号")
    try:
        result = await service.bulk_action(
            ctx.seller_id, ids, action, price_delta=float(price_delta or 0), actor=repo.ACTOR_AI
        )
    except GoodsError as exc:
        return _fail(exc.message)
    done, failed = len(result.get("done") or []), len(result.get("failed") or [])
    text = f"批量处理完成：成功 {done} 件" + (f"，失败 {failed} 件" if failed else "")
    if failed:
        text += "；失败明细：" + "；".join(f"{f['goods_id']} {f['error']}" for f in result["failed"][:5])
    return ToolResult(text, result, ok=not failed)


# ============================================================
# 写（**计费**，只声明不执行）
# ============================================================
#
# 与 `destructive` 的区别：这些动作**不改变数据**（生图是追加、文案与定价只是候选），
# 但**会产生 API 费用**。所以同样要卖家点头 —— 只是确认卡上的理由写"会产生费用"而非"不可撤销"。
#
# ⚠️ 这三件事此前只有 HTTP 端点、没进注册表，导致助手只能说"系统不支持直接生成图片"。
#    这正是那条设计原则的反面案例：**加能力 ≠ 加端点，而是要往注册表里加工具。**


async def _generate_cover(ctx: AssistantContext, goods_no: str, hint: str = "") -> ToolResult:
    from app.goods import image_ai

    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        task = await image_ai.generate_cover(ctx.seller_id, int(goods["id"]), hint=hint or "")
    except GoodsError as exc:
        return _fail(exc.message)
    # ⚠️ 生图已改成**后台任务**：这里必须说"已开始、还没完成"，绝不能谎报已生成
    return ToolResult(
        f"已把「{goods['title']}」的宣传图生成放到后台了（任务 #{task.get('id')}，约 30—60 秒）。"
        f"**现在还没完成** —— 可以在左栏「任务」里看进度，完成后图片会自动加到商品。",
        task,
    )


async def _generate_copy(
    ctx: AssistantContext, goods_no: str, style: str = "口语", apply: bool = False
) -> ToolResult:
    from app.goods import ai as goods_ai

    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        out = await goods_ai.generate_copy(ctx.seller_id, int(goods["id"]), style)
    except GoodsError as exc:
        return _fail(exc.message)

    copy_text = str(out.get("copy") or "").strip()

    # apply=true：生成后**直接写回商品**（「生成并写入」一步原子完成）。
    # 为什么要有这一步（2026-10-21 P2）：旧设计靠确认后的模型续跑再调 update_goods 写回，
    # 是**非确定性**的 —— 模型可能停手不写（于是要 `_warn_copy_not_applied` 兜底）、可能重复写，
    # 数十件商品时续跑写回还会再次撞单轮预算。写入本该是计划里确定性的一步，不是靠模型自觉。
    if apply:
        try:
            await service.update_goods(
                ctx.seller_id, goods["id"], actor=repo.ACTOR_AI, description=copy_text,
            )
        except GoodsError as exc:
            return _fail(f"文案已生成，但写入商品失败：{exc.message}")
        return ToolResult(
            f"为「{goods['title']}」生成了文案并已写入商品详情：\n{copy_text}",
            out,
        )

    # apply=false：只生成候选（写入与否由卖家后续决定）
    return ToolResult(
        f"为「{goods['title']}」生成了文案：\n{copy_text}",
        out,
    )


async def _suggest_price(ctx: AssistantContext, goods_no: str) -> ToolResult:
    from app.goods import ai as goods_ai

    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        out = await goods_ai.suggest_price(ctx.seller_id, int(goods["id"]))
    except GoodsError as exc:
        return _fail(exc.message)
    source = "规则估算（模型未参与）" if out.get("source") == "rule_fallback" else "模型建议"
    return ToolResult(
        f"「{goods['title']}」定价建议：¥{out['suggested']:.2f}"
        f"（区间 ¥{out['min']:.2f}—¥{out['max']:.2f}，来源：{source}）。理由：{out['reason']}\n"
        f"（这**只是建议，没有改价**；采纳时调用 update_goods）",
        out,
    )


# ============================================================
# 影响预览（确认卡展开显示"到底会动什么"）
# ============================================================


async def _impact_delete(ctx: AssistantContext, args: dict[str, Any]) -> str:
    goods = await service.find_by_no(ctx.seller_id, str(args.get("goods_no") or ""))
    if goods is None:
        return "该商品已不存在，确认后将直接失败。"
    media = len(goods.get("media") or [])
    return (f"将删除「{goods['title']}」（{goods['goods_no']}，{_p(goods['price'])}）"
            + (f"及 {media} 张图片" if media else "（无图片）")
            + "。删除后无法恢复。")


_ACTION_LABELS = {"on_shelf": "上架", "off_shelf": "下架", "delete": "删除", "price_delta": "调价"}


async def _impact_bulk(ctx: AssistantContext, args: dict[str, Any]) -> str:
    nos = args.get("goods_nos") or []
    action = str(args.get("action") or "")
    delta = float(args.get("price_delta") or 0)
    lines: list[str] = []
    media_total = 0
    for no in nos[:8]:
        goods = await service.find_by_no(ctx.seller_id, str(no))
        if goods is None:
            lines.append(f"{no}：已不存在")
            continue
        media_total += len(goods.get("media") or [])
        if action == "price_delta":
            lines.append(f"{goods['title']}：{_p(goods['price'])} → {_p(float(goods['price']) + delta)}")
        else:
            lines.append(f"{goods['title']}（{_p(goods['price'])}）")
    head = f"将对 {len(nos)} 件商品执行「{_ACTION_LABELS.get(action, action)}」"
    if action == "price_delta":
        head += f"（每件 {delta:+.2f} 元）"
    if action == "delete" and media_total:
        head += f"，并删除 {media_total} 张图片"
    return head + "：\n" + "\n".join(lines)


# ============================================================
# 批量计划（P0「意图/执行解耦」入口）
# ============================================================


async def _plan_batch(ctx: AssistantContext, items: list[dict[str, Any]]) -> ToolResult:
    """把一批「工具意图」打包成**一次计划**落地。

    为什么需要它（2026-10-21 实测根因）：模型默认「边想边做」，N 件商品 × 每件多步
    （建品/生图/文案/上架）= N×M 次工具调用，撞上单轮预算/步数上限（4 件商品就把
    预算 8 打穿，后续确认卡全被拦，回复只能空口承诺「马上接着处理」）。

    本工具把 N×M 次调用**压缩成 1 次**：
    - 确认类动作（needs_confirm）→ 展开成**同一批** `pending_action`（同 batch_id），
      界面合成一张卡、卖家一次确认 —— 执行仍走既有 `confirm_batch`（确定性 + CAS）；
    - 非确认动作 → 直接执行。

    于是「数十件商品」的意图不再受单轮预算约束，与执行彻底解耦。

    防重复：同一轮内「同工具 + 同参数」的非确认写操作只执行一次。
    """
    import json as _json
    import uuid as _uuid

    from app.assistant import actions, registry

    batch_id = f"b{_uuid.uuid4().hex[:12]}"
    done: list[str] = []
    waiting: list[str] = []
    failures: list[str] = []
    pending_count = 0
    billable_count = 0
    seen: set[str] = set()

    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            failures.append(f"第 {index + 1} 步：格式不对，已跳过")
            continue
        tool_name = str(item.get("tool") or "").strip()
        args = item.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        spec = registry.get(tool_name)
        if spec is None:
            failures.append(f"第 {index + 1} 步：不存在「{tool_name}」这个操作，已跳过")
            continue

        if spec.needs_confirm:
            try:
                card = await actions.create_pending(ctx, spec, args, batch_id=batch_id)
            except Exception as exc:  # noqa: BLE001 —— 单项落卡失败不中断整批
                failures.append(f"第 {index + 1} 步：{spec.name} 落卡失败：{type(exc).__name__}")
                continue
            if card.get("already_done"):
                done.append(card.get("summary") or tool_name)
            else:
                waiting.append(card.get("summary") or tool_name)
                pending_count += 1
                if spec.billable:
                    billable_count += 1
            continue

        # 非确认动作：直接执行；同一轮内同工具+同参数只执行一次（防重复写 / 重复计费）
        key = f"{tool_name}|{_json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        if key in seen:
            continue
        seen.add(key)
        try:
            result = await spec.handler(ctx, **args)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"第 {index + 1} 步：{tool_name} 执行出错：{type(exc).__name__}")
            continue
        if getattr(result, "ok", True):
            done.append(spec.describe_action(args))
        else:
            failures.append(f"第 {index + 1} 步：{result.content}")

    lines: list[str] = []
    if done:
        lines.append(f"已直接完成 {len(done)} 步。")
    if waiting:
        lines.append(
            f"另有 {pending_count} 步已生成待确认计划（其中 {billable_count} 步会产生费用），"
            f"合并成一张卡等卖家确认。"
        )
    if failures:
        lines.append("未成功：" + "；".join(failures))
    if not lines:
        lines.append("计划为空，没有可执行的操作。")

    content = " ".join(lines)
    if pending_count:
        content += (
            f" 请用一句话告诉卖家「共 {pending_count} 步需要他在卡片上确认」，"
            "不要逐步说「已发起」，也不要说「已全部完成」。"
        )
    return ToolResult(
        content=content,
        data={"batch_id": batch_id, "pending_count": pending_count,
              "billable_count": billable_count, "done": len(done), "failed": len(failures)},
    )


# ============================================================
# 注册
# ============================================================


def register_all() -> None:
    """注册商品域与陈列域工具。**幂等**：重复调用不会重复注册。"""
    specs = [
        ToolSpec(
            name="list_goods", domain=DOMAIN_GOODS,
            description=(
                "查询商品列表。**筛选维度与卖家界面完全一致**：关键词、状态、类目、价格区间、"
                "库存区间、只看低库存，并支持排序。只读。"
            ),
            parameters={"type": "object", "properties": {
                "keyword": {"type": "string", "description": "按标题或编号搜索（可选）"},
                "status": {"type": "string", "enum": ["draft", "on_shelf", "off_shelf"],
                           "description": "按状态筛选（可选）"},
                "low_stock": {"type": "boolean", "description": "只看低于预警阈值的商品（可选）"},
                "category": {"type": "string", "description": "按类目精确筛选（可选）"},
                "price_min": {"type": "number", "description": "最低价（含，可选）"},
                "price_max": {"type": "number", "description": "最高价（含，可选）"},
                "stock_min": {"type": "integer", "description": "最低库存（含，可选）"},
                "stock_max": {"type": "integer", "description": "最高库存（含，可选）"},
                "sort": {"type": "string", "enum": ["price", "stock", "updated_at", "created_at"],
                         "description": "排序字段（可选）"},
                "order": {"type": "string", "enum": ["asc", "desc"],
                          "description": "排序方向，默认 asc"},
                "created_after": {"type": "string",
                                  "description": "创建时间起始，格式 YYYY-MM-DD（可选）"},
                "created_before": {"type": "string",
                                   "description": "创建时间截止，格式 YYYY-MM-DD（可选，含当天）"},
            }, "required": []},
            handler=_list_goods,
        ),
        ToolSpec(
            name="get_goods_aftersale", domain=DOMAIN_GOODS,
            description=(
                "查看某件商品的**售后情况**：工单数、退货率、转人工数、结果分布。只读。"
                "卖家问「这件商品退货率多少」「这个商品售后多不多」时用它。"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填），如 G-0001"},
            }, "required": ["goods_no"]},
            handler=_goods_aftersale,
        ),
        ToolSpec(
            name="get_goods", domain=DOMAIN_GOODS,
            description="查看单个商品的详情（价格、成本、库存、类目、状态、图片数量、文案）。只读。",
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号，如 G-0001"},
            }, "required": ["goods_no"]},
            handler=_get_goods,
        ),
        ToolSpec(
            name="list_categories", domain=DOMAIN_GOODS,
            description=(
                "查看**现有商品类目清单**（含每个类目的商品数）。"
                "新建商品但卖家没说类目时，**先用它判断能否归入现有类目**。只读。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_list_categories,
        ),
        ToolSpec(
            name="create_goods", domain=DOMAIN_GOODS,
            description="新建商品（默认为**草稿**，不会直接上架）。",
            parameters={"type": "object", "properties": {
                "title": {"type": "string", "description": "商品标题（必填）"},
                "price": {"type": "number", "description": "售价（元，可选）"},
                "description": {"type": "string", "description": "商品文案（可选）"},
                "category": {"type": "string",
                             "description": "类目（可选）。卖家没说类目时，"
                                            "先调 list_categories 判断能否沿用现有类目，"
                                            "不能才新建"},
                "stock": {"type": "integer", "description": "库存（可选）"},
                "stock_alert": {"type": "integer", "description": "低库存预警阈值，0 表示不预警（可选）"},
            }, "required": ["title"]},
            handler=_create_goods,
        ),
        ToolSpec(
            name="update_goods", domain=DOMAIN_GOODS,
            description="修改商品的标题、文案、价格、类目、库存或预警阈值。只改你明确给出的字段。",
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "title": {"type": "string"}, "description": {"type": "string"},
                "price": {"type": "number"}, "category": {"type": "string"},
                "stock": {"type": "integer"}, "stock_alert": {"type": "integer"},
            }, "required": ["goods_no"]},
            handler=_update_goods,
        ),
        ToolSpec(
            name="set_goods_status", domain=DOMAIN_GOODS,
            description="把商品上架、下架或转回草稿。上架要求标题非空且价格大于 0。",
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "status": {"type": "string", "enum": ["draft", "on_shelf", "off_shelf"],
                           "description": "目标状态"},
            }, "required": ["goods_no", "status"]},
            handler=_set_status,
        ),
        ToolSpec(
            name="reorder_goods", domain=DOMAIN_DISPLAY,
            description="调整商品在**陈列**中的展示顺序（按你给的顺序从前到后）。",
            parameters={"type": "object", "properties": {
                "goods_nos": {"type": "array", "items": {"type": "string"},
                              "description": "按目标顺序排列的商品编号列表"},
            }, "required": ["goods_nos"]},
            handler=_reorder,
        ),
        ToolSpec(
            name="set_pinned", domain=DOMAIN_DISPLAY,
            description="把商品在陈列中置顶或取消置顶（置顶商品固定排在最前）。",
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "pinned": {"type": "boolean", "description": "true 置顶 / false 取消"},
            }, "required": ["goods_no", "pinned"]},
            handler=_set_pinned,
        ),
        ToolSpec(
            name="list_trash_goods", domain=DOMAIN_GOODS,
            description="查看**回收站**里有哪些商品（含剩余可恢复天数）。卖家问「回收站里有什么」时用它。只读。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_list_trash_goods,
        ),
        ToolSpec(
            name="restore_goods", domain=DOMAIN_GOODS,
            description=(
                "把**回收站里**的商品恢复回来（原状态一并恢复）。"
                "卖家说「把刚才删的那个捞回来」「恢复误删」时用它。只影响回收站中的商品。"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填），如 G-0001"},
            }, "required": ["goods_no"]},
            handler=_restore_goods,
        ),
        # ---- 破坏性：只声明，执行走确认流程 ----
        ToolSpec(
            name="delete_goods", domain=DOMAIN_GOODS, destructive=True,
            summary_template="删除商品 {goods_no}（移入回收站）",
            description=(
                "删除商品 → **移入回收站**（默认 7 天内可恢复，图片一并保留）。"
                "**需要卖家确认后才会执行。**"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
            }, "required": ["goods_no"]},
            handler=_delete_goods, impact_builder=_impact_delete,
        ),
        ToolSpec(
            name="bulk_apply", domain=DOMAIN_GOODS, destructive=True,
            summary_builder=lambda a: f"批量{_ACTION_LABELS.get(str(a.get('action')), a.get('action'))}"
                                      f"（{len(a.get('goods_nos') or [])} 件）",
            description=("对多件商品批量执行上架 / 下架 / 删除 / 调价。"
                         "**一次影响多件，需要卖家确认后才会执行。**"),
            parameters={"type": "object", "properties": {
                "goods_nos": {"type": "array", "items": {"type": "string"},
                              "description": "商品编号列表（必填）"},
                "action": {"type": "string", "enum": ["on_shelf", "off_shelf", "delete", "price_delta"],
                           "description": "批量动作"},
                "price_delta": {"type": "number", "description": "调价幅度（元，action=price_delta 时必填，可为负）"},
            }, "required": ["goods_nos", "action"]},
            handler=_bulk_apply, impact_builder=_impact_bulk,
        ),
        # ---- 计费：只声明，执行走确认流程（成本可见 + 需点头）----
        ToolSpec(
            name="generate_cover", domain=DOMAIN_GOODS, billable=True,
            summary_template="为商品 {goods_no} 生成宣传图",
            # 同一商品只该有一张待确认卡（`hint` 的差异不构成"另一件事"）
            idempotent_by=("goods_no",),
            confirm_note="确认后建后台任务（约 30—60 秒），进度在左栏「任务」；在此之前商品页还没有这张图。",
            description=(
                "为商品生成一张 AI 宣传图并加入商品图片。**会产生 API 费用**，"
                "需要卖家确认后才会执行；生成约需 30—60 秒。图片是追加的，不覆盖已有图片。"
                "当卖家说「生成宣传图」「配张图」时用它。"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "hint": {"type": "string", "description": "对画面的补充要求（可选），如「蓝色调」「户外场景」"},
            }, "required": ["goods_no"]},
            handler=_generate_cover,
        ),
        ToolSpec(
            name="generate_copy", domain=DOMAIN_GOODS, billable=True,
            # 同一商品只该有一张待确认卡（`style`/`apply` 的差异不构成"另一件事"）
            idempotent_by=("goods_no",),
            # 按 apply 区分措辞：写入 vs 只出候选，让确认卡一眼看出会不会改商品
            summary_builder=lambda a: f"为商品 {a.get('goods_no')} 生成文案"
                                      + ("并写入商品" if a.get("apply") else "（候选）"),
            confirm_note="确认后生成一版文案；若你选的是「写入」，会同时写入商品详情。",
            description=(
                "生成一版商品文案。**会产生 API 费用**，需卖家确认后才会执行。"
                "`apply=true` = 生成后**直接写入商品详情**（卖家要「写文案 / 改文案」时用，一步完成）；"
                "`apply=false` = 只生成候选给卖家看。"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "style": {"type": "string", "enum": ["口语", "专业", "促销"], "description": "文案风格"},
                "apply": {"type": "boolean",
                          "description": "true = 生成后写入商品；false = 只生成候选（默认）"},
            }, "required": ["goods_no"]},
            handler=_generate_copy,
        ),
        ToolSpec(
            name="suggest_price", domain=DOMAIN_GOODS, billable=True,
            summary_template="为商品 {goods_no} 生成定价建议",
            description="给出定价建议（区间 + 理由，**不会改价**）。**会产生 API 费用**，需卖家确认。",
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
            }, "required": ["goods_no"]},
            handler=_suggest_price,
        ),
        # ---- 批量计划：把 N 件商品的多步操作打包成一次提交（P0 意图/执行解耦）----
        ToolSpec(
            name="plan_batch", domain=DOMAIN_GOODS,
            description=(
                "把一批操作打包成**一次计划**一次提交，避免逐件逐个调用工具。"
                "当卖家一次要求多件商品的多步操作（如「给这 4 件商品分别生成文案、"
                "宣传图并上架」）时，把每件商品的每个动作列进 items："
                "需要确认的动作（generate_copy / generate_cover / suggest_price / "
                "bulk_apply / delete_goods / set_goods_status 等）会被合成**一张确认卡**"
                "让卖家一次确认；其余动作（create_goods / update_goods 等）会直接执行。"
                "**批量请求务必优先用它，不要逐件逐个调工具**（逐件调会被单轮额度拦断）。"
            ),
            parameters={"type": "object", "properties": {
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string",
                                 "description": "工具名，如 create_goods / generate_copy / generate_cover / bulk_apply"},
                        "args": {"type": "object", "description": "该工具的入参"},
                    },
                    "required": ["tool", "args"],
                }, "description": "本次计划要执行的动作列表"},
            }, "required": ["items"]},
            handler=_plan_batch,
        ),
    ]
    for spec in specs:
        if spec.name in _REGISTERED_NAMES:
            continue
        register(spec)
        _REGISTERED_NAMES.add(spec.name)


_REGISTERED_NAMES: set[str] = set()
