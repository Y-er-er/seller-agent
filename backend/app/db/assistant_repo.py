"""AI 助手的数据访问（对话原文 + 待确认动作）。

与 `goods_repo` 同一套取向：同步 pymysql + `run_sync` 线程池；写路径失败上抛。

两张表（见 `sql/assistant_schema.sql`）：

- `assistant_message` —— 对话原文。**与售后对话分表**：两条对话的语义与生命周期不同，
  混存会污染彼此的审计链。
- `pending_action` —— 破坏性动作的"待确认"落点。**没有它，AI 就能直接删数据。**
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.db.connection import connect, run_sync

logger = logging.getLogger(__name__)

DEFAULT_SELLER_ID = "default"

#: 待确认动作的状态
ACTION_PENDING = "pending"
ACTION_CONFIRMED = "confirmed"
ACTION_CANCELLED = "cancelled"
#: 超时未处理自动作废 —— 避免"忘了就永远亮着红点"
ACTION_EXPIRED = "expired"
#: **已确认但执行失败**。
#:
#: 必须与 `confirmed` 分开：否则"近期已完成"会把失败当成"已办妥"，
#: 卖家再提一次时被判成"刚做过"而不给建卡（实测踩到：生图失败后无法重试）。
ACTION_FAILED = "failed"
#: **执行中（抢占标记）**。
#:
#: 确认动作会先把它从 pending **原子地**改成 running，抢到才执行 —— 否则连点两下
#: （或两个页面同时点）会**执行两次、计费两次**：两个请求都读到 pending，
#: 都去跑 handler，最后只有一个能改成 confirmed，另一个报 409 却已经"做过了"。
ACTION_RUNNING = "running"

#: 待确认项的有效期（小时）
PENDING_TTL_HOURS = 24
#: running 超过多少分钟判定为"执行中断"（进程被杀 / 崩溃时留下的死状态）
RUNNING_STALE_MINUTES = 30


def _require_uri() -> None:
    if not settings.mysql_uri:
        raise RuntimeError("未配置 MYSQL_URI，AI 助手无法访问数据库")


#: `batch_id` 列的可用性只在进程内确认一次
_BATCH_COLUMN_READY = False


def _ensure_batch_column() -> None:
    """确保 `pending_action.batch_id` 存在（幂等）。

    **为什么值得让它自动补**：缺这一列时，建卡会整条失败 ——
    表现是"助手说话了但一张确认卡都不出现"，报错离真实原因很远。
    标准路径仍是 `sql/migrations/2026-10-20_add_pending_action_batch.sql`；
    这里只是兜底，避免"忘跑迁移 = 助手不可用"。
    """
    global _BATCH_COLUMN_READY
    if _BATCH_COLUMN_READY:
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'pending_action' "
            "AND column_name = 'batch_id'"
        )
        if int(cur.fetchone()[0]) == 0:
            logger.warning("pending_action 缺少 batch_id 列，自动补上（建议改为跑 sql/migrations/ 下的迁移）")
            cur.execute(
                "ALTER TABLE pending_action ADD COLUMN batch_id VARCHAR(40) NULL "
                "COMMENT '批次：同一次请求里的多个待确认动作共享一个值' AFTER status"
            )
            cur.execute("ALTER TABLE pending_action ADD KEY idx_batch (batch_id)")
    _BATCH_COLUMN_READY = True


def _loads(raw: Any) -> Any:
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


# ============================================================
# 对话消息
# ============================================================


def _next_seq(thread_id: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM assistant_message WHERE thread_id=%s",
            (thread_id,),
        )
        return int(cur.fetchone()[0])


def _append_message(
    seller_id: str, thread_id: str, seq: int, role: str, content: str,
    tool_name: str | None, tool_args: dict[str, Any] | None,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO assistant_message (seller_id, thread_id, seq, role, content, tool_name, tool_args) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (seller_id, thread_id, seq, role, content, tool_name,
             json.dumps(tool_args, ensure_ascii=False) if tool_args is not None else None),
        )
        return int(cur.lastrowid)


async def append_message(
    thread_id: str,
    role: str,
    content: str = "",
    *,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    seller_id: str = DEFAULT_SELLER_ID,
) -> dict[str, Any]:
    """追加一条对话消息（自动分配 seq）。返回写入的完整行。"""
    _require_uri()
    seq = await run_sync(_next_seq, thread_id)
    mid = await run_sync(_append_message, seller_id, thread_id, seq, role, content, tool_name, tool_args)
    return {
        "id": mid, "thread_id": thread_id, "seq": seq, "role": role,
        "content": content, "tool_name": tool_name, "tool_args": tool_args,
    }


#: 「最近 N 条」的取法：先按 seq 倒序截断，再正序回来。
#: 单独提出来是为了能被测试**静态钉住**（回退成 `ASC LIMIT` 时测试会失败）。
SQL_RECENT_MESSAGES = (
    "SELECT * FROM ("
    "  SELECT id, thread_id, seq, role, content, tool_name, tool_args, created_at "
    "  FROM assistant_message WHERE thread_id=%s ORDER BY seq DESC LIMIT %s"
    ") t ORDER BY t.seq ASC"
)


def _list_messages(thread_id: str, limit: int) -> list[dict[str, Any]]:
    """取**最近** `limit` 条，仍按 seq 升序返回。

    ⚠️ 必须"先倒序截断、再正序" —— 直接 `ORDER BY seq ASC LIMIT n` 取到的是**最早**的 n 条。
    实测代价很大（2026-10-20）：会话超过 40 条后，装配出的模型上下文停在历史中段，
    **卖家刚发的那句话根本没进模型**，模型于是接着"续写"窗口最后一条助手消息 ——
    问「售后政策中哪些不予受理？」，它连续四轮答"电竞椅文案已更新"。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SQL_RECENT_MESSAGES, (thread_id, limit))
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(columns, row))
        item["tool_args"] = _loads(item.get("tool_args"))
        out.append(item)
    return out


