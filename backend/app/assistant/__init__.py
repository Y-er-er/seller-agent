"""AI 经营助手。

| 模块 | 职责 |
| --- | --- |
| `registry.py` | **工具注册表**：域 / 破坏性标记 / schema / 能力清单 |
| `tools_goods.py` | 商品域与陈列域工具（9 个） |
| `tools_aftersale.py` | 售后域工具（4 个，**完全复用指志台**） |
| `actions.py` | **待确认动作**：破坏性操作的人在环机制 |
| `loop.py` | 对话循环（形态沿用 `app/chat/agent.py`，更精简） |
| `routes.py` | HTTP 端点 |

设计依据：`docs/设计_智能体能力地图_v1.md` ——
**加智能体 ≠ 加代码模块，而是往注册表里加工具。**
"""

from app.assistant import actions, loop, registry, tools_aftersale, tools_goods

__all__ = ["actions", "loop", "registry", "tools_aftersale", "tools_goods"]
