"""外部原始值 → 领域类型的安全转换（**适配层基础设施**）。

为什么不放在 `tools/http_client.py`：那是"HTTP 传输"模块，而本文件是
"**外部原始值 → 内部类型**"的转换，属于**适配层**。放错层会造成
`adapters → tools` 的反向依赖，而 `tools` 的包初始化又会回头 import 各 tools 模块，
最终形成循环导入（实测踩到）。

统一约定：**非法值一律回落默认值，绝不抛异常** ——
单个字段脏不该让整条取数失败（契约见 `docs/契约_外部数据源_v1.md` §零）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

_E = TypeVar("_E", bound=Enum)


def coerce_enum(enum_cls: type[_E], value: Any, default: _E) -> _E:
    """把外部返回值安全转换为枚举；非法值回落到默认值（不抛 ValueError）。"""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def coerce_float(value: Any, default: float = 0.0) -> float:
    """安全转 float；None/非法值回落默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: Any, default: int = 0) -> int:
    """安全转 int；None/非法值回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_datetime(value: Any) -> datetime | None:
    """把外部返回值安全转为 datetime；None/空/非法值返回 None（不抛异常）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
