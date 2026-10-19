"""端口层 —— 业务代码与外部世界之间的**唯一接口**。

用法（**唯一允许的形式**）：

```python
from app.ports import get_datasource
order = await get_datasource().fetch_order(order_id)
```

⚠️ **禁止**在业务代码里 `import app.adapters.*` —— 那是实现细节。
一旦被直接引用，"可插拔"就失效了（换实现就得改业务代码）。
本模块是**唯一**允许知道具体适配器的地方。
"""

from __future__ import annotations

from app.adapters.datasource_http import HttpDataSource
from app.adapters.datasource_mock import MockDataSource
from app.adapters.storage_local import LocalObjectStorage
from app.config import settings
from app.ports.datasource import DataSource
from app.ports.storage import ObjectStorage

__all__ = [
    "DataSource",
    "ObjectStorage",
    "get_datasource",
    "get_storage",
    "reset_datasource",
    "reset_storage",
]

_datasource: DataSource | None = None
_datasource_mode = ""

_storage: ObjectStorage | None = None
_storage_backend = ""


def get_datasource() -> DataSource:
    """按配置返回数据源实现（进程内缓存）。

    缓存的键是**解析后的模式**：模式一变（例如测试里改了 `*_API_BASE`，或改了 `DATA_SOURCE`）
    就自动重建 —— 既省去重复构造，也不会让测试之间互相污染。
    """
    global _datasource, _datasource_mode
    mode = settings.resolved_data_source
    if _datasource is None or _datasource_mode != mode:
        _datasource = HttpDataSource() if mode == "remote" else MockDataSource()
        _datasource_mode = mode
    return _datasource


def reset_datasource() -> None:
    """清空数据源缓存（测试用）。"""
    global _datasource, _datasource_mode
    _datasource = None
    _datasource_mode = ""


def get_storage() -> ObjectStorage:
    """按配置返回对象存储实现（进程内缓存）。"""
    global _storage, _storage_backend
    backend = (settings.storage_backend or "local").strip().lower()
    if _storage is None or _storage_backend != backend:
        _storage = _build_storage(backend)
        _storage_backend = backend
    return _storage


def _build_storage(backend: str) -> ObjectStorage:
    if backend == "cos":
        # **明确拒绝，而不是静默回落到本地** —— 否则会出现"以为图片在云上、其实在本机"
        if not settings.storage_configured:
            raise RuntimeError(
                "STORAGE_BACKEND=cos 但 COS 配置不完整："
                "需 COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET"
            )
        raise NotImplementedError(
            "COS 适配器尚未实现（本轮只交付本地实现）。"
            "实现时新增 app/adapters/storage_cos.py，并通过 storage 契约测试。"
        )
    return LocalObjectStorage()


def reset_storage() -> None:
    """清空存储缓存（测试用）。"""
    global _storage, _storage_backend
    _storage = None
    _storage_backend = ""