async def list_messages(thread_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """按 seq 顺序返回对话历史。"""
    _require_uri()
    return await run_sync(_list_messages, thread_id, limit)


def _reset_thread(thread_id: str, seller_id: str) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM assistant_message WHERE thread_id=%s AND seller_id=%s",
                    (thread_id, seller_id))
        msgs = cur.rowcount
        # 未决的待确认动作一并作废（否则"新对话"后还会冒出一个悬空的确认卡）
        cur.execute(
            "UPDATE pending_action SET status=%s, decided_at=NOW() "
            "WHERE thread_id=%s AND seller_id=%s AND status=%s",
            (ACTION_CANCELLED, thread_id, seller_id, ACTION_PENDING),
        )
        actions = cur.rowcount
    return {"messages": msgs, "actions_cancelled": actions}


async def reset_thread(thread_id: str, seller_id: str = DEFAULT_SELLER_ID) -> dict[str, int]:
    """清空该线程（新对话）：删消息 + 作废未决动作。"""
    _require_uri()
    return await run_sync(_reset_thread, thread_id, seller_id)


# ============================================================
# 待确认动作
# ============================================================


def _canonical(raw: Any) -> str:
    """把入参规范成可比较的字符串（键排序、去空白差异）。

    用来判断"是不是同一个待确认动作" —— 直接比 JSON 文本会因为键顺序/空格不同而误判。
    """
    if raw is None:
        return "{}"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    try:
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(raw)


def _find_pending(
    thread_id: str, seller_id: str, tool_name: str, args_json: str
) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_action WHERE thread_id=%s AND seller_id=%s "
            "AND tool_name=%s AND status=%s ORDER BY id DESC",
            (thread_id, seller_id, tool_name, ACTION_PENDING),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    target = _canonical(args_json)
    for row in rows:
        item = dict(zip(columns, row))
        if _canonical(item.get("arguments")) == target:
            item["arguments"] = _loads(item.get("arguments")) or {}
            return item
    return None


async def find_pending(
    thread_id: str, tool_name: str, arguments: dict[str, Any], seller_id: str = DEFAULT_SELLER_ID
) -> dict[str, Any] | None:
    """查**同工具同参数**的未决动作；没有则返回 None。

    存在的意义：模型可能在同一件事上反复调用工具（比如卖家重申了一遍要求），
    每次调用都新建一张确认卡，就会看到"好几张一模一样的卡"，
    卖家逐张点 → **同一件事被重复执行、重复计费**。
    """
    _require_uri()
    return await run_sync(_find_pending, thread_id, seller_id, tool_name, _canonical(arguments))


