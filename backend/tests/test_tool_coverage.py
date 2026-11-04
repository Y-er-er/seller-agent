"""工具覆盖比对测试 —— **永久堵住"端点做了、工具忘了"**。

背景：同类漏接已发生过三次（生图、文案定价、售后统计/任务/日志/撤销）。
靠记性不可靠，所以把规则写成测试：

> **每个"卖家可执行的业务动作端点"，都必须有对应的助手工具。**

白名单里只放**纯 UI / 纯展示**端点（上传下载、概览看板等），
它们不是"业务动作"，不需要被 AI 调用。

运行：python backend/tests/test_tool_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import loop, registry  # noqa: E402
from app.main import app  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


#: 端点路径片段 → 对应工具名。**新增卖家业务端点时必须在这里登记**
#: （或者直接加一个同名工具；漏了这条测试就红）。
ENDPOINT_TO_TOOL = {
    "/api/seller/goods/bulk": "bulk_apply",
    "/api/seller/goods/reorder": "reorder_goods",
    "/api/seller/goods/{goods_id}/aftersale-stats": "get_goods_aftersale",
    "/api/seller/goods/{goods_id}/ai/cover": "generate_cover",
    "/api/seller/goods/{goods_id}/ai/copy": "generate_copy",
    "/api/seller/goods/{goods_id}/ai/price": "suggest_price",
    "/api/seller/tasks": "list_tasks",
    "/api/seller/tasks/{task_id}": "list_tasks",
    "/api/seller/logs": "list_logs",
    "/api/seller/logs/{log_id}/undo": "undo_log",
    "/api/seller/goods/{goods_id}/restore": "restore_goods",
    "/api/seller/tasks/{task_id}/retry": "retry_task",
    # 知识库：检索 / 清单 / 删除都有工具；只有**上传**没有（模型拿不到文件字节）
    "/api/seller/knowledge/search": "search_knowledge",
    "/api/seller/knowledge": "list_knowledge_docs",
    "/api/seller/orders": "list_orders",
    "/api/seller/orders/{order_id}": "get_order",
    "/api/seller/service/conversations": "list_service_conversations",
    "/api/seller/service/quick-replies": "list_quick_replies",
    # 同一路径上的 PATCH（改归属/标签）**没有**对应工具 —— 有意为之：
    # 让模型有权把资料挪来挪去，只会制造"我明明放在售后政策库里"的困惑；
    # 删除（DELETE）有工具，且必须走确认卡。
    "/api/seller/knowledge/{doc_id}": "delete_knowledge_doc",
}

#: 纯 UI / 展示端点，不需要 AI 工具
WHITELIST_PREFIXES = (
    "/api/seller/uploads",       # 图片静态服务
    "/api/seller/goods/{goods_id}/media",  # 图片上传/删除走界面交互
    "/api/seller/overview",      # 概览看板：纯展示聚合，AI 用 list_goods/统计工具即可拼出
    # 彻底删除**不给 AI 工具**：不可恢复，且只在回收站里由人手动执行。
    # 这是有意的边界，不是漏接。
    "/api/seller/goods/{goods_id}/purge",
    # 知识库**上传**不给 AI 工具：对话里没有文件通路，模型拿不到文件字节。
    # （删除资料已有 `delete_knowledge_doc` 工具，走确认卡，见上面的映射。）
    "/api/seller/knowledge/upload",
    # 导入和生成演示订单只由人发起；模型不应制造订单事实。
    "/api/seller/orders/import", "/api/seller/orders/demo",
    # 客服对外动作只由人工执行；AI 仅有只读工具。
    "/api/seller/service/conversations/{conversation_id}/takeover",
    "/api/seller/service/conversations/{conversation_id}/close",
    "/api/seller/service/conversations/{conversation_id}/messages",
    # 买家原话可能含个人信息；会话详情仅人工在界面查看，不送模型。
    "/api/seller/service/conversations/{conversation_id}",
)

#: 回收站列表走 `list_trash_goods` 工具（路径是 /goods/trash，单独登记）
ENDPOINT_TO_TOOL["/api/seller/goods/trash"] = "list_trash_goods"


def main() -> int:
    loop.ensure_tools_registered()
    tools = {s.name: s for s in registry.all_specs()}
    paths = sorted(app.openapi()["paths"].keys())

    print("【A 组】端点 → 工具 映射完整")
    for path, tool_name in ENDPOINT_TO_TOOL.items():
        check(f"端点 {path} 有对应工具 {tool_name}",
              tool_name in tools, f"注册表里没有 {tool_name}")

    print("\n【B 组】所有卖家域端点都被覆盖或显式豁免")
    seller_paths = [p for p in paths if p.startswith("/api/seller")]
    unmapped = []
    for path in seller_paths:
        if path in ENDPOINT_TO_TOOL:
            continue
        if any(path.startswith(prefix) for prefix in WHITELIST_PREFIXES):
            continue
        # 通用 CRUD（/goods、/goods/{id}）由 list_goods / get_goods / update_goods 等覆盖
        if path in ("/api/seller/goods", "/api/seller/goods/{goods_id}"):
            continue
        if path.startswith("/api/seller/assistant"):     # 助手自身的会话端点
            continue
        # 登录端点**刻意不给 AI 工具**：让模型有机会"代替卖家登录/退出"，
        # 等于把身份边界交给提示词去守。登录只能由人在界面上做。
        if path.startswith("/api/seller/auth"):
            continue
        unmapped.append(path)

    check("没有「悬空」的卖家业务端点（每个都有工具或已豁免）",
          not unmapped, "未覆盖：" + ", ".join(unmapped))

    print("\n【C 组】工具自身的基本约束")
    dup = len(registry.all_specs()) - len(tools)
    check("工具名无重复", dup == 0, f"重复 {dup} 个")
    check("每个工具都有描述", all((s.description or "").strip() for s in tools.values()))
    check("需要确认的工具都有说明文案",
          all((s.summary_template or s.summary_builder or not s.needs_confirm) for s in tools.values()))
    check("工具总数符合预期（原 30 + 订单/客服只读 4）",
          len(tools) == 34, f"实际 {len(tools)} 个：{sorted(tools)}")

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
