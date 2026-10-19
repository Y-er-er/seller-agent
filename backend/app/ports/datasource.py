"""数据源端口（**契约**）。

业务代码只认这个协议，不认具体实现。取数语义三条硬约束（与既有 `http_client` 一致）：

1. **只读**：本端口不提供任何写方法（资金动作另走 `tools/aftersale.py`，语义根本不同）；
2. **不抛异常**：未配置 / 超时 / 非 2xx / 解析失败 → 一律返回 `None`，
   交由规则 R8 判「数据不足 → 转人工」，**不让网络异常中断判定链路**；
3. **不造假**：取不到就是 `None`，绝不用合成数据顶替。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import BuyerProfile, LogisticsInfo, OrderSnapshot


@runtime_checkable
class DataSource(Protocol):
    """外部业务数据源。实现见 `app/adapters/datasource_*.py`。"""

    async def fetch_order(self, order_id: str) -> OrderSnapshot | None:
        """订单快照；取不到返回 `None`。契约见 docs/契约_外部数据源_v1.md §一。"""
        ...

    async def fetch_logistics(self, order_id: str) -> LogisticsInfo | None:
        """物流信息；取不到返回 `None`。契约见 §二。"""
        ...

    async def fetch_profile(self, order_id: str) -> BuyerProfile | None:
        """买家画像；取不到返回 `None`。契约见 §三。"""
        ...