def _find_recent_done(
    thread_id: str, seller_id: str, tool_name: str, args_json: str, minutes: int
) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_action WHERE thread_id=%s AND seller_id=%s "
            "AND tool_name=%s AND status=%s AND decided_at >= (NOW() - INTERVAL %s MINUTE) "
            "ORDER BY id DESC",
            (thread_id, seller_id, tool_name, ACTION_CONFIRMED, minutes),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    target = _canonical(args_json)
    for row in rows:
        item = dict(zip(columns, row))
        if _canonical(item.get("arguments")) == target:
            item["arguments"] = _loads(item.get("arguments")) or {}
            return item
    return None


async def find_recent_done(
    thread_id: str, tool_name: str, arguments: dict[str, Any],
    seller_id: str = DEFAULT_SELLER_ID, minutes: int = 30,
) -> dict[str, Any] | None:
    """查最近 `minutes` 分钟内**已确认执行过**的同参数动作。

    存在的意义：卖家确认某个动作后，模型会在"续跑"那一轮**再次提交同一个动作**
    （它看到历史里有这个意图，就想再确认一次）。若此时旧卡已 confirmed，
    幂等查不到未决卡 → 就会**新建一张多余的卡**，而卖家看到的是"事情已经做完了，
    怎么还有一张卡等着确认"（实测踩到）。
    """
    _require_uri()
    return await run_sync(
        _find_recent_done, thread_id, seller_id, tool_name, _canonical(arguments), minutes
    )


def _create_action(
    seller_id: str, thread_id: str, tool_name: str,
    arguments: dict[str, Any], summary: str, detail: str, batch_id: str,
) -> int:
    _ensure_batch_column()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_action "
            "(seller_id, thread_id, tool_name, arguments, summary, detail, batch_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (seller_id, thread_id, tool_name, json.dumps(arguments, ensure_ascii=False),
             summary, detail, batch_id or None),
        )
        return int(cur.lastrowid)


async def create_action(
    thread_id: str, tool_name: str, arguments: dict[str, Any], summary: str, detail: str = "",
    seller_id: str = DEFAULT_SELLER_ID, batch_id: str = "",
) -> int:
    """登记一个待确认动作，返回 id。

    `batch_id`：**同一次卖家请求**里生成的多个动作共享一个值 ——
    界面据此把它们收成**一张卡**（一次确认执行整批），而不是让卖家逐张点。
    """
    _require_uri()
    return await run_sync(
        _create_action, seller_id, thread_id, tool_name, arguments, summary, detail, batch_id
    )


