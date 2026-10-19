"""商品域。

分层：`api`（HTTP 形态）→ **`service`（业务规则）** → `repo`（SQL）。

同一份数据支撑两个模块：
- **商品陈列** = `status='on_shelf'` 的视图（对外展示的样子）；
- **商品管理** = 全量视图（增删改查）。
两者不是两套数据，因此"管理里删除 → 陈列同步消失"是天然成立的。
"""

from app.goods import service
from app.goods.service import GoodsError

__all__ = ["service", "GoodsError"]
