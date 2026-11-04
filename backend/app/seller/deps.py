"""卖家身份解析 —— **鉴权的唯一挂载点**。

2026-10-20 起这里不再是"恒返回 default"的占位：**所有 `/api/seller/*` 都要求登录**。
实现见 `app/seller/auth.py` 与 `sql/seller_schema.sql`。

为什么用 `X-Seller-Token` 请求头而不是 Cookie：

- 控制台可以**双击 html 用 file:// 打开**（`origin` 为 null），跨源 Cookie 需要
  后端 `allow_credentials` + 精确 origin，而当前 CORS 是 `*`（本地开发）；
  一旦打开 credentials，浏览器就会拒绝 `*`。请求头没有这个矛盾。
- 代价是 Token 存在 `localStorage`（XSS 可读）。本项目页面**全部输出转义**且无第三方脚本；
  若将来引入用户可编辑的富文本，需改回 HttpOnly Cookie 方案。

两个依赖的分工：

- `require_seller()` → 只要 `seller_id`（商品 / 知识库等按卖家隔离的接口用）；
- `require_session()` → 需要**会话**的接口（AI 助手要 `thread_id`，登录态接口要 Token）。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.seller import auth

#: 单卖家时代的历史常量，保留给迁移脚本 / 测试引用（业务代码不要再用）。
DEFAULT_SELLER_ID = "default"


@dataclass(frozen=True)
class SellerSession:
    """一次请求的登录态。**不含密码哈希等敏感字段。**"""

    token: str
    seller_id: str
    username: str
    display_name: str
    thread_id: str

    @property
    def display(self) -> str:
        return self.display_name or self.username


async def require_session(
    x_seller_token: str | None = Header(default=None, alias="X-Seller-Token"),
) -> SellerSession:
    """解析登录态；无效 / 过期 / 已退出统一 401。

    ⚠️ 401 的文案要能指导下一步动作：前端收到 401 会**立刻弹登录窗**，
    所以这里不区分"没登录"和"过期了"，只说"请重新登录"。
    """
    session = await auth.resolve(x_seller_token or "")
    if not session:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return SellerSession(
        token=x_seller_token or "",
        seller_id=session["seller_id"],
        username=session["username"],
        display_name=session.get("display_name") or session["username"],
        thread_id=session["thread_id"],
    )


async def require_seller(session: SellerSession = Depends(require_session)) -> str:
    """只要卖家标识的接口用这个（数据隔离靠 `seller_id`）。"""
    return session.seller_id
