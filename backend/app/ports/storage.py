"""对象存储端口（**契约**）。

⚠️ 本端口存在的首要理由：**URL 必须由实现给出**（`url_for`）。
本地实现的 URL 形如 `/api/seller/uploads/xxx`，COS 实现形如
`https://<bucket>.cos.<region>.myqcloud.com/xxx` —— 两者形态不同，
若让调用方自己拼 URL，将来换存储后端时散落各处的拼接必然出错。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStorage(Protocol):
    """对象存储。实现见 `app/adapters/storage_*.py`。"""

    async def put(self, key: str, data: bytes, content_type: str = "") -> str:
        """写入对象，返回 **key**（不是 URL）。失败时抛异常——写失败必须让调用方知道。"""
        ...

    async def get(self, key: str) -> bytes | None:
        """读取对象；不存在返回 `None`（读通道失败可降级）。"""
        ...

    async def delete(self, key: str) -> bool:
        """删除对象；返回是否确实删掉了（不存在也算成功语义由实现决定）。"""
        ...

    def url_for(self, key: str) -> str:
        """把 key 转成**可直接给浏览器访问**的 URL。

        ⚠️ 调用方**禁止**自己拼 URL，一律走本方法。
        """
        ...