def _get_action(action_id: int, seller_id: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pending_action WHERE id=%s AND seller_id=%s", (action_id, seller_id))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        item = dict(zip(columns, row))
    item["arguments"] = _loads(item.get("arguments")) or {}
    return item


def _expire_stale(hours: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, decided_at=NOW(), result=%s "
            "WHERE status=%s AND created_at < (NOW() - INTERVAL %s HOUR)",
            (ACTION_EXPIRED, f"超过 {hours} 小时未处理，已自动作废", ACTION_PENDING, hours),
        )
        expired = cur.rowcount
        # 抢占后进程被杀 → 卡在 running 的动作**永远不会自己结束**，
        # 卖家看到一张点不动的卡。这里如实标为失败（不是成功），可重新发起。
        # ⚠️ 用 `decided_at`（抢占时刻）而不是 `created_at`（建卡时刻）：
        #    一小时前建卡、刚刚才点的动作，正在生图（30—60 秒）中，用建卡时间会把它误判成卡死。
        cur.execute(
            "UPDATE pending_action SET status=%s, decided_at=NOW(), result=%s "
            "WHERE status=%s AND decided_at < (NOW() - INTERVAL %s MINUTE)",
            (ACTION_FAILED, "执行过程被中断（服务重启或异常退出），这次没有完成，可重新发起",
             ACTION_RUNNING, RUNNING_STALE_MINUTES),
        )
        return expired + cur.rowcount


async def expire_stale(hours: int = PENDING_TTL_HOURS) -> int:
    """把超时未处理的待确认项标为**已过期**，返回条数。

    为什么需要它：待确认项是"等卖家点头"的，但卖家可能直接关掉页面不再回来。
    若没有过期，那条卡会永远挂着、入口上的角标也会永远亮着 ——
    **红点疲劳会让真正要紧的提示也被无视**。
    """
    _require_uri()
    return await run_sync(_expire_stale, hours)


async def get_action(action_id: int, seller_id: str) -> dict[str, Any] | None:
    """读取单个待确认动作。

    ⚠️ `seller_id` **刻意不给默认值**：这两个动作（读取 / 决定）直接决定"能不能执行破坏性操作"，
    漏传会静默按其它卖家处理 —— 让它在调用处就报错，比事后排查便宜得多（实测踩到过）。
    """
    _require_uri()
    return await run_sync(_get_action, action_id, seller_id)


def _list_actions(thread_id: str, seller_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    where = ["thread_id=%s", "seller_id=%s"]
    params: list[Any] = [thread_id, seller_id]
    if status:
        where.append("status=%s")
        params.append(status)
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_action WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT %s", params,
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(columns, row))
        item["arguments"] = _loads(item.get("arguments")) or {}
        out.append(item)
    return out


async def list_actions(
    thread_id: str, seller_id: str = DEFAULT_SELLER_ID, *, status: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """列出待确认动作（`status='pending'` 即当前待处理）。"""
    _require_uri()
    return await run_sync(_list_actions, thread_id, seller_id, status, limit)


def _decide_action(action_id: int, seller_id: str, status: str, result: str) -> bool:
    """标记动作已决。**只有 pending 能被决定** —— 重复确认会被这里挡住。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, result=%s, decided_at=NOW() "
            "WHERE id=%s AND seller_id=%s AND status=%s",
            (status, result, action_id, seller_id, ACTION_PENDING),
        )
        return cur.rowcount > 0


async def decide_action(
    action_id: int, status: str, result: str, seller_id: str
) -> bool:
    """把动作标记为 confirmed / cancelled。返回是否生效（已被决定过则 False）。

    ⚠️ `seller_id` **刻意不给默认值**，理由同 `get_action`：漏传会让"决定"落到别的卖家名下。
    """
    _require_uri()
    return await run_sync(_decide_action, action_id, seller_id, status, result)


def _claim_action(action_id: int, seller_id: str) -> bool:
    """**原子抢占**：pending → running。抢到才允许执行。

    这是防重复执行的唯一可靠办法（实测风险）：两个请求（连点两下、两个标签页）
    都先读到 `pending`，都去跑 handler，最后只有一个能写成功 —— 另一个报 409，
    **但动作已经执行了两遍**（生图 = 重复计费，删除 = 第二次报不存在）。
    所以"读状态再执行"必须换成"改状态成功才执行"。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, decided_at=NOW() "
            "WHERE id=%s AND seller_id=%s AND status=%s",
            (ACTION_RUNNING, action_id, seller_id, ACTION_PENDING),
        )
        return cur.rowcount > 0


async def claim_action(action_id: int, seller_id: str) -> bool:
    """抢占一个待确认动作（pending → running）。返回是否抢到。"""
    _require_uri()
    return await run_sync(_claim_action, action_id, seller_id)


def _finish_action(action_id: int, seller_id: str, status: str, result: str) -> bool:
    """把**已抢占**（running）的动作落到终态。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, result=%s, decided_at=NOW() "
            "WHERE id=%s AND seller_id=%s AND status=%s",
            (status, result, action_id, seller_id, ACTION_RUNNING),
        )
        return cur.rowcount > 0


async def finish_action(action_id: int, seller_id: str, status: str, result: str) -> bool:
    """执行完毕后落终态（confirmed / failed）。仅对 running 生效。"""
    _require_uri()
    return await run_sync(_finish_action, action_id, seller_id, status, result)


def _release_action(action_id: int, seller_id: str, reason: str) -> bool:
    """抢占后**没能执行**（工具已被移除等）→ 退回 pending，让卖家还能再点。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, result=%s "
            "WHERE id=%s AND seller_id=%s AND status=%s",
            (ACTION_PENDING, reason, action_id, seller_id, ACTION_RUNNING),
        )
        return cur.rowcount > 0


async def release_action(action_id: int, seller_id: str, reason: str) -> bool:
    """把抢占失败的动作退回 pending（不丢卡）。"""
    _require_uri()
    return await run_sync(_release_action, action_id, seller_id, reason)


def _list_batch(batch_id: str, seller_id: str, status: str = ACTION_PENDING) -> list[dict[str, Any]]:
    where = ["batch_id=%s", "seller_id=%s"]
    params: list[Any] = [batch_id, seller_id]
    if status:
        where.append("status=%s")
        params.append(status)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_action WHERE " + " AND ".join(where) + " ORDER BY id ASC",
            params,
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(columns, row))
        item["arguments"] = _loads(item.get("arguments")) or {}
        out.append(item)
    return out


