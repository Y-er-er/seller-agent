"""审计读写：`decision_audit` 表的落库与查询。

**写路径红线**：审计写入失败**绝不可阻断判定链路**。
`save_decision()` 捕获所有异常、记录告警并返回 False；调用方据此在审计事件中标注是否落库成功。

**读路径（查询）策略不同**：查询是用户主动发起的请求，失败**不可静默**——
若返回空列表，使用者会误以为"没有数据"。故查询函数让异常透出，由接入层转成明确错误。

表结构见 `sql/decision_audit.sql`。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

_INSERT_SQL = (
    "INSERT INTO decision_audit"
    " (request_id, order_id, conclusion, hit_rules, rule_version, field_values,"
    "  risk_level, action, executed, shadow, review_decision, reviewer)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)

# 落库字段（顺序与 SQL 占位符一一对应）
_COLUMNS = (
    "request_id",
    "order_id",
    "conclusion",
    "hit_rules",
    "rule_version",
    "field_values",
    "risk_level",
    "action",
    "executed",
    "shadow",
    "review_decision",
    "reviewer",
)


def _to_params(record: dict[str, Any]) -> tuple:
    """把审计记录整理成 SQL 参数元组。

    - `hit_rules` 由列表拼成逗号分隔字符串（列类型 varchar）；
    - `field_values` 序列化为 JSON 字符串（列类型 json）。
    """
    hit_rules = record.get("hit_rules") or []
    if isinstance(hit_rules, (list, tuple)):
        hit_rules = ",".join(str(x) for x in hit_rules)

    field_values = record.get("field_values")
    if field_values is not None and not isinstance(field_values, str):
        field_values = json.dumps(field_values, ensure_ascii=False, default=str)

    merged = {**record, "hit_rules": hit_rules, "field_values": field_values}
    return tuple(merged.get(col) for col in _COLUMNS)


def _insert(record: dict[str, Any]) -> int:
    """同步插入（在线程池中执行）。返回受影响行数。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_INSERT_SQL, _to_params(record))
        return cur.rowcount


async def save_decision(record: dict[str, Any]) -> bool:
    """把一条判定记录写入 `decision_audit`。

    返回 True 表示已落库；False 表示**已降级**（未启用 / 未配置 / 写入失败）。
    任何情况下都不会向上抛异常——审计是旁路，不能拖垮判定。
    """
    if not settings.audit_db_enabled:
        return False
    if not settings.mysql_uri:
        logger.warning("未配置 MYSQL_URI，审计落库已跳过")
        return False

    try:
        await run_sync(_insert, record)
        return True
    except Exception as exc:  # noqa: BLE001 — 审计失败必须吞掉并降级
        logger.warning("审计落库失败（已降级，不影响判定结论）：%s: %s", type(exc).__name__, exc)
        return False


# ============================================================
# 查询（读路径）
# ============================================================

_SELECT_COLUMNS = (
    "id, request_id, order_id, conclusion, hit_rules, rule_version, field_values,"
    " risk_level, action, executed, shadow, review_decision, reviewer, created_at"
)

# 与 _SELECT_COLUMNS 一一对应，用于把游标行转成字典
_RESULT_FIELDS = (
    "id",
    "request_id",
    "order_id",
    "conclusion",
    "hit_rules",
    "rule_version",
    "field_values",
    "risk_level",
    "action",
    "executed",
    "shadow",
    "review_decision",
    "reviewer",
    "created_at",
)


def _row_to_dict(row: tuple) -> dict[str, Any]:
    """把数据库行转成对外可 JSON 化的字典。

    三处规整：
    - `hit_rules` 存储为逗号分隔字符串 → 还原为列表；
    - `field_values` 存储为 JSON → 还原为对象（解析失败时保留原文，不抛错）；
    - `DATETIME` → ISO 字符串；TINYINT 布尔位 → 真正的 bool。
    """
    record = dict(zip(_RESULT_FIELDS, row))

    hits = record.get("hit_rules") or ""
    record["hit_rules"] = [x for x in hits.split(",") if x] if isinstance(hits, str) else list(hits)

    field_values = record.get("field_values")
    if isinstance(field_values, str) and field_values:
        try:
            record["field_values"] = json.loads(field_values)
        except json.JSONDecodeError:
            # 保留原文——怀疑是坏数据时，把原样内容交给排查者比丢掉更有用
            pass

    created = record.get("created_at")
    if isinstance(created, (datetime, date)):
        record["created_at"] = created.isoformat(sep=" ")

    record["executed"] = bool(record.get("executed"))
    record["shadow"] = bool(record.get("shadow"))
    return record


