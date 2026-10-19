"""卖家身份解析 —— **鉴权挂载点**（本轮唯一的"谁在操作"来源）。

现状：**单卖家**，恒返回 `default`。
用户 2026-10-16 明确：本轮**只预留接口位**，不做登录，后续版本再迭代多卖家。

将来接登录时，**只改本文件**：
    从请求的会话 / Token 解析出 `seller_id`，并在此处做数据隔离校验（越权直接 403）。
所有 `/api/seller/*` 端点都已 `Depends(require_seller)`，因此**无需改动任何端点**。

⚠️ 已知缺口（不假装已解决）：现阶段 `/api/seller/*` **无任何鉴权**，
能访问该端口者即可操作商品。服务默认只绑 `127.0.0.1` 以缩小暴露面。
"""

from __future__ import annotations

#: 单卖家形态下的固定卖家标识（与 DDL 中 `seller_id` 的默认值一致）
DEFAULT_SELLER_ID = "default"


async def require_seller() -> str:
    """返回当前卖家标识。多卖家迭代时在此处接入会话解析与数据隔离。"""
    return DEFAULT_SELLER_ID
