"""对话层数据访问（P1）。

与 `audit_repo` 的关系与差异：
- 二者都走 `run_sync`（pymysql 是同步驱动，不能阻塞事件循环）；
- **写路径**：都**吞异常并降级**——对话不能因为一次落库失败而整体不可用；
  但 chat 的写失败会**返回 False** 让调用方知道（会话状态错乱比审计缺行更影响体验）；
- **读路径**：都**透出异常**——查询是用户主动请求，静默返回空会让人误判"没有数据"。

表结构见 `sql/chat_schema.sql`。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

_JSON_FIELDS = ("attachments", "tool_calls")


def _require_uri() -> None:
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，无法访问对话数据")


def _json_or_none(value: Any) -> str | None:
    """把可序列化对象转 JSON 字符串；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """把 DB 行规整为可直接 JSON 序列化的字典（datetime → ISO 字符串）。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif key in _JSON_FIELDS and isinstance(value, (str, bytes)):
            raw = value.decode("utf-8") if isinstance(value, bytes) else value
            try:
                out[key] = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                out[key] = raw
        else:
            out[key] = value
    return out


# ============================================================
# 会话主体
# ============================================================


def _create_case(case_id: str, order_id: str, goods_no: str, timeout_at: datetime | None) -> bool:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_case (case_id, order_id, goods_no, status, timeout_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (case_id, order_id, goods_no or None, "CREATED", timeout_at),
            )
        return True
    except Exception as exc:  # noqa: BLE001 —— 唯一键冲突（重复创建）也走这里
        logger.warning("创建会话失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def create_case(
    case_id: str, order_id: str, goods_no: str = "", timeout_at: datetime | None = None
) -> bool:
    """创建会话。`case_id` 唯一，重复创建返回 False（入口幂等）。

    `goods_no` 由受理方从订单事实带入 —— 它是「商品 × 售后联动」的关联键，
    取不到时留空（**不阻断受理**：售后不该因为没有商品关联而被卡住）。
    """
    _require_uri()
    return await run_sync(_create_case, case_id, order_id, goods_no, timeout_at)


def _get_case(case_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM conversation_case WHERE case_id=%s", (case_id,))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
    return _normalize(dict(zip(columns, row)))


async def get_case(case_id: str) -> dict[str, Any] | None:
    """读取会话主体；不存在返回 None。**异常透出**。"""
    _require_uri()
    return await run_sync(_get_case, case_id)


def _update_case(
    case_id: str,
    status: str | None = None,
    escalated_reason: str | None = None,
    round_delta: int = 0,
    last_seq: int | None = None,
    timeout_at: datetime | None = None,
    workflow_status: str | None = None,
    assignee: str | None = None,
    decision_result: str | None = None,
    decision_amount: float | None = None,
    confirmed_by: str | None = None,
    confirmed_at: datetime | None = None,
) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status=%s")
        params.append(status)
    if escalated_reason is not None:
        sets.append("escalated_reason=%s")
        params.append(escalated_reason)
    if round_delta:
        sets.append("round=round+%s")
        params.append(round_delta)
    if last_seq is not None:
        # 只增不减，避免乱序写入把序号改小
        sets.append("last_seq=GREATEST(last_seq, %s)")
        params.append(last_seq)
    if timeout_at is not None:
        sets.append("timeout_at=%s")
        params.append(timeout_at)
    if workflow_status is not None:
        sets.append("workflow_status=%s")
        params.append(workflow_status)
    if assignee is not None:
        sets.append("assignee=%s")
        params.append(assignee)
    if decision_result is not None:
        sets.append("decision_result=%s")
        params.append(decision_result)
    if decision_amount is not None:
        sets.append("decision_amount=%s")
        params.append(decision_amount)
    if confirmed_by is not None:
        sets.append("confirmed_by=%s")
        params.append(confirmed_by)
    if confirmed_at is not None:
        sets.append("confirmed_at=%s")
        params.append(confirmed_at)
    if not sets:
        return True

    params.append(case_id)
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE conversation_case SET {', '.join(sets)} WHERE case_id=%s", params)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新会话失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def update_case(
    case_id: str,
    status: str | None = None,
    escalated_reason: str | None = None,
    round_delta: int = 0,
    last_seq: int | None = None,
    timeout_at: datetime | None = None,
    workflow_status: str | None = None,
    assignee: str | None = None,
    decision_result: str | None = None,
    decision_amount: float | None = None,
    confirmed_by: str | None = None,
    confirmed_at: datetime | None = None,
) -> bool:
    """更新会话状态 / 轮次 / 序号 / 超时时刻 / 工单字段（只更新传入的字段）。"""
    _require_uri()
    return await run_sync(
        _update_case,
        case_id, status, escalated_reason, round_delta, last_seq, timeout_at,
        workflow_status, assignee, decision_result, decision_amount, confirmed_by, confirmed_at,
    )


def _list_cases_by_goods(goods_no: str, limit: int) -> list[dict[str, Any]]:
    """按商品编号查工单（「商品 × 售后联动」的数据来源）。

    ⚠️ 本表**没有 seller_id 列**（售后域早于商品域），故只按 `goods_no` 过滤 ——
    而 `goods_no` 本身已隐含卖家（同一卖家的编号唯一）。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT case_id, order_id, status, workflow_status, assignee, escalated_reason, "
            "decision_result, decision_amount, created_at, updated_at "
            "FROM conversation_case WHERE goods_no=%s ORDER BY created_at DESC LIMIT %s",
            (goods_no, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_cases_by_goods(goods_no: str, limit: int = 200) -> list[dict[str, Any]]:
    """按商品编号查工单（按创建时间倒序）。"""
    _require_uri()
    return await run_sync(_list_cases_by_goods, goods_no, limit)


def _aggregate_cases_by_goods(goods_nos: list[str]) -> dict[str, dict[str, int]]:
    """**一次查完**多个商品的工单计数与赔付数（`健康度榜` 专用）。

    ⚠️ 为什么要有这个函数：逐商品调 `list_cases_by_goods` 是 N+1，而连接层
    每次查询都新建一条 MySQL 连接（远程库单次握手实测约 45ms）。22 件商品
    就是 22 次握手 ≈ 1 秒 —— 实测概览接口因此要 0.95s，且商品越多线性恶化。
    健康度榜只要 `total / costly` 两个聚合值，一条 `GROUP BY` 就够。

    口径必须与 `_summarize()` 一致：赔付类只算 `refund` / `reship`
    （补发也是履约失败）；`decision_result` 为 NULL 的行计入 total 但不计入 costly
    —— 与"未判定不当作没退货"一致，它落在分母里。
    """
    if not goods_nos:
        return {}
    # 去重的同时保持对调用方透明（IN 列表长度由调用方商品数决定，上限 200）
    keys = list(dict.fromkeys(goods_nos))
    placeholders = ",".join(["%s"] * len(keys))
    sql = (
        "SELECT goods_no, COUNT(*) AS total, "
        "       COALESCE(SUM(decision_result IN ('refund','reship')), 0) AS costly "
        f"FROM conversation_case WHERE goods_no IN ({placeholders}) GROUP BY goods_no"
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(keys))
        return {
            str(row[0]): {"total": int(row[1]), "costly": int(row[2])}
            for row in cur.fetchall()
        }


async def aggregate_cases_by_goods(goods_nos: list[str]) -> dict[str, dict[str, int]]:
    """批量版的工单聚合（返回 `{goods_no: {total, costly}}`；无工单的商品不出现）。"""
    _require_uri()
    return await run_sync(_aggregate_cases_by_goods, goods_nos)


def _list_cases_by_workflow(workflow_status: str | None, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        if workflow_status:
            cur.execute(
                "SELECT * FROM conversation_case WHERE workflow_status=%s "
                "ORDER BY updated_at DESC LIMIT %s",
                (workflow_status, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM conversation_case ORDER BY updated_at DESC LIMIT %s",
                (limit,),
            )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_cases_by_workflow(workflow_status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """按工单状态列会话（`None` = 全部），供指挥台收件箱使用。"""
    _require_uri()
    return await run_sync(_list_cases_by_workflow, workflow_status, limit)


def _list_cases_by_status(status: str, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM conversation_case WHERE status=%s ORDER BY updated_at DESC LIMIT %s",
            (status, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_cases_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    _require_uri()
    return await run_sync(_list_cases_by_status, status, limit)


# ============================================================
# 会话消息
# ============================================================


def _append_message(
    case_id: str,
    seq: int,
    role: str,
    content: str | None,
    attachments: Any,
    tool_calls: Any,
    guardrail_event: str | None,
) -> bool:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_message"
                " (case_id, seq, role, content, attachments, tool_calls, guardrail_event)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    case_id,
                    seq,
                    role,
                    content,
                    _json_or_none(attachments),
                    _json_or_none(tool_calls),
                    guardrail_event,
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 —— 唯一键 (case_id, seq) 冲突也在此
        logger.warning("写入会话消息失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def append_message(
    case_id: str,
    seq: int,
    role: str,
    content: str | None = None,
    attachments: Any = None,
    tool_calls: Any = None,
    guardrail_event: str | None = None,
) -> bool:
    """追加一条会话消息。`(case_id, seq)` 唯一 → 重复提交同序号被拒绝（天然幂等）。"""
    _require_uri()
    return await run_sync(
        _append_message, case_id, seq, role, content, attachments, tool_calls, guardrail_event
    )


#: 「最近 N 条」的取法：先按 seq 倒序截断，再正序回来。
#: 单独提出来是为了能被测试**静态钉住**（回退成 `ASC LIMIT` 时测试会失败）。
SQL_RECENT_MESSAGES = (
    "SELECT * FROM ("
    "  SELECT * FROM conversation_message WHERE case_id=%s ORDER BY seq DESC LIMIT %s"
    ") t ORDER BY t.seq ASC"
)


def _list_messages(case_id: str, limit: int | None) -> list[dict[str, Any]]:
    """按 seq 升序返回；给了 `limit` 时取**最近** limit 条。

    ⚠️ `ORDER BY seq ASC LIMIT n` 取到的是**最早**的 n 条（不是最近的）——
    售后会话一旦超过 20 条，`build_history` 拿到的就是开头那段，
    **买家刚发的那条消息进不了模型**，智能体会一直回应早前那一句。
    所以限流时必须"先倒序截断、再正序"。
    """
    if limit:
        sql, params = SQL_RECENT_MESSAGES, [case_id, limit]
    else:
        sql = "SELECT * FROM conversation_message WHERE case_id=%s ORDER BY seq ASC"
        params = [case_id]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_messages(case_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    _require_uri()
    return await run_sync(_list_messages, case_id, limit)


# ============================================================
# 槽位（模型抽取的**推断**，不参与判定）
# ============================================================


def _upsert_slot(
    case_id: str, slot: str, value: str | None, confidence: float | None, source_seq: int | None
) -> bool:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO case_slot (case_id, slot, value, confidence, source_seq)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value), confidence=VALUES(confidence),"
                " source_seq=VALUES(source_seq), extracted_at=CURRENT_TIMESTAMP",
                (case_id, slot, value, confidence, source_seq),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入槽位失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def upsert_slot(
    case_id: str,
    slot: str,
    value: str | None,
    confidence: float | None = None,
    source_seq: int | None = None,
) -> bool:
    """写入/更新槽位。**必须带 `source_seq`** —— 卷宗要能点回买家原话的那一轮。"""
    _require_uri()
    return await run_sync(_upsert_slot, case_id, slot, value, confidence, source_seq)


def _list_slots(case_id: str) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM case_slot WHERE case_id=%s ORDER BY id ASC", (case_id,))
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_slots(case_id: str) -> list[dict[str, Any]]:
    _require_uri()
    return await run_sync(_list_slots, case_id)


# ============================================================
# 护栏事件
# ============================================================


def _record_guardrail(case_id: str | None, event_type: str, detail: str | None) -> bool:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guardrail_event (case_id, type, detail) VALUES (%s, %s, %s)",
                (case_id, event_type, detail),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入护栏事件失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def record_guardrail(
    case_id: str | None, event_type: str, detail: str | None = None
) -> bool:
    _require_uri()
    return await run_sync(_record_guardrail, case_id, event_type, detail)


def _list_guardrails(case_id: str, limit: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM guardrail_event WHERE case_id=%s ORDER BY id DESC LIMIT %s",
            (case_id, limit),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [_normalize(dict(zip(columns, row))) for row in rows]


async def list_guardrails(case_id: str, limit: int = 50) -> list[dict[str, Any]]:
    _require_uri()
    return await run_sync(_list_guardrails, case_id, limit)


def _count_guardrail_since(event_type: str, since: datetime) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM guardrail_event WHERE type=%s AND created_at>=%s",
            (event_type, since),
        )
        return int(cur.fetchone()[0])


async def count_guardrail_since(event_type: str, since: datetime) -> int:
    """统计某类护栏事件在窗口内的条数。

    用途：成本闸门"状态跃迁时才汇报"——避免每轮都写一条 `cost_warning` 刷表。
    """
    _require_uri()
    return await run_sync(_count_guardrail_since, event_type, since)


# ============================================================
# 模型用量（成本闸门记账）
# ============================================================


def _insert_usage(
    case_id: str | None,
    seq: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    amount: float,
) -> bool:
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_usage"
                " (case_id, seq, model, input_tokens, output_tokens, amount)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (case_id, seq, model, input_tokens, output_tokens, amount),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入用量失败（已降级）：%s: %s", type(exc).__name__, exc)
        return False


async def insert_usage(
    case_id: str | None,
    seq: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    amount: float,
) -> bool:
    _require_uri()
    return await run_sync(
        _insert_usage, case_id, seq, model, input_tokens, output_tokens, amount
    )


def _sum_usage(since: datetime | None, model: str | None = None) -> dict[str, Any]:
    """汇总用量。

    `model` 非空时**只统计该模型** —— 免费额度是按模型发放的，取"当前模型已用多少"
    必须带这个过滤；不带就是全模型合计（用于"这个月一共花了多少钱"）。
    """
    sql = ("SELECT COALESCE(SUM(amount),0), COALESCE(SUM(input_tokens+output_tokens),0),"
           " COUNT(*) FROM llm_usage")
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("created_at>=%s")
        params.append(since)
    if model:
        where.append("model=%s")
        params.append(model)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        amount, tokens, calls = cur.fetchone()
    return {"amount": float(amount or 0), "tokens": int(tokens or 0), "calls": int(calls or 0)}


async def sum_usage(since: datetime | None = None, model: str | None = None) -> dict[str, Any]:
    """汇总用量。

    `since=None` 表示全部历史（用于免费额度的**全量累计估算**）；
    `model` 传入时只算该模型（**额度判定必须传**，否则换模型后会继承旧模型的用量）。
    """
    _require_uri()
    return await run_sync(_sum_usage, since, model)
