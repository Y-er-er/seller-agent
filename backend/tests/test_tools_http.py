"""工具层 HTTP 路径测试（本地桩服务器，零外部依赖）。

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_tools_http.py

覆盖：
    T1 配置 base_url → 走 HTTP，字段正确映射
    T2 HTTP 404 → 返回 None（不抛异常）
    T3 枚举值非法 → 回落默认值（不抛异常）
    T4 连接失败（端口无服务）→ 返回 None
    T5 未配置 base_url → 降级本地 mock
    T6 物流 / 画像的 HTTP 映射
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

from app.config import settings  # noqa: E402
from app.models import LogisticsStatus  # noqa: E402
from app.tools import (  # noqa: E402
    aclose,
    fetch_logistics,
    fetch_order,
    fetch_profile,
    seed_order,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


class _Handler(BaseHTTPRequestHandler):
    """桩服务：按路径返回固定 JSON。"""

    def do_GET(self) -> None:  # noqa: N802 (stdlib 命名约定)
        if self.path == "/orders/OK":
            self._json(200, {"order_id": "OK", "item_category": "日用百货",
                             "pay_amount": 39.0, "logistics_status": "not_signed"})
        elif self.path == "/orders/BAD":
            # logistics_status 非法 → 应回落默认值而不是抛异常
            self._json(200, {"order_id": "BAD", "item_category": "x",
                             "pay_amount": "not-a-number", "logistics_status": "weird-status"})
        elif self.path == "/logistics/OK":
            self._json(200, {"order_id": "OK", "logistics_status": "signed",
                             "traces": ["已揽收", "已签收"]})
        elif self.path == "/profiles/OK":
            self._json(200, {"buyer_id": "B001", "buyer_credit_score": 700,
                             "buyer_refund_rate_90d": 0.05, "refund_count_90d": 1})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音桩服务日志
        return


async def run_checks(base_url: str, dead_url: str) -> None:
    # 保存原配置，结束后恢复
    original = (
        settings.order_api_base,
        settings.logistics_api_base,
        settings.aftersale_api_base,
    )

    try:
        # ---------------- T1 正常 HTTP 路径 ----------------
        settings.order_api_base = base_url
        order = await fetch_order("OK")
        ok = order is not None and order.pay_amount == 39.0 and order.logistics_status == LogisticsStatus.NOT_SIGNED
        check("T1 HTTP 取订单字段映射", ok, f"实得 {order}")

        # ---------------- T2 404 → None ----------------
        missing = await fetch_order("MISSING")
        check("T2 404 返回 None", missing is None, f"实得 {missing}")

        # ---------------- T3 非法值 → 回落默认 ----------------
        bad = await fetch_order("BAD")
        ok = bad is not None and bad.logistics_status == LogisticsStatus.IN_TRANSIT and bad.pay_amount == 0.0
        check("T3 非法枚举/数值回落默认", ok, f"实得 {bad}")

        # ---------------- T6 物流与画像 HTTP 映射 ----------------
        settings.logistics_api_base = base_url
        settings.aftersale_api_base = base_url
        lg = await fetch_logistics("OK")
        check("T6a HTTP 取物流", lg is not None and lg.logistics_status == LogisticsStatus.SIGNED and len(lg.traces) == 2, f"实得 {lg}")
        pf = await fetch_profile("OK")
        check("T6b HTTP 取画像", pf is not None and pf.buyer_credit_score == 700 and pf.refund_count_90d == 1, f"实得 {pf}")

        # ---------------- T4 连接失败 → None ----------------
        settings.order_api_base = dead_url
        dead = await fetch_order("OK")
        check("T4 连接失败返回 None", dead is None, f"实得 {dead}")

        # ---------------- T5 未配置 → 降级 mock ----------------
        settings.order_api_base = ""
        settings.logistics_api_base = ""
        settings.aftersale_api_base = ""
        seed_order("MOCK-1", pay_amount=12.5, logistics_status=LogisticsStatus.SIGNED)
        mocked = await fetch_order("MOCK-1")
        check("T5 未配置时降级 mock", mocked is not None and mocked.pay_amount == 12.5, f"实得 {mocked}")
        check("T5 未配置且无 mock 数据 → None", await fetch_order("NO-SUCH") is None)
    finally:
        settings.order_api_base, settings.logistics_api_base, settings.aftersale_api_base = original
        await aclose()


def main() -> int:
    # 桩服务绑定随机空闲端口
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # 一个确定没有服务的端口，用于验证连接失败路径
    dead_url = f"http://127.0.0.1:{port + 1}"

    try:
        asyncio.run(run_checks(base_url, dead_url))
    finally:
        server.shutdown()
        server.server_close()

    print("-" * 72)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
