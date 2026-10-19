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
    ctx: AssistantContext, keyword: str = "", status: str = "", low_stock: bool = False
) -> ToolResult:
    items = await repo.list_goods(
        ctx.seller_id, scope="all", q=keyword or "", status=status or "",
        low_stock_only=bool(low_stock), limit=50,
    )
    if not items:
        return ToolResult("没有符合条件的商品。", {"count": 0})
    body = "\n".join(_line(g) for g in items)
    return ToolResult(
        f"共 {len(items)} 件商品：\n{body}",
        {"count": len(items), "items": [
            {"goods_no": g["goods_no"], "title": g["title"], "price": g["price"],
             "status": g["status"], "stock": g["stock"]} for g in items
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
    return ToolResult(
        f"已删除「{goods['title']}」（{goods['goods_no']}）及 {result.get('media_removed', 0)} 张图片。此操作不可撤销。",
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
        out = await image_ai.generate_cover(ctx.seller_id, int(goods["id"]), hint=hint or "")
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(
        f"已为「{goods['title']}」生成宣传图并加入商品图片（模型 {out['model']}）。"
        f"这张图是**追加**的，不满意可以直接删除。",
        out,
    )


async def _generate_copy(ctx: AssistantContext, goods_no: str, style: str = "口语") -> ToolResult:
    from app.goods import ai as goods_ai

    goods = await service.find_by_no(ctx.seller_id, goods_no)
    if goods is None:
        return _fail(f"找不到编号为 {goods_no} 的商品")
    try:
        out = await goods_ai.generate_copy(ctx.seller_id, int(goods["id"]), style)
    except GoodsError as exc:
        return _fail(exc.message)
    return ToolResult(
        f"为「{goods['title']}」生成了{style}风格的文案候选：\n{out['copy']}\n"
        f"（该候选**尚未写入商品**。⚠️ 若卖家本就是要更新文案，请**立即调用 update_goods** "
        f"把上面这段写进去，不要停下来等他再说第二遍；只有他明确说「先看看」时才停。）",
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
# 注册
# ============================================================


def register_all() -> None:
    """注册商品域与陈列域工具。**幂等**：重复调用不会重复注册。"""
    specs = [
        ToolSpec(
            name="list_goods", domain=DOMAIN_GOODS,
            description="查询商品列表（可按关键词、状态筛选，或只看低库存）。只读。",
            parameters={"type": "object", "properties": {
                "keyword": {"type": "string", "description": "按标题或编号搜索（可选）"},
                "status": {"type": "string", "enum": ["draft", "on_shelf", "off_shelf"],
                           "description": "按状态筛选（可选）"},
                "low_stock": {"type": "boolean", "description": "只看低于预警阈值的商品（可选）"},
            }, "required": []},
            handler=_list_goods,
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
            name="create_goods", domain=DOMAIN_GOODS,
            description="新建商品（默认为**草稿**，不会直接上架）。",
            parameters={"type": "object", "properties": {
                "title": {"type": "string", "description": "商品标题（必填）"},
                "price": {"type": "number", "description": "售价（元，可选）"},
                "description": {"type": "string", "description": "商品文案（可选）"},
                "category": {"type": "string", "description": "类目（可选）"},
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
        # ---- 破坏性：只声明，执行走确认流程 ----
        ToolSpec(
            name="delete_goods", domain=DOMAIN_GOODS, destructive=True,
            summary_template="删除商品 {goods_no}",
            description="删除商品（连同其图片）。**这是不可撤销的操作，需要卖家确认后才会执行。**",
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
            summary_template="为商品 {goods_no} 生成文案候选",
            description=(
                "生成一版商品文案**候选**（本身**不写入商品**）。**会产生 API 费用**，需卖家确认。"
                "⚠️ 若卖家要求的是「生成并更新 / 写入文案」，生成后应**继续调用 update_goods** 写入，"
                "不要只给候选就结束。"
            ),
            parameters={"type": "object", "properties": {
                "goods_no": {"type": "string", "description": "商品编号（必填）"},
                "style": {"type": "string", "enum": ["口语", "专业", "促销"], "description": "文案风格"},
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
    ]
    for spec in specs:
        if spec.name in _REGISTERED_NAMES:
            continue
        register(spec)
        _REGISTERED_NAMES.add(spec.name)


_REGISTERED_NAMES: set[str] = set()
