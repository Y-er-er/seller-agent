"""外部业务客户端。

**读通道**（`order` / `logistics` / `profile`）：配置了 base_url 走真实只读 HTTP，
未配置则降级为本地内存 mock —— 两类入口对调用方完全同形。

**写通道**（`aftersale` 模块，M2 处置执行）：语义与读通道**根本不同** ——
未获授权时拒绝执行（不 mock 成功）、失败必须升级人工、调用携带幂等键。
详见 `app/tools/aftersale.py` 的模块说明。
"""

from app.tools.aftersale import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SHADOW,
    DisposalResult,
    aexecute_action,
)
from app.tools.http_client import aclose
from app.tools.logistics import fetch_logistics, seed_logistics
from app.tools.order import fetch_order, seed_order
from app.tools.profile import fetch_profile, seed_profile

__all__ = [
    "aclose",
    "fetch_logistics",
    "fetch_order",
    "fetch_profile",
    "seed_logistics",
    "seed_order",
    "seed_profile",
    # 写通道（处置执行）
    "aexecute_action",
    "DisposalResult",
    "STATUS_EXECUTED",
    "STATUS_FAILED",
    "STATUS_NOT_CONFIGURED",
    "STATUS_SHADOW",
]
