"""订单服务客户端（**只读**）—— 已退化为**薄封装**。

真正的取数实现按配置在 `app/ports` 里选择：
`.env` 的 `DATA_SOURCE`（或自动推断）决定走**本地夹具**还是**真实 HTTP**。

保留本模块的两个理由：

1. 既有调用方（`graph/nodes/fetch.py`、`chat/tools.py`）无需任何改动；
2. 为测试与演示保留 `seed_*` / `peek_*` 注入入口。

取数语义见 `docs/契约_外部数据源_v1.md`：**失败一律返回 `None`，绝不抛异常**。
"""

from __future__ import annotations

from typing import Any

from app.adapters.datasource_mock import get_fixtures
from app.models import OrderSnapshot
from app.ports import get_datasource


def seed_order(order_id: str, **facts: Any) -> None:
    """测试/演示用：注入一条订单事实（写入本地夹具，进程内有效、不落库）。"""
    get_fixtures().seed_order(order_id, **facts)


def is_seeded(order_id: str) -> bool:
    """该订单是否已有本地夹具事实。

    用途：演示夹具据此判断"订单是否已存在"，避免覆盖已预置的事实。
    remote 模式下夹具不参与取数，本函数返回 False 也无影响。
    """
    return order_id in get_fixtures().orders


def peek_order(order_id: str) -> dict[str, Any] | None:
    """读取该订单**已注入的事实**（只读副本）；不存在返回 `None`。

    用途：演示夹具在"订单已存在"时必须**如实回显真正注入的事实**，
    而不是另算一份合成值 —— 否则界面显示的金额/类目会与规则实际使用的事实不一致。
    """
    row = get_fixtures().orders.get(order_id)
    return dict(row) if row else None


async def fetch_order(order_id: str) -> OrderSnapshot | None:
    """拉取订单快照。取不到返回 `None`。"""
    return await get_datasource().fetch_order(order_id)
