"""卖家控制台 API 层。

- `deps.py`   —— `require_seller()`：鉴权挂载点（本轮恒返回 `default`，留位给多卖家）；
- `routes.py` —— 商品 / 图片 / 操作日志的 HTTP 端点。

业务规则不在此层，见 `app/goods/service.py`。
"""

from app.seller import deps, routes

__all__ = ["deps", "routes"]
