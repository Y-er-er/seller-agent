"""对话内核（P1）：成本闸门、会话状态机、会话服务。

模块职责：
- `cost_gate.py` —— 成本闸门（调用前预检 + 调用后记账）；
- `state.py`     —— 会话状态机与护栏判定（**纯逻辑、无 IO**）；
- `session.py`   —— 会话服务（把状态机与仓储串起来的**编排层**）。

**边界**：本包只负责"对话过程"，**不参与资格判定**——
判定一律通过 `submit_for_decision` 委托给既有判定图（`app/graph`）。

导入约定：为保持依赖方向单一，请使用**完整路径**导入子模块
（如 `from app.chat.session import create_session`），本包不在 `__init__` 里做re-export。
"""
