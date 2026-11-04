"""数据源端口（**契约**）。

业务代码只认这个协议，不认具体实现。取数语义三条硬约束（与既有 `http_client` 一致）：

1. **只读**：本端口不提供任何写方法（资金动作另走 `tools/aftersale.py`，语义根本不同）；
2. **不抛异常**：未配置 / 超时 / 非 2xx / 解析失败 → 一律返回 `None`，
   交由规则 R8 判「数据不足 → 转人工」，**不让网络异常中断判定链路**；
3. **不造假**：取不到就是 `None`，绝不用合成数据顶替。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.models import BuyerProfile, GoodsSnapshot, LogisticsInfo, OrderSnapshot


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


@runtime_checkable
class GoodsSource(Protocol):
    """**商品只读源** —— 接真机时由平台提供。

    为什么不塞进 `DataSource`：两者**能力与节奏完全不同** ——
    订单是"来一单查一单"（即时），商品是"批量增量同步"（`updated_since`），
    而且真实业务里两者常常来自**不同系统**（订单 ERP、商品平台）。

    ⚠️ **只读**。写回平台（改价 / 上下架 / 改库存）是另一件事：
    多数平台不开放第三方写，需官方 ISV / ERP 授权 —— 不属于本端口职责，
    也不应该在这里假装能做到。
    """

    async def fetch_goods(self, goods_no: str) -> GoodsSnapshot | None:
        """按编号取单个商品；不存在返回 `None`。"""
        ...

    async def list_goods(
        self, *, updated_since: datetime | None = None, limit: int = 200
    ) -> list[GoodsSnapshot]:
        """列商品。`updated_since` 用于**增量同步**（只取这之后更新的）。

        ⚠️ 真实店铺商品是几千条，全量拉不动 —— 增量是**必需项**，不是优化项。
        """
        ...

    async def fetch_categories(self) -> list[dict[str, Any]]:
        """平台类目树（多级）。用于把**叶子类目名**映射成我们的扁平 `goods.category`。"""
        ...