async def list_batch(
    batch_id: str, seller_id: str = DEFAULT_SELLER_ID, *, status: str = ACTION_PENDING
) -> list[dict[str, Any]]:
    """取一个批次里的动作（按提出顺序）。`status=''` 表示不过滤。"""
    _require_uri()
    _ensure_batch_column()
    return await run_sync(_list_batch, batch_id, seller_id, status)


# ============================================================
# 批量计划进度（P1「意图/执行解耦」的可观测面）
# ============================================================


def _batch_progress(batch_id: str, seller_id: str) -> dict[str, Any]:
    """一个批次的进度：按状态计数。**单一真相** —— 不另建计划表，直接聚合 pending_action。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM pending_action "
            "WHERE batch_id=%s AND seller_id=%s GROUP BY status",
            (batch_id, seller_id),
        )
        rows = cur.fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}
    return {
        "batch_id": batch_id,
        "total": sum(counts.values()),
        "pending": counts.get(ACTION_PENDING, 0),
        "running": counts.get(ACTION_RUNNING, 0),
        "confirmed": counts.get(ACTION_CONFIRMED, 0),
        "failed": counts.get(ACTION_FAILED, 0),
        "cancelled": counts.get(ACTION_CANCELLED, 0),
        "expired": counts.get(ACTION_EXPIRED, 0),
    }


async def batch_progress(
    batch_id: str, seller_id: str = DEFAULT_SELLER_ID
) -> dict[str, Any]:
    """查一个批次的进度。"""
    _require_uri()
    _ensure_batch_column()
    return await run_sync(_batch_progress, batch_id, seller_id)


def _recent_batches(seller_id: str, limit: int) -> list[dict[str, Any]]:
    """最近若干批次各自的进度（按最新动作倒序）。

    为什么存在：模型要汇报「刚才那批做得怎么样」时，**不该要求它记住 batch_id**
    （跨轮它记不住）。这里直接给「最近 N 批」的进度，模型按时间顺序挑最新的说。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT batch_id, status, COUNT(*) AS cnt, MAX(id) AS m "
            "FROM pending_action WHERE seller_id=%s AND batch_id IS NOT NULL "
            "AND batch_id <> '' GROUP BY batch_id, status",
            (seller_id,),
        )
        rows = cur.fetchall()
    by_batch: dict[str, dict[str, Any]] = {}
    for batch_id, status, cnt, m in rows:
        b = by_batch.setdefault(batch_id, {
            "batch_id": batch_id, "total": 0, "pending": 0, "running": 0,
            "confirmed": 0, "failed": 0, "cancelled": 0, "expired": 0, "latest_id": 0,
        })
        b["total"] += int(cnt)
        key = str(status)
        if key in b:
            b[key] += int(cnt)
        b["latest_id"] = max(b["latest_id"], int(m or 0))
    batches = sorted(by_batch.values(), key=lambda b: b["latest_id"], reverse=True)
    for b in batches:
        b.pop("latest_id", None)
    return batches[:limit]


async def recent_batches(
    seller_id: str = DEFAULT_SELLER_ID, *, limit: int = 5
) -> list[dict[str, Any]]:
    """最近若干批次进度。"""
    _require_uri()
    _ensure_batch_column()
    return await run_sync(_recent_batches, seller_id, limit)


def _reset_failed(batch_id: str, seller_id: str) -> int:
    """把一批里**失败**的步骤重置回待确认（pending），返回重置条数。

    为什么不直接重试：失败项里可能有计费工具（生图），重试会**再花一次钱** ——
    按「计费需点头」红线，重置后仍需卖家再点一次确认，走既有 confirm_batch 链路。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_action SET status=%s, result=NULL, decided_at=NULL "
            "WHERE batch_id=%s AND seller_id=%s AND status=%s",
            (ACTION_PENDING, batch_id, seller_id, ACTION_FAILED),
        )
        return cur.rowcount


async def reset_failed(batch_id: str, seller_id: str = DEFAULT_SELLER_ID) -> int:
    """重置一批里的失败步骤（failed → pending）。"""
    _require_uri()
    _ensure_batch_column()
    return await run_sync(_reset_failed, batch_id, seller_id)
