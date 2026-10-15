"""M2 处置执行层测试（**资金动作**，用本地桩服务器验证真实调用路径）。

重点覆盖"绝不允许发生"的情形：
- 未获授权 / 未配置接口时，**必须拒绝执行并升级人工**，不得伪造成功；
- 调用失败（网络 / 非 2xx / 业务拒绝）同样升级人工，不得静默结束；
- 真实调用必须携带**幂等键**，防网络重试导致重复退款；
- 影子模式下**不得产生任何外部调用**。

运行（无需 MySQL；桩服务器为本机进程内）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_disposal.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langgraph.types import Command  # noqa: E402

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


# ============================================================
# 桩服务器：模拟售后处置接口
# ============================================================
class _StubHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    status_code = 200
    payload: dict = {"status": "ok", "disposal_id": "D-001"}

    def do_POST(self) -> None:  # noqa: N802 — http.server 的约定命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        type(self).calls.append(
            {
                "path": self.path,
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "body": body,
            }
        )
        data = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # noqa: D102 — 静音，避免污染测试输出
        pass


def reset_stub(status_code: int = 200, payload: dict | None = None) -> None:
    """重置桩服务器的响应与调用记录。"""
    _StubHandler.calls = []
    _StubHandler.status_code = status_code
    _StubHandler.payload = payload if payload is not None else {"status": "ok", "disposal_id": "D-001"}


def cfg(order_id: str) -> dict:
    return {"configurable": {"thread_id": order_id}}


def seed_pass(order_id: str, amount: float = 39.0) -> None:
    """构造"会自动通过 → 进入 dispose"的事实。"""
    seed_order(order_id, pay_amount=amount, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_logistics(order_id, logistics_status=LogisticsStatus.NOT_SIGNED)
    seed_profile(order_id, buyer_credit_score=600, buyer_refund_rate_90d=0.05, refund_count_90d=1)


def make_request(order_id: str, amount: float = 39.0) -> RefundRequest:
    return RefundRequest(
        order_id=order_id, pay_amount=amount, claim_amount=amount,
        refund_reason_code=RefundReason.NOT_RECEIVED,
    )


async def run_checks(graph, stub_port: int) -> None:
    # ============================================================
    # D1 影子模式：不得产生任何外部调用
    # ============================================================
    reset_stub()
    settings.shadow_mode = True
    settings.disposal_enabled = True  # 即便授权了，影子模式也不执行
    settings.disposal_api_base = f"http://127.0.0.1:{stub_port}"

    oid = "T-DISP-1"
    seed_pass(oid)
    r1 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act1 = r1.get("action")
    check("D1 影子模式未落地执行", act1 is not None and act1.executed is False, f"action={act1}")
    check("D1 影子模式标记为真", act1 is not None and act1.shadow is True)
    check("D1 影子模式未产生外部调用", len(_StubHandler.calls) == 0,
          f"实得 {len(_StubHandler.calls)} 次调用")
    check("D1 影子模式不挂起（照常落审计）", not r1.get("__interrupt__"), "出现了意外中断")

    # ============================================================
    # D2 未开启处置授权：拒绝执行 + 升级人工
    # ============================================================
    reset_stub()
    settings.shadow_mode = False
    settings.disposal_enabled = False

    oid = "T-DISP-2"
    seed_pass(oid)
    r2 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act2 = r2.get("action")
    check("D2 未授权时不落地执行", act2 is not None and act2.executed is False, f"action={act2}")
    check("D2 未授权时不产生外部调用", len(_StubHandler.calls) == 0,
          f"实得 {len(_StubHandler.calls)} 次调用")
    check("D2 未授权时标记需人工", act2 is not None and act2.detail.get("needs_human") is True)
    check("D2 未授权时已挂起待人工", bool(r2.get("__interrupt__")), "未挂起")
    check("D2 挂起原因为处置未落地",
          (r2.get("__interrupt__") or [{}])[0].value.get("reason") == "disposal_failed"
          if r2.get("__interrupt__") else False,
          f"实得 {r2.get('__interrupt__')}")

    # 恢复到完成态，避免影响后续用例
    await graph.ainvoke(Command(resume={"decision": "maintain", "reviewer": "tester"}), config=cfg(oid))

    # ============================================================
    # D3 已授权但未配置接口：同样拒绝执行
    # ============================================================
    reset_stub()
    settings.disposal_enabled = True
    settings.disposal_api_base = ""

    oid = "T-DISP-3"
    seed_pass(oid)
    r3 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act3 = r3.get("action")
    check("D3 未配置接口时拒绝执行", act3 is not None and act3.executed is False, f"action={act3}")
    check("D3 未配置接口时无外部调用", len(_StubHandler.calls) == 0)
    check("D3 错误信息已记录", "未配置" in str(act3.detail.get("error", "")),
          f"实得 {act3.detail.get('error')}")
    await graph.ainvoke(Command(resume={"decision": "maintain", "reviewer": "tester"}), config=cfg(oid))

    # ============================================================
    # D4 真实执行成功：落地 + 幂等键传递正确
    # ============================================================
    reset_stub(200, {"status": "ok", "disposal_id": "D-REAL-9"})
    settings.disposal_enabled = True
    settings.disposal_api_base = f"http://127.0.0.1:{stub_port}"

    oid = "T-DISP-4"
    seed_pass(oid)
    r4 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act4 = r4.get("action")
    check("D4 真实执行已落地", act4 is not None and act4.executed is True, f"action={act4}")
    check("D4 影子标记为假", act4 is not None and act4.shadow is False)
    check("D4 未挂起（正常结束）", not r4.get("__interrupt__"), "出现了意外中断")
    check("D4 已发生一次外部调用", len(_StubHandler.calls) == 1,
          f"实得 {len(_StubHandler.calls)} 次")
    if _StubHandler.calls:
        call = _StubHandler.calls[0]
        check("D4 请求路径正确", call["path"] == "/disposals", f"实得 {call['path']}")
        check("D4 幂等键=受理号", call["idempotency_key"] == oid,
              f"实得 {call['idempotency_key']}")
        check("D4 请求体含动作与订单", call["body"].get("action") == "approve_refund"
              and call["body"].get("order_id") == oid, f"实得 {call['body']}")
    check("D4 处置单号已留痕", act4 is not None and act4.detail.get("disposal_id") == "D-REAL-9",
          f"实得 {act4.detail.get('disposal_id') if act4 else None}")

    # ============================================================
    # D5 业务拒绝（2xx 但 status=rejected）：必须升级人工
    # ============================================================
    reset_stub(200, {"status": "rejected", "reason": "余额不足"})
    oid = "T-DISP-5"
    seed_pass(oid)
    r5 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act5 = r5.get("action")
    check("D5 业务拒绝不落地", act5 is not None and act5.executed is False, f"action={act5}")
    check("D5 业务拒绝升级人工", act5 is not None and act5.detail.get("needs_human") is True)
    check("D5 业务拒绝已挂起", bool(r5.get("__interrupt__")), "未挂起")
    # 人工跟进后链路才会继续走到 audit（挂起时 audit 尚未执行，这是预期行为）
    r5_resumed = await graph.ainvoke(
        Command(resume={"decision": "maintain", "reviewer": "tester"}), config=cfg(oid)
    )

    # ============================================================
    # D6 HTTP 500：必须升级人工
    # ============================================================
    reset_stub(500, {"status": "error"})
    oid = "T-DISP-6"
    seed_pass(oid)
    r6 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act6 = r6.get("action")
    check("D6 HTTP 500 不落地", act6 is not None and act6.executed is False, f"action={act6}")
    check("D6 HTTP 500 升级人工", act6 is not None and act6.detail.get("needs_human") is True)
    await graph.ainvoke(Command(resume={"decision": "maintain", "reviewer": "tester"}), config=cfg(oid))

    # ============================================================
    # D7 连接失败（端口不可达）：必须升级人工
    # ============================================================
    reset_stub(200)
    settings.disposal_api_base = "http://127.0.0.1:1"  # 不可达端口
    oid = "T-DISP-7"
    seed_pass(oid)
    r7 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act7 = r7.get("action")
    check("D7 连接失败不落地", act7 is not None and act7.executed is False, f"action={act7}")
    check("D7 连接失败升级人工", act7 is not None and act7.detail.get("needs_human") is True)
    check("D7 连接失败已记录错误", bool(act7.detail.get("error")), "错误信息为空")
    await graph.ainvoke(Command(resume={"decision": "maintain", "reviewer": "tester"}), config=cfg(oid))

    # ============================================================
    # D8 失败路径的审计留痕（人工跟进后链路完整走完）
    # ============================================================
    stages = [getattr(e, "stage", None) for e in r5_resumed.get("audit_events", [])]
    check("D8 失败路径在人工跟进后完整落审计",
          "dispose" in stages and "human_review" in stages and "audit" in stages,
          f"stages={sorted(set(stages))}")

    # ============================================================
    # D9 白名单：to_human 路径不进入 dispose（不产生任何资金动作）
    # ============================================================
    reset_stub(200)
    settings.disposal_api_base = f"http://127.0.0.1:{stub_port}"
    oid = "T-DISP-9"
    seed_order(oid, pay_amount=1800.0, logistics_status=LogisticsStatus.SIGNED)
    seed_logistics(oid, logistics_status=LogisticsStatus.SIGNED)
    seed_profile(oid, buyer_credit_score=700, buyer_refund_rate_90d=0.05, refund_count_90d=0)
    r9 = await graph.ainvoke(
        {"request": RefundRequest(order_id=oid, pay_amount=1800.0, claim_amount=1800.0,
                                  refund_reason_code=RefundReason.QUALITY)},
        config=cfg(oid),
    )
    check("D9 转人工路径无资金动作", r9.get("action") is None, f"action={r9.get('action')}")
    check("D9 转人工路径无外部调用", len(_StubHandler.calls) == 0,
          f"实得 {len(_StubHandler.calls)} 次")
    check("D9 转人工挂起原因标为规则判定",
          (r9.get("__interrupt__") or [{}])[0].value.get("reason") == "rule_to_human"
          if r9.get("__interrupt__") else False,
          f"实得 {(r9.get('__interrupt__') or [{}])[0].value.get('reason') if r9.get('__interrupt__') else None}")

    # ============================================================
    # D10 人工重试：首次失败 → 接口恢复后重试 → 成功落地
    # ============================================================
    reset_stub(200, {"status": "ok", "disposal_id": "D-RETRY-1"})
    settings.disposal_api_base = "http://127.0.0.1:1"  # 先制造失败
    oid = "T-DISP-10"
    seed_pass(oid)
    r10 = await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    act10 = r10.get("action")
    check("D10 首次处置失败并挂起",
          act10 is not None and act10.executed is False and bool(r10.get("__interrupt__")),
          f"action={act10}")

    # 接口恢复，人工选择重试
    settings.disposal_api_base = f"http://127.0.0.1:{stub_port}"
    r10b = await graph.ainvoke(
        Command(resume={"decision": "retry", "reviewer": "qa-li"}), config=cfg(oid)
    )
    act10b = r10b.get("action")
    check("D10 重试后落地成功", act10b is not None and act10b.executed is True, f"action={act10b}")
    check("D10 重试后不再挂起", not r10b.get("__interrupt__"), "仍处于挂起态")
    check("D10 审计记录重试次数", act10b is not None and act10b.detail.get("retry_attempt") == 1,
          f"实得 {act10b.detail.get('retry_attempt') if act10b else None}")
    check("D10 复核结论记为 retry", r10b.get("review_decision") == "retry",
          f"实得 {r10b.get('review_decision')}")

    # ============================================================
    # D11 重试仍失败：再次挂起，由人工继续决定（不自动循环）
    # ============================================================
    reset_stub()
    settings.disposal_api_base = "http://127.0.0.1:1"
    oid = "T-DISP-11"
    seed_pass(oid)
    await graph.ainvoke({"request": make_request(oid)}, config=cfg(oid))
    r11b = await graph.ainvoke(
        Command(resume={"decision": "retry", "reviewer": "qa-li"}), config=cfg(oid)
    )
    check("D11 重试仍失败会再次挂起", bool(r11b.get("__interrupt__")), "未再次挂起")
    if r11b.get("__interrupt__"):
        value = r11b["__interrupt__"][0].value
        check("D11 再次挂起时仍提示可重试", "retry" in value.get("options", []),
              f"实得 {value.get('options')}")
        check("D11 载荷记录重试次数", value.get("attempt") == 1, f"实得 {value.get('attempt')}")

    r11c = await graph.ainvoke(
        Command(resume={"decision": "maintain", "reviewer": "qa-li"}), config=cfg(oid)
    )
    check("D11 放弃重试后正常结束", not r11c.get("__interrupt__"), "仍处于挂起态")

    # ============================================================
    # D12 规则判定转人工时不提供"重试处置"（无处置动作可重试）
    # ============================================================
    if r9.get("__interrupt__"):
        value9 = r9["__interrupt__"][0].value
        check("D12 规则转人工不提供重试项", "retry" not in value9.get("options", []),
              f"实得 {value9.get('options')}")
        check("D12 规则转人工载荷含可选项", bool(value9.get("options")), "options 为空")


async def amain() -> int:
    # 本测试不使用 MySQL（审计落库关闭），聚焦处置执行链路
    original = {
        "shadow_mode": settings.shadow_mode,
        "disposal_enabled": settings.disposal_enabled,
        "disposal_api_base": settings.disposal_api_base,
        "audit_db_enabled": settings.audit_db_enabled,
    }
    settings.audit_db_enabled = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    stub_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    ckpt_path = PROJECT_ROOT / "data" / "test_disposal_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)
    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    print(f"桩处置服务：http://127.0.0.1:{stub_port}")
    print("-" * 72)
    try:
        await run_checks(graph, stub_port)
    finally:
        server.shutdown()
        settings.shadow_mode = original["shadow_mode"]
        settings.disposal_enabled = original["disposal_enabled"]
        settings.disposal_api_base = original["disposal_api_base"]
        settings.audit_db_enabled = original["audit_db_enabled"]
        await checkpointer.conn.close()
        ckpt_path.unlink(missing_ok=True)

    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
