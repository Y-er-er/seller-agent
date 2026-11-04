"""订单与客服的只读助手工具。不会发送买家消息或修改订单。"""

from __future__ import annotations

from app.assistant.registry import (
    DOMAIN_ORDERS, DOMAIN_SERVICE, AssistantContext, ToolResult, ToolSpec, register,
)
from app.db import order_repo, service_repo

_REGISTERED: set[str] = set()


async def _list_orders(ctx: AssistantContext, query: str = "", limit: int = 10) -> ToolResult:
    try:
        result = await order_repo.list_orders(ctx.seller_id, query=query[:64], limit=min(max(limit, 1), 20), offset=0)
    except order_repo.OrderRepoError as exc:
        return ToolResult(f"订单暂时查不到：{exc.message}", ok=False)
    items = result["items"]
    if not items:
        return ToolResult("没有查到本店的订单快照。这里的数据只来自本地演示或商家导入。", {"count": 0})
    lines = [f"#{o['id']} {o['external_order_id']} · {o['status']} · 订单金额 ¥{o['order_amount_cent']/100:.2f} · "
             f"{'演示' if o['source'] == 'demo' else '导入'}" for o in items]
    return ToolResult("本地订单快照：\n" + "\n".join(lines) + "\n（不是外部平台实时状态）",
                      {"count": len(items), "items": items})


async def _get_order(ctx: AssistantContext, order_id: int) -> ToolResult:
    try:
        o = await order_repo.get_order(ctx.seller_id, order_id)
    except order_repo.OrderRepoError as exc:
        return ToolResult(f"订单暂时查不到：{exc.message}", ok=False)
    if not o:
        return ToolResult("本店没有这笔订单快照。", {"found": False})
    # 原始导入行可能含手机号和地址；工具结果只提供订单基础字段。
    safe_order = {key: value for key, value in o.items() if key != "import_payload"}
    lines = [f"· {x['title']} × {x['quantity']}，该行当前已收 ¥{x['paid_cent']/100:.2f}"
             for x in o["lines"]]
    return ToolResult(
        f"订单 {o['external_order_id']}：{o['status']}，订单金额 ¥{o['order_amount_cent']/100:.2f}，"
        f"当前已收 ¥{o['paid_cent']/100:.2f}；"
        f"来源：{'演示' if o['source'] == 'demo' else '商家导入'}。\n" + "\n".join(lines)
        + "\n这是本地快照，不代表外部平台的实时状态。", {"order": safe_order},
    )


async def _list_conversations(ctx: AssistantContext) -> ToolResult:
    try:
        items = await service_repo.list_conversations(ctx.seller_id, limit=20)
    except service_repo.ServiceRepoError as exc:
        return ToolResult(f"客服收件箱暂时查不到：{exc.message}", ok=False)
    if not items:
        return ToolResult("本店目前没有本地咨询会话。", {"count": 0})
    # 买家原话可能含个人信息；模型只拿状态，不拿消息正文或 last_message。
    safe = [{"public_id": c["public_id"], "status": c["status"], "assignee": c["assignee"]} for c in items]
    lines = [f"· {c['public_id'][:8]}… [{c['status']}] 接待：{c['assignee']}" for c in safe]
    return ToolResult("最近的本地咨询状态（不含买家消息）：\n" + "\n".join(lines),
                      {"count": len(safe), "items": safe})


async def _list_replies(ctx: AssistantContext) -> ToolResult:
    try:
        items = await service_repo.list_replies(ctx.seller_id)
    except service_repo.ServiceRepoError as exc:
        return ToolResult(f"快捷回复暂时查不到：{exc.message}", ok=False)
    if not items:
        return ToolResult("本店还没有快捷回复。", {"count": 0})
    lines = [f"· {r['title']}：{r['content'][:200]}" for r in items[:20]]
    return ToolResult("本店快捷回复：\n" + "\n".join(lines), {"count": len(items), "items": items})


def register_all() -> None:
    specs = [
        ToolSpec("list_orders", DOMAIN_ORDERS, "查询本店的本地订单快照，可按订单号筛选；只读，非平台实时数据。",
                 {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}, _list_orders),
        ToolSpec("get_order", DOMAIN_ORDERS, "按本地订单数字 ID 查看订单行和金额；只读，非平台实时数据。",
                 {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]}, _get_order),
        ToolSpec("list_service_conversations", DOMAIN_SERVICE, "查看本店最近的本地客服咨询；只读，不对买家发消息。",
                 {"type": "object", "properties": {}, "required": []}, _list_conversations),
        ToolSpec("list_quick_replies", DOMAIN_SERVICE, "查看本店已有的快捷回复；只读，不会发送。",
                 {"type": "object", "properties": {}, "required": []}, _list_replies),
    ]
    for spec in specs:
        if spec.name not in _REGISTERED:
            register(spec)
            _REGISTERED.add(spec.name)
