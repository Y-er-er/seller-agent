"""图状态查询（供复核工作台与状态接口复用）。

职责边界：本模块只做**只读查询与形态裁剪**，不修改任何状态、不触发任何节点。

两类查询：
1. `get_thread_state()`  — 取单个受理号的当前快照（含待复核节点与中断载荷）；
2. `list_pending_reviews()` — 列出当前**挂起待复核**的受理号（M4 复核工作台的数据源）。

为什么"待复核列表"必须从检查点读、而不是查 `decision_audit` 表：
`to_human` 路径在 `human_review` 节点处 `interrupt()` 挂起，此时**尚未走到 audit 节点**，
业务审计表里还没有这一行的记录。只有复核完成、图跑到 audit 之后才落库。
因此待办队列的权威数据源只能是检查点（方案 §9.2 的"技术留痕"侧）。
"""

from __future__ import annotations

from typing import Any, Mapping

# 对外返回的状态枚举
STATUS_PENDING_REVIEW = "pending_review"
STATUS_COMPLETED = "completed"


def thread_config(thread_id: str) -> dict[str, Any]:
    """构造 RunnableConfig。thread_id = 受理号，与业务库一一对应（方案 §5）。"""
    return {"configurable": {"thread_id": thread_id}}


def summarize_state(values: Mapping[str, Any]) -> dict[str, Any]:
    """把图状态裁剪为对外返回的精简视图。

    只暴露复核/排查所需的字段，不把整个 state（含大对象）回传给前端。
    Pydantic 模型在此处降为普通字段，避免接口层与领域模型耦合。

    ⚠️ `claim_amount` / `pay_amount` / `image_count` 是**复核必需信息**：
    人工复核人要判断"这笔金额是否该放行""举证是否充分"，缺了它们复核台无法工作。
    """
    action = values.get("action")
    risk = values.get("risk")
    request = values.get("request")
    evidence = values.get("evidence")
    return {
        "order_id": request.order_id if request else None,
        # 复核依据：申请金额与举证图数量
        "claim_amount": request.claim_amount if request else None,
        "pay_amount": request.pay_amount if request else None,
        "refund_reason_code": (
            request.refund_reason_code.value
            if request and hasattr(request.refund_reason_code, "value")
            else None
        ),
        "image_count": evidence.image_count if evidence else None,
        "conclusion": values.get("conclusion"),
        "hit_rules": list(values.get("hits", [])),
        "hit_details": list(values.get("hit_details", [])),
        "rule_version": values.get("rule_version"),
        "risk_level": risk.level if risk else None,
        "risk_flags": list(risk.flags) if risk else [],
        "action": action.action if action else None,
        "action_executed": action.executed if action else False,
        "shadow_mode": action.shadow if action else None,
        "intake_error": values.get("intake_error"),
        "review_decision": values.get("review_decision"),
        "reviewer": values.get("reviewer"),
        "audit_event_count": len(values.get("audit_events", [])),
    }


def summarize_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """裁剪 `ainvoke` 的返回值。

    与 `get_thread_state` 的区别：`ainvoke` 的返回值里带 `__interrupt__` 键，
    可据此直接判定本次调用是否在中断处停下。
    """
    summary = summarize_state(result)
    summary["status"] = STATUS_PENDING_REVIEW if result.get("__interrupt__") else STATUS_COMPLETED
    return summary


def _extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从 StateSnapshot.tasks 中提取中断载荷（复核台需要展示"为什么挂起"）。

    LangGraph 把中断信息放在 `snapshot.tasks[*].interrupts[*].value`，
    其值即 `interrupt()` 调用时传入的对象。
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return {"value": getattr(item, "value", None)}
    return None


async def get_thread_state(graph: Any, thread_id: str) -> dict[str, Any] | None:
    """取某个受理号的当前状态。不存在时返回 None。

    "是否挂起"以 `snapshot.next` 非空为准——这是 LangGraph 的官方语义
    （next 列出待执行的节点；挂起在 interrupt 上时该值非空）。
    """
    snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot or not snapshot.values:
        return None

    summary = summarize_state(snapshot.values)
    summary["status"] = STATUS_PENDING_REVIEW if snapshot.next else STATUS_COMPLETED
    summary["pending_nodes"] = list(snapshot.next)
    summary["interrupt"] = _extract_interrupt(snapshot)
    return summary


async def list_pending_reviews(graph: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """列出当前挂起待人工复核的受理号（最近活动优先）。

    实现说明：
    - 用 checkpointer 的 `alist()` 遍历检查点，而**不直接读底层数据表**——
      这样将来把检查点后端从 SQLite 换成 MySQL 时本函数无需改动；
    - `alist` 返回的是**检查点记录**（每个 superstep 一条），同一受理号可能有多条，故需去重；
    - 扫描窗口按 `limit * 20` 放大（单笔申请约 7~10 个 superstep，留足余量）。

    ⚠️ **必须分两阶段**（重要，不要合并回单循环）：
    `AsyncSqliteSaver` 用一把 `asyncio.Lock` 串行化对连接的访问，而 `alist()` 在
    **整个迭代期间持有该锁**。若在迭代体内调用 `aget_state()`（同样要拿这把锁），
    会形成自锁死锁——表现为查询永久挂起、无异常、无输出。
    因此先收完 thread_id 列表（迭代结束、锁释放），再逐个查状态。
    """
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None:
        # 未启用持久化时无法挂起/恢复，不存在待复核项
        return []

    # ---- 阶段一：收集受理号（去重保序；最近活动的排在前）----
    thread_ids: list[str] = []
    seen: set[str] = set()
    async for item in checkpointer.alist(None, limit=limit * 20):
        thread_id = item.config["configurable"]["thread_id"]
        if thread_id not in seen:
            seen.add(thread_id)
            thread_ids.append(thread_id)

    # ---- 阶段二：逐个取状态，筛出挂起项 ----
    pending: list[dict[str, Any]] = []
    for thread_id in thread_ids:
        state = await get_thread_state(graph, thread_id)
        if state and state["status"] == STATUS_PENDING_REVIEW:
            pending.append(state)
            if len(pending) >= limit:
                break

    return pending
