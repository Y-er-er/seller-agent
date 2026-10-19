"""任务中心。

| 模块 | 职责 |
| --- | --- |
| `runner.py` | 后台执行器：`submit()` 长任务 / `run_inline()` 即时操作（都留任务记录） |

数据落在 `ops_task` 表（`sql/task_schema.sql`），仓储在 `app/db/task_repo.py`。

**为什么要有它**：AI 生图 30—60 秒、批量操作会有部分失败 ——
这两类"结果不在当场"的动作，需要一个**事后可查**的载体；
否则用户只能说"刚才好像生成了/好像失败了一件"。
"""

from app.tasks import runner

__all__ = ["runner"]
