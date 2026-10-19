"""本地对象存储 —— 写入 `backend/data/uploads/<key>`。

生产环境换成 `CosObjectStorage`（同端口），业务代码零改动。
URL 由本实现给出（`url_for`），调用方禁止自己拼。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

#: 本地存储对外暴露的 URL 前缀（由 main.py 挂静态路由）
URL_PREFIX = "/api/seller/uploads"


class LocalObjectStorage:
    """本地目录实现：实现 `ports.ObjectStorage`。

    根目录解析规则：`UPLOAD_DIR` 为相对路径时，相对 **backend/** 解析
    （即默认落在 `backend/data/uploads`）。
    """

    def __init__(self, root: str | None = None, url_prefix: str = URL_PREFIX) -> None:
        raw = Path(root or settings.upload_dir)
        if not raw.is_absolute():
            raw = PROJECT_ROOT / "backend" / raw
        self._root = raw.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._prefix = url_prefix.rstrip("/")

    @property
    def root(self) -> Path:
        """根目录绝对路径（供静态路由与排查用）。"""
        return self._root

    def _safe_path(self, key: str) -> Path:
        """把 key 解析为绝对路径，并**校验未越出根目录**（防路径穿越）。

        拒绝：空 key、绝对路径、含 `..` 的路径、解析后落在根目录之外的路径。
        """
        if not key or key.startswith(("/", "\\")) or ".." in Path(key).parts:
            raise ValueError(f"非法对象 key：{key!r}")
        target = (self._root / key).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError(f"非法对象 key（越界）：{key!r}")
        return target

    async def put(self, key: str, data: bytes, content_type: str = "") -> str:
        """写入对象；返回 key。写失败**抛异常**（写通道失败不得静默）。"""
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    async def get(self, key: str) -> bytes | None:
        """读取对象；不存在或 key 非法返回 `None`（读通道可降级）。"""
        try:
            path = self._safe_path(key)
        except ValueError:
            logger.warning("拒绝读取非法对象 key：%r", key)
            return None
        return path.read_bytes() if path.is_file() else None

    async def delete(self, key: str) -> bool:
        """删除对象；返回是否确实删掉了。空目录不清理（交由运维）。"""
        try:
            path = self._safe_path(key)
        except ValueError:
            logger.warning("拒绝删除非法对象 key：%r", key)
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    def url_for(self, key: str) -> str:
        """把 key 转成浏览器可直接访问的 URL。"""
        return f"{self._prefix}/{key.lstrip('/')}"
