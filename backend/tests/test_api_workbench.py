"""M4 复核工作台 + M5 审计查询：接入层测试。

与前几个测试的分工：
- 本文件测**端点契约**（状态码、返回结构、过滤与分页），端点函数以 `await` 直接调用；
- 端点内部经 `app.main.get_graph()` 取图，本测试用一个**临时检查点的图**替换它，
  避免污染开发用的 `data/checkpoints.sqlite`；
- 审计查询部分**真实打 MySQL**（M5 的数据源就是它），写入的测试行在结束时删除。

前置：`backend/.env` 配置了 `MYSQL_URI`。未配置时打印 SKIP 并以 0 退出。

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_api_workbench.py

覆盖：
    W1  提交大额质量单 → status=pending_review
    W2  待复核队列包含该单，且带中断载荷与命中明细
    W3  小额自动通过单 → 不出现在待复核队列
    W4  查单状态：待复核单的 pending_nodes 指向 human_review
    W5  对已完成单提交复核 → 409
    W6  对不存在的单提交复核 → 404
    W7  查不存在的单 → 404
    W8  复核恢复后 → 从待复核队列消失（status=completed）
    W9  审计查询（按订单号过滤）字段完整
    W10 审计查询（按结论过滤）
    W11 单受理号审计明细
    W12 审计明细查不到 → 404
    W13 分页：limit/offset 与 total 一致
    W14 改判样本已落库
    W15~W19 统计：总数 / 结论分布 / 规则命中排序 / 一致率 / 影子模式无落地 / 纯日期含当天
    W20 时间参数非法与倒置 → 400
    W21 真实 HTTP 冒烟（路由注册与序列化）
    W22 **幂等保护**：重复提交不重复判定 / 不重复留痕 / 命中不累加（涉及资金安全）
    W23 健康检查：依赖状态与计费闸门
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException  # noqa: E402

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.db.connection import connect  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402

# 订单号前缀统一用于测试数据清理
ORDER_HUMAN = "T-API-HUMAN"    # 大额 + 质量 → 转人工，复核"维持"，用于复核台用例
ORDER_HUMAN2 = "T-API-HUMAN2"  # 同上，但复核"改判"，为一致率提供分母
ORDER_PASS = "T-API-PASS"      # 小额未签收 → 自动通过，用于"不应出现在队列"的对照
ORDER_MISSING = "T-API-NOPE"   # 从不提交，用于 404 用例
ORDER_IDEM = "T-API-IDEM"             # 幂等用例：自动通过路径
ORDER_IDEM_HUMAN = "T-API-IDEM-HUMAN"  # 幂等用例：挂起路径

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


def cleanup_db() -> int:
    """删除本测试可能写入的审计行（按订单号前缀，覆盖多次运行的残留）。"""
    with connect() as conn, conn.cursor() as cur:
        return cur.execute("DELETE FROM decision_audit WHERE order_id LIKE 'T-API-%'")


def seed_human(order_id: str) -> None:
    """构造"转人工"所需事实：大额 + 已签收 + 质量原因。"""
    seed_order(order_id, pay_amount=1800.0, logistics_status=LogisticsStatus.SIGNED)
    seed_logistics(order_id, logistics_status=LogisticsStatus.SIGNED)
    seed_profile(order_id, buyer_credit_score=700, buyer_refund_rate_90d=0.05, refund_count_90d=0)


def seed_pass(order_id: str) -> None:
    """构造"自动通过"所需事实：小额 + 未签收 + 未收到货。"""
    seed_order(order_id, pay_amount=39.0, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_logistics(order_id, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_profile(order_id, buyer_credit_score=600, buyer_refund_rate_90d=0.05, refund_count_90d=1)


def make_request(order_id: str, amount: float, reason: RefundReason) -> RefundRequest:
    return RefundRequest(
        order_id=order_id, pay_amount=amount, claim_amount=amount, refund_reason_code=reason
    )


async def expect_http_error(coro, expected_code: int, name: str) -> None:
    """断言某个协程抛出指定状态码的 HTTPException。"""
    try:
        await coro
    except HTTPException as exc:
        check(name, exc.status_code == expected_code, f"实得 {exc.status_code}")
    else:
        check(name, False, "未抛出 HTTPException")


async def run_checks(graph) -> None:
    import app.main as api

    # 统计断言必须**精确隔离本次测试写入的数据**。
    # 若用"今天"作窗口，库中任何其他今日数据（例如手工造的演示数据）都会污染
    # `agreement_rate` 这类精确指标 —— 本测试就因此失败过一次。
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------------- W1 提交转人工单 ----------------
    seed_human(ORDER_HUMAN)
    submitted = await api.submit_request(
        make_request(ORDER_HUMAN, 1800.0, RefundReason.QUALITY)
    )
    check("W1 大额质量单已挂起", submitted.get("status") == "pending_review",
          f"实得 {submitted.get('status')}")
    check("W1 结论为 to_human", submitted.get("conclusion") == "to_human",
          f"实得 {submitted.get('conclusion')}")

    # ---------------- W2 待复核队列包含该单 ----------------
    queue = await api.list_reviews_pending(limit=50)
    human_items = [x for x in queue["items"] if x.get("order_id") == ORDER_HUMAN]
    check("W2 待复核队列包含该单", len(human_items) == 1, f"队列共 {queue['total']} 条")
    if human_items:
        item = human_items[0]
        check("W2 队列项带中断载荷", bool(item.get("interrupt")), f"interrupt={item.get('interrupt')}")
        check("W2 队列项带命中明细", len(item.get("hit_details") or []) > 0,
              f"hit_details={item.get('hit_details')}")
        check("W2 队列项标记待复核", item.get("status") == "pending_review",
              f"实得 {item.get('status')}")

    # ---------------- W3 自动通过单不进入队列 ----------------
    seed_pass(ORDER_PASS)
    passed = await api.submit_request(
        make_request(ORDER_PASS, 39.0, RefundReason.NOT_RECEIVED)
    )
    check("W3 小额单自动通过", passed.get("status") == "completed", f"实得 {passed.get('status')}")
    queue2 = await api.list_reviews_pending(limit=50)
    check("W3 待复核队列不含自动通过单",
          all(x.get("order_id") != ORDER_PASS for x in queue2["items"]),
          f"队列={[x.get('order_id') for x in queue2['items']]}")

    # ---------------- W4 单查状态 ----------------
    state = await api.get_request_state(ORDER_HUMAN)
    check("W4 待复核单 pending_nodes 指向 human_review",
          "human_review" in list(state.get("pending_nodes") or []),
          f"实得 {state.get('pending_nodes')}")

    # ---------------- W5/W6/W7 错误语义 ----------------
    await expect_http_error(
        api.review(ORDER_PASS, api.ReviewPayload(decision="maintain", reviewer="tester")),
        409, "W5 对已完成单复核 → 409",
    )
    await expect_http_error(
        api.review(ORDER_MISSING, api.ReviewPayload(decision="maintain", reviewer="tester")),
        404, "W6 对不存在单复核 → 404",
    )
    await expect_http_error(api.get_request_state(ORDER_MISSING), 404, "W7 查不存在单 → 404")

    # ---------------- W8 复核恢复后出队 ----------------
    resumed = await api.review(
        ORDER_HUMAN, api.ReviewPayload(decision="maintain", reviewer="qa-zhang")
    )
    check("W8 复核后已完成", resumed.get("status") == "completed", f"实得 {resumed.get('status')}")
    check("W8 复核决定已记录", resumed.get("review_decision") == "maintain",
          f"实得 {resumed.get('review_decision')}")
    queue3 = await api.list_reviews_pending(limit=50)
    check("W8 复核后已出队",
          all(x.get("order_id") != ORDER_HUMAN for x in queue3["items"]),
          f"队列={[x.get('order_id') for x in queue3['items']]}")

    # ---------------- W9 审计查询（按订单号） ----------------
    by_order = await api.list_audit_records(
        conclusion=None, order_id=ORDER_HUMAN, limit=50, offset=0
    )
    check("W9 按订单号查到审计行", by_order["total"] == 1, f"实得 total={by_order['total']}")
    if by_order["items"]:
        rec = by_order["items"][0]
        check("W9 结论正确", rec["conclusion"] == "to_human", f"实得 {rec['conclusion']}")
        # 本用例未提交举证图，故 R6（应举证未举证）也会命中；
        # 断言用"包含"而非"相等"——R6 结论优先级低于 to_human，不影响最终结论（全量求值）。
        check("W9 命中规则已还原为列表且含 R1/R7",
              {"R1", "R7"} <= set(rec["hit_rules"]), f"实得 {rec['hit_rules']}")
        check("W9 命中明细为 JSON 对象", isinstance(rec["field_values"], list),
              f"实得 {type(rec['field_values']).__name__}")
        check("W9 复核结论已落库", rec["review_decision"] == "maintain",
              f"实得 {rec['review_decision']}")
        # to_human 路径不经过 dispose，没有资金动作 → action 为空、executed 为假。
        # （shadow 字段描述的是"该动作"是否处于影子模式，此处无动作故为 0，属预期）
        check("W9 转人工路径无资金动作",
              rec["action"] is None and rec["executed"] is False,
              f"action={rec['action']} executed={rec['executed']}")
        check("W9 时间字段可序列化", isinstance(rec["created_at"], str), f"实得 {rec['created_at']}")

    # ---------------- W9b 自动通过单：有处置动作，且影子模式下未落地 ----------------
    by_pass = await api.list_audit_records(
        conclusion=None, order_id=ORDER_PASS, limit=50, offset=0
    )
    check("W9b 自动通过单有审计行", by_pass["total"] == 1, f"实得 total={by_pass['total']}")
    if by_pass["items"]:
        p = by_pass["items"][0]
        check("W9b 记录了处置动作", bool(p["action"]), f"实得 action={p['action']}")
        check("W9b 影子模式下未落地执行", p["executed"] is False, f"实得 executed={p['executed']}")
        check("W9b 影子模式标记为真", p["shadow"] is True, f"实得 shadow={p['shadow']}")

    # ---------------- W10 审计查询（按结论过滤） ----------------
    by_conclusion = await api.list_audit_records(
        conclusion="auto_pass", order_id=ORDER_PASS, limit=50, offset=0
    )
    check("W10 按结论过滤命中自动通过单", by_conclusion["total"] == 1,
          f"实得 total={by_conclusion['total']}")
    human_only = await api.list_audit_records(
        conclusion="auto_pass", order_id=ORDER_HUMAN, limit=50, offset=0
    )
    check("W10 结论不匹配时过滤为空", human_only["total"] == 0, f"实得 total={human_only['total']}")

    # ---------------- W11/W12 单受理号明细 ----------------
    detail = await api.get_audit_records(ORDER_HUMAN)
    check("W11 单受理号明细可查", detail["total"] == 1, f"实得 total={detail['total']}")
    await expect_http_error(api.get_audit_records(ORDER_MISSING), 404, "W12 审计明细查不到 → 404")

    # ---------------- W13 分页一致性 ----------------
    page1 = await api.list_audit_records(conclusion=None, order_id=None, limit=1, offset=0)
    page2 = await api.list_audit_records(conclusion=None, order_id=None, limit=1, offset=1)
    check("W13 分页 total 稳定", page1["total"] == page2["total"],
          f"{page1['total']} vs {page2['total']}")
    check("W13 两页返回不同记录",
          page1["items"][0]["id"] != page2["items"][0]["id"]
          if page1["items"] and page2["items"] else False,
          "两页返回了同一条记录")

    # ---------------- W14 再补一笔"被改判"的样本，让一致率具备分母 ----------------
    seed_human(ORDER_HUMAN2)
    await api.submit_request(make_request(ORDER_HUMAN2, 1800.0, RefundReason.QUALITY))
    await api.review(ORDER_HUMAN2, api.ReviewPayload(decision="override", reviewer="qa-li"))
    detail2 = await api.get_audit_records(ORDER_HUMAN2)
    check("W14 改判结论已落库",
          detail2["items"] and detail2["items"][0]["review_decision"] == "override",
          f"实得 {detail2['items'][0]['review_decision'] if detail2['items'] else None}")

    # ---------------- W15~W19 统计（用测试开始时刻隔离，排除库中其他数据） ----------------
    today = date.today().isoformat()
    stats = await api.audit_stats(since=started_at, until=today)

    check("W15 统计仅含本次测试数据", stats["total"] == 3, f"实得 total={stats['total']}")
    dist = stats["conclusion_distribution"]
    check("W15 结论分布精确匹配（1 自动通过 / 2 转人工）",
          dist.get("auto_pass") == 1 and dist.get("to_human") == 2,
          f"实得 {dist}")
    check("W15 结论分布补齐全部枚举", set(dist) == {"auto_pass", "auto_reject", "request_evidence", "to_human"},
          f"实得 {sorted(dist)}")

    rule_ids = {item["rule_id"] for item in stats["rule_hits"]}
    check("W16 规则命中含 R1/R2/R7", {"R1", "R2", "R7"} <= rule_ids, f"实得 {sorted(rule_ids)}")
    check("W16 规则命中按次数倒序",
          all(stats["rule_hits"][i]["count"] >= stats["rule_hits"][i + 1]["count"]
              for i in range(len(stats["rule_hits"]) - 1)),
          f"实得 {stats['rule_hits']}")

    rd = stats["review_distribution"]
    check("W17 复核分布精确匹配（1 维持 / 1 改判）",
          rd["maintain"] == 1 and rd["override"] == 1, f"实得 {rd}")
    # 一致率 = 维持 / (维持 + 改判)。本测试恰好各 1 笔 → 0.5
    check("W17 一致率为维持占比", stats["agreement_rate"] == 0.5,
          f"实得 {stats['agreement_rate']}")
    check("W17 复核参与率为 1.0", stats["review_coverage"] == 1.0,
          f"实得 {stats['review_coverage']}")

    # 影子模式下资金动作应全部未落地
    check("W18 影子模式下无资金动作落地", stats["execution_stats"]["executed"] == 0,
          f"实得 {stats['execution_stats']}")

    # W19 时间参数：纯日期应"含当天"（若不补齐时分秒，当天数据会被整段排除）
    check("W19 纯日期 until 含当天", stats["total"] == 3, f"实得 total={stats['total']}")
    check("W19 时间回显规范化", stats["time_range"]["until"] == f"{today} 23:59:59",
          f"实得 {stats['time_range']['until']}")

    # W20 时间参数非法与倒置
    await expect_http_error(
        api.audit_stats(since="2026/10/13", until=None), 400, "W20 日期格式非法 → 400"
    )
    await expect_http_error(
        api.audit_stats(since="2026-10-14", until="2026-10-13"), 400, "W20 since 晚于 until → 400"
    )

    # ---------------- W22 幂等保护（**涉及资金安全**） ----------------
    # 缺陷背景：thread_id = 受理号，重复提交会从检查点续跑，而 hits 是 operator.add 字段，
    # 会累积成 R2 → R2,R2 → R2,R2,R2，并重复写审计、重复触发处置（真实场景即重复退款）。
    seed_pass(ORDER_IDEM)
    first = await api.submit_request(make_request(ORDER_IDEM, 39.0, RefundReason.NOT_RECEIVED))
    check("W22 首次提交非幂等重放", first.get("idempotent_replay") is False,
          f"实得 {first.get('idempotent_replay')}")
    check("W22 首次命中为单条 R2", first.get("hit_rules") == ["R2"], f"实得 {first.get('hit_rules')}")

    second = await api.submit_request(make_request(ORDER_IDEM, 39.0, RefundReason.NOT_RECEIVED))
    check("W22 重复提交被识别为幂等重放", second.get("idempotent_replay") is True,
          f"实得 {second.get('idempotent_replay')}")
    check("W22 重复提交后命中规则未累加", second.get("hit_rules") == ["R2"],
          f"实得 {second.get('hit_rules')}")
    check("W22 重复提交结论与首次一致",
          second.get("conclusion") == first.get("conclusion"),
          f"{second.get('conclusion')} vs {first.get('conclusion')}")

    idem_rows = await api.get_audit_records(ORDER_IDEM)
    check("W22 重复提交未重复留痕", idem_rows["total"] == 1, f"实得 {idem_rows['total']} 行")

    # 挂起中的订单重复提交：应返回既有挂起状态，不重复判定
    seed_human(ORDER_IDEM_HUMAN)
    await api.submit_request(make_request(ORDER_IDEM_HUMAN, 1800.0, RefundReason.QUALITY))
    replay = await api.submit_request(make_request(ORDER_IDEM_HUMAN, 1800.0, RefundReason.QUALITY))
    check("W22 挂起单重复提交返回既有挂起状态",
          replay.get("idempotent_replay") is True and replay.get("status") == "pending_review",
          f"实得 {replay.get('idempotent_replay')}/{replay.get('status')}")

    # ---------------- W23 健康检查 ----------------
    h = await api.health()
    check("W23 健康检查 ok", h["status"] == "ok", f"实得 {h['status']}")
    check("W23 审计库状态已暴露", h["audit_db"]["reachable"] is True, f"实得 {h['audit_db']}")
    check("W23 模型开关如实反映配置", h["llm"]["enabled"] == settings.llm_enabled, f"实得 {h['llm']}")
    check("W23 模型可用性由「开关 + 密钥」共同决定",
          h["llm"]["text_available"] == (settings.llm_enabled and bool(settings.dashscope_api_key)),
          f"实得 {h['llm']}")
    check("W23 影子模式已暴露", h["shadow_mode"] is True, f"实得 {h['shadow_mode']}")

    # ---------------- W24 配置自检（防"配置合法但语义失效"）----------------
    from app.config_validation import validate_settings

    original_small = settings.small_amount
    original_max_rate = settings.max_refund_rate
    try:
        # 冲突 1：小额线 >= 金额闸门 → R1 会先命中，R2/R4 永不生效
        settings.small_amount = settings.auto_amount_cap
        warnings = validate_settings()
        check("W24 检出「小额线不小于金额闸门」",
              any("SMALL_AMOUNT" in w for w in warnings), f"实得 {warnings}")

        # 冲突 2：R4 上限 > R5 门槛 → 退款率出现无人区
        settings.small_amount = original_small
        settings.max_refund_rate = settings.high_refund_rate + 0.10
        warnings = validate_settings()
        check("W24 检出「退款率无人区」",
              any("MAX_REFUND_RATE" in w for w in warnings), f"实得 {warnings}")
    finally:
        settings.small_amount = original_small
        settings.max_refund_rate = original_max_rate

    # 复原后（当前定稿配置）不应再报阈值类冲突
    warnings = validate_settings()
    check("W24 定稿配置无阈值语义冲突",
          not any("SMALL_AMOUNT" in w or "MAX_REFUND_RATE" in w for w in warnings),
          f"实得 {warnings}")


async def amain() -> int:
    if not settings.mysql_uri:
        print("SKIP：未配置 MYSQL_URI，跳过 M4/M5 接入层测试")
        return 0

    # 先探测审计库可用性：不可达则整体 SKIP。
    # 理由：本测试依赖真实数据源做查询断言，中途崩溃会掩盖"环境不可用"与"实现有缺陷"的区别。
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP：审计库不可用（{type(exc).__name__}: {exc}），跳过 M4/M5 接入层测试")
        return 0

    original_enabled = settings.audit_db_enabled
    settings.audit_db_enabled = True  # M5 查询需要真实数据

    ckpt_path = PROJECT_ROOT / "data" / "test_api_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)

    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    import app.main as api

    original_get_graph = api.get_graph

    async def _fake_get_graph():
        """把端点指向本测试的临时图，避免写入开发用检查点库。"""
        return graph

    api.get_graph = _fake_get_graph

    try:
        cleanup_db()  # 清掉上次运行的残留，保证计数断言可靠
        await run_checks(graph)
    finally:
        api.get_graph = original_get_graph
        settings.audit_db_enabled = original_enabled
        try:
            removed = cleanup_db()
            print(f"        已清理测试数据：{removed} 行")
        except Exception as exc:  # noqa: BLE001
            print(f"        ⚠️ 清理失败，请手工删除 order_id LIKE 'T-API-%'：{exc}")
        await checkpointer.conn.close()
        ckpt_path.unlink(missing_ok=True)

    print("-" * 72)
    return 1 if failures else 0


def http_smoke() -> None:
    """真实 HTTP 冒烟：验证路由注册与响应序列化。

    为什么必须单独做一次 **HTTP** 调用：
    前面的断言都是直接 `await` 端点函数，**绕过了 FastAPI 的路由匹配**。
    而 `/api/audits/stats` 与 `/api/audits/{request_id}` 存在路径冲突——
    若声明顺序写反，`stats` 会被当作受理号，直接调函数是**测不出来的**。
    本函数在 `asyncio.run` 之外同步执行，避免与事件循环冲突。
    """
    from fastapi.testclient import TestClient

    import app.main as api

    with TestClient(api.app) as client:
        resp = client.get("/api/audits/stats")
        # 允许 200（正常）或 503（库不可用）；唯独不能是"被当作受理号"的 404
        captured = resp.status_code == 404 and "stats" in str(resp.json().get("detail", ""))
        check("W21 路由顺序正确（stats 未被 {request_id} 捕获）",
              not captured, f"status={resp.status_code} body={resp.text[:120]}")

        if resp.status_code == 200:
            body = resp.json()
            check("W21 统计响应结构完整",
                  {"total", "conclusion_distribution", "agreement_rate", "rule_hits"} <= set(body),
                  f"实得键={sorted(body)}")
            check("W21 统计可 JSON 序列化", isinstance(body["rule_hits"], list), f"实得 {type(body['rule_hits'])}")

        # 非法时间参数应返回 400（走真实请求，验证参数校验接入生效）
        bad = client.get("/api/audits/stats", params={"since": "not-a-date"})
        check("W21 非法时间参数 → 400", bad.status_code == 400, f"实得 {bad.status_code}")


if __name__ == "__main__":
    code = asyncio.run(amain())
    if code == 0:
        # 仅在库相关断言全过时再做 HTTP 冒烟，避免环境问题掩盖真实缺陷
        http_smoke()

    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)
    print("结果：全部通过（含 HTTP 冒烟）")
    raise SystemExit(0)