def _fetch(conclusion: str | None, order_id: str | None, limit: int, offset: int) -> dict[str, Any]:
    """同步分页查询（在线程池中执行）。返回 `{"total": n, "items": [...]}`。"""
    where: list[str] = []
    params: list[Any] = []
    if conclusion:
        where.append("conclusion = %s")
        params.append(conclusion)
    if order_id:
        where.append("order_id = %s")
        params.append(order_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM decision_audit{clause}", params)
        total = cur.fetchone()[0]

        # 最新在前；limit/offset 实现分页
        cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM decision_audit{clause}"
            " ORDER BY id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        items = [_row_to_dict(row) for row in cur.fetchall()]

    return {"total": total, "items": items}


async def list_decisions(
    *,
    conclusion: str | None = None,
    order_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """分页查询审计记录（M5：审计查询 / 一致率分析的数据源）。

    与写路径不同，**异常直接透出**：查询失败必须让调用方明确报错，
    不能返回空列表造成"没有数据"的误判。
    """
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，无法查询审计记录")
    return await run_sync(_fetch, conclusion, order_id, limit, offset)


def _fetch_by_request(request_id: str) -> list[dict[str, Any]]:
    """按受理号取全部审计记录（同步，线程池执行）。

    一次受理号理论上只有一行；但保留多行能力——若同一受理号被重复提交，
    全部返回以便排查（不静默丢弃历史）。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM decision_audit WHERE request_id = %s ORDER BY id ASC",
            (request_id,),
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


async def get_decisions_by_request(request_id: str) -> list[dict[str, Any]]:
    """查询某受理号的全部审计记录（按写入顺序）。查不到则返回空列表。"""
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，无法查询审计记录")
    return await run_sync(_fetch_by_request, request_id)


# ============================================================
# 统计（M5：一致率报表 / 调优依据）
# ============================================================

# 判定结论的固定枚举。统计结果里**补齐未出现的结论**（计 0），
# 避免前端因缺键而画不出完整的分布图。
_CONCLUSIONS = ("auto_pass", "auto_reject", "request_evidence", "to_human")


def _build_clause(since: str | None, until: str | None) -> tuple[str, list[Any]]:
    """构造时间范围 WHERE 子句（参数化，不做字符串拼接）。"""
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append("created_at >= %s")
        params.append(since)
    if until:
        where.append("created_at <= %s")
        params.append(until)
    return (f" WHERE {' AND '.join(where)}" if where else ""), params


def _stats(since: str | None, until: str | None) -> dict[str, Any]:
    """同步聚合统计（在线程池中执行）。

    统计口径说明：
    - `agreement_rate` = 人工**维持**数 / (维持 + 改判) 数，即"规则结论被人工认可的比例"。
      分母为 0 时返回 **None**（无数据 ≠ 一致率 0，二者不能混淆）。
    - `rule_hits` 用 Python 侧统计：`hit_rules` 是逗号分隔字符串，
      通用 SQL 无法一次聚合多项（FIND_IN_SET 需已知规则集且不利于扩展）。
      审计表是低频写入、单笔一行，拉取全量命中列的代价可接受。
    """
    clause, params = _build_clause(since, until)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM decision_audit{clause}", params)
        total = cur.fetchone()[0]

        cur.execute(
            f"SELECT conclusion, COUNT(*) FROM decision_audit{clause} GROUP BY conclusion", params
        )
        raw_conclusion = {row[0]: row[1] for row in cur.fetchall()}

        # 人工复核只发生在 to_human 路径；其余路径本就没有人工参与，不能计入一致率分母
        review_clause = clause + (" AND " if clause else " WHERE ") + "conclusion = 'to_human'"
        cur.execute(
            f"SELECT review_decision, COUNT(*) FROM decision_audit{review_clause}"
            " GROUP BY review_decision",
            params,
        )
        review_counts: dict[str, int] = {}
        for decision, count in cur.fetchall():
            # review_decision 为 NULL 表示尚未有复核结论（异常/中断场景）
            review_counts[decision or "unrecorded"] = count

        cur.execute(f"SELECT hit_rules FROM decision_audit{clause}", params)
        rule_hits: dict[str, int] = {}
        for (hits,) in cur.fetchall():
            if not hits:
                continue
            for rule_id in hits.split(","):
                rule_id = rule_id.strip()
                if rule_id:
                    rule_hits[rule_id] = rule_hits.get(rule_id, 0) + 1

        cur.execute(
            f"SELECT executed, COUNT(*) FROM decision_audit{clause} GROUP BY executed", params
        )
        executed_counts = {bool(row[0]): row[1] for row in cur.fetchall()}

    maintained = review_counts.get("maintain", 0)
    overridden = review_counts.get("override", 0)
    reviewed = maintained + overridden

    return {
        "total": total,
        "time_range": {"since": since, "until": until},
        # 补齐未出现的结论，保证分布图结构稳定
        "conclusion_distribution": {c: raw_conclusion.get(c, 0) for c in _CONCLUSIONS},
        "review_distribution": {
            "maintain": maintained,
            "override": overridden,
            "unrecorded": review_counts.get("unrecorded", 0),
        },
        # 人工复核参与率（复核数 / 转人工数）：过低说明影子模式样本不足，不足以支撑调优
        "review_coverage": (reviewed / raw_conclusion.get("to_human", 0))
        if raw_conclusion.get("to_human")
        else None,
        "agreement_rate": (maintained / reviewed) if reviewed else None,
        # 按命中次数倒序，便于直接展示"哪些规则在拦人"
        "rule_hits": [
            {"rule_id": k, "count": v}
            for k, v in sorted(rule_hits.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        # 资金动作落地情况：影子模式下应恒为 0，可用于验证影子模式生效
        "execution_stats": {
            "executed": executed_counts.get(True, 0),
            "not_executed": executed_counts.get(False, 0),
        },
    }


async def get_stats(*, since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """判定结论的聚合统计（M5）。异常透出，由接入层转明确错误。"""
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，无法统计审计记录")
    return await run_sync(_stats, since, until)
