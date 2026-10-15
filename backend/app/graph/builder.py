"""LangGraph 图装配与编译。

拓扑（对齐方案 §4）：
    START → intake ─(invalid)→ audit → END
                 └─(fetch)→ fanout ─┬─ fetch_order ─┐
                                    ├─ fetch_logistics ─┼→ evidence → judge
                                    └─ fetch_profile ─┘
                                                        ├─(to_human)→ human_review ─┐
                                                        └─(else)→ risk → dispose ───┴→ audit → END
"""

from __future__ import annotations

import inspect
import logging
from enum import Enum
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

import app.models as _models
from app.config import PROJECT_ROOT, settings
from app.graph.nodes import (
    audit_node,
    dispose_node,
    evidence_node,
    fetch_logistics_node,
    fetch_order_node,
    fetch_profile_node,
    human_review_node,
    intake_node,
    judge_node,
    risk_node,
)
from app.graph.routing import route_after_dispose, route_after_intake, route_after_judge
from app.graph.state import RefundState

_FETCH_NODES = ("fetch_order", "fetch_logistics", "fetch_profile")

logger = logging.getLogger(__name__)


def _fanout_node(state: RefundState) -> dict:
    """纯路由节点（无副作用，不读写 state 字段）。

    存在的理由：LangGraph 条件边的**映射值必须是单个节点名**，不能直接写列表
    （否则 compile 期校验会报 `unhashable type: 'list'`）。
    用一个空节点承接分支，再靠它的多条出边形成并行 fan-out，语义等价且对校验友好。
    """
    return {}


def _resolve_ckpt_path(db_path: str | None = None) -> Path:
    """把配置里的相对路径解析为项目内的绝对路径。"""
    raw = db_path or settings.checkpoint_sqlite_path
    path = Path(raw)
    if not path.is_absolute():
        path = (PROJECT_ROOT / raw.removeprefix("./")).resolve()
    return path


def build_serde() -> JsonPlusSerializer:
    """构造检查点序列化器，**显式登记**本项目自定义类型。

    为什么需要：状态中存放了 Pydantic 模型与枚举，LangGraph 反序列化时会告警
    `Deserializing unregistered type ... will be blocked in a future version`。
    官方给出的解法就是本参数（显式白名单）——比放开 pickle 更符合"最小授权"。

    白名单从 `app.models` **动态枚举**（仅取本模块定义的类），
    因此新增/改名模型无需手工维护，也不会因遗漏而触发告警。
    """
    allowed: list[tuple[str, str]] = [
        (_models.__name__, name)
        for name, obj in vars(_models).items()
        if isinstance(obj, type)
        and obj.__module__ == _models.__name__
        and issubclass(obj, (BaseModel, Enum))
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


async def make_checkpointer(db_path: str | None = None) -> AsyncSqliteSaver:
    """创建**异步** SQLite 检查点存储。

    为什么必须用 Async 版：图节点为 async（取数需并行 I/O），LangGraph 的异步执行
    路径会调用 checkpointer 的 `aget_tuple` / `aput`；同步 `SqliteSaver` 会直接抛
    `NotImplementedError`。此处不经任何"降级为同步"的妥协。
    """
    path = _resolve_ckpt_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(path))
    checkpointer = AsyncSqliteSaver(conn, serde=build_serde())

    # 幂等建表：不同版本 API 可能是同步或异步，这里统一兼容
    setup = getattr(checkpointer, "setup", None)
    if callable(setup):
        result = setup()
        if inspect.isawaitable(result):
            await result

    return checkpointer


def build_graph(checkpointer: AsyncSqliteSaver | None = None):
    """装配并编译图。传入 checkpointer 才会启用持久化与 interrupt 恢复能力。"""
    builder = StateGraph(RefundState)

    # ---- 节点 ----
    builder.add_node("intake", intake_node)
    builder.add_node("fetch_order", fetch_order_node)
    builder.add_node("fetch_logistics", fetch_logistics_node)
    builder.add_node("fetch_profile", fetch_profile_node)
    builder.add_node("evidence", evidence_node)
    builder.add_node("judge", judge_node)
    builder.add_node("risk", risk_node)
    builder.add_node("dispose", dispose_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("audit", audit_node)
    builder.add_node("fanout", _fanout_node)

    # ---- 入口与受理分支 ----
    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        {"fetch": "fanout", "invalid": "audit"},
    )

    # ---- 并行 fan-out 与汇聚 ----
    # fanout 的多条出边会在同一 superstep 并行触发三个取数节点；
    # 三个取数节点均指向 evidence，故 evidence 会等三者全部完成后再执行。
    for name in _FETCH_NODES:
        builder.add_edge("fanout", name)
        builder.add_edge(name, "evidence")

    builder.add_edge("evidence", "judge")

    # ---- 判定分支 ----
    builder.add_conditional_edges(
        "judge",
        route_after_judge,
        {"human_review": "human_review", "risk": "risk"},
    )

    # ---- 处置链与统一汇聚 ----
    builder.add_edge("risk", "dispose")
    # dispose 后需分流：处置未成功落地（未获授权 / 调用失败）时升级人工，
    # 否则资金动作会"悬空"（判定该退、但钱没退，且无人跟进）。见 routing.route_after_dispose。
    builder.add_conditional_edges(
        "dispose",
        route_after_dispose,
        {"human_review": "human_review", "audit": "audit"},
    )
    # 转人工的两条来路（judge 判定转人工 / dispose 执行未成功）都汇聚到 audit
    builder.add_edge("human_review", "audit")
    builder.add_edge("audit", END)

    return builder.compile(checkpointer=checkpointer)


_graph = None


async def get_graph():
    """进程内单例图（方案 §13：只编译一次）。首次调用时创建异步 checkpointer。"""
    global _graph
    if _graph is None:
        _graph = build_graph(await make_checkpointer())
    return _graph


async def close_graph() -> None:
    """关闭单例图持有的检查点连接，并清空单例。

    为什么必须显式关闭：`aiosqlite` 为每个连接启动一个**非守护线程**，
    连接未关闭时 Python 解释器退出前会一直等待该线程 —— 表现为进程"卡住不退出"
    （实测：仅打开一次共享图而不关闭，脚本会在结尾挂到超时被杀）。
    FastAPI 的 lifespan 关闭阶段与测试收尾都应调用本函数。
    """
    global _graph
    if _graph is None:
        return

    connection = getattr(getattr(_graph, "checkpointer", None), "conn", None)
    if connection is not None:
        try:
            await connection.close()
        except Exception:  # noqa: BLE001 — 关闭失败不得影响退出流程
            logger.warning("关闭检查点连接失败", exc_info=True)
    _graph = None
