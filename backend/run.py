"""便捷启动脚本 —— 在 PyCharm 里右键 Run 本文件（或命令行 `python run.py`）即可起服务。

⚠️ 为什么不直接 Run `app/main.py`：
   `app/main.py` 用的是**包内导入**（`from app import ...`）。直接运行它时，Python 会把
   `app/` 目录当作根，导致 `ModuleNotFoundError: No module named 'app'`；而且 `main.py`
   里**没有启动入口块**（只有 `app = FastAPI(...)` 定义），跑完也不会起服务。
   本脚本位于 `backend/` 根，用 `uvicorn.run("app.main:app")` **字符串形式**加载，
   由 uvicorn 负责 import，规避上述两个问题。

等价命令：
   cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000

前置：MySQL（VM 内 Docker）需可达；否则对话层落库失败（建单返回 500）。
"""

from __future__ import annotations

import os
import sys

# 把 backend/ 加入 sys.path，兼容从任意位置运行本脚本
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 默认开启对话入口（指挥台 + 买家 H5）；可用外部环境变量覆盖，例如 CHAT_ENABLED=false
os.environ.setdefault("CHAT_ENABLED", "true")

import uvicorn  # noqa: E402  （必须在 sys.path 处理之后再导入）

if __name__ == "__main__":
    # host=127.0.0.1：仅本机可访问（安全默认）；端口与前端默认 API 地址一致
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
