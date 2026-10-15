"""视觉理解链路测试（**全部离线**，用桩替换真实模型调用）。

覆盖目标：
1. 闸门关闭时**零调用**（纪律 10 的计费闸门必须真的拦住）；
2. 只有可直接送模型的 http(s) 图片才调用，且**张数受上限约束**（成本护栏）；
3. 超时 / 异常 / 非 200 / 输出无法解析 → 一律降级，**不抛出**；
4. 模型输出仅作辅助信号：**判定结论与命中规则不发生任何变化**（业务红线）。

运行（无需 MySQL、无网络、零费用）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_vision_evidence.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.graph.builder import build_graph, make_checkpointer  # noqa: E402
from app.graph.nodes.evidence import evidence_node  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
from app.models import LogisticsStatus, RefundReason, RefundRequest  # noqa: E402
from app.tools import seed_logistics, seed_order, seed_profile  # noqa: E402
from langgraph.types import Command  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


# ============================================================
# 桩：替换底层视觉调用（不联网、不产生任何费用）
# ============================================================
class _VisionStub:
    """记录调用参数，并可配置返回内容 / 抛异常。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.text: str | None = json.dumps(
            {"has_quality_claim": True, "claim_type": "quality", "visible_defect": True,
             "confidence": 0.88, "summary": "外包装破损，商品表面有明显污渍"},
            ensure_ascii=False,
        )
        self.usage: dict = {"input_tokens": 1200, "output_tokens": 60, "total_tokens": 1260}
        self.raise_error: Exception | None = None

    def __call__(self, image_refs, prompt, *, timeout):  # noqa: ANN001 — 与真实签名对齐
        self.calls.append(list(image_refs))
        if self.raise_error is not None:
            raise self.raise_error
        return self.text, self.usage


stub = _VisionStub()
_real_invoke = llm_client._invoke_vision


def make_state(order_id: str, images: list[str], note: str = "") -> dict:
    return {
        "request": RefundRequest(
            order_id=order_id, pay_amount=100.0, claim_amount=100.0,
            refund_reason_code=RefundReason.QUALITY, claim_images=images, buyer_note=note,
        )
    }


def http_images(n: int) -> list[str]:
    return [f"https://example.com/evidence/{i + 1}.jpg" for i in range(n)]


# ============================================================
# A 组：闸门关闭 —— 必须零调用
# ============================================================
async def run_gate_closed() -> None:
    print("【A 组】闸门关闭（纪律 10：默认不得产生任何计费调用）")
    settings.llm_enabled = False
    settings.dashscope_api_key = "test-key-not-used"

    stub.calls.clear()
    out = await evidence_node(make_state("V-A1", []))
    check("A1 无图片 → source=none", out["evidence"].source == "none", f"实得 {out['evidence'].source}")
    check("A1 无图片 → 零调用", len(stub.calls) == 0, f"实得 {len(stub.calls)} 次")

    stub.calls.clear()
    out = await evidence_node(make_state("V-A2", http_images(2)))
    check("A2 闸门关闭 → source=llm_disabled",
          out["evidence"].source == "llm_disabled", f"实得 {out['evidence'].source}")
    check("A2 闸门关闭 → 零调用", len(stub.calls) == 0, f"实得 {len(stub.calls)} 次")
    check("A2 闸门关闭 → 不产出模型信号", out["evidence"].has_quality_claim is False)
    check("A2 闸门关闭 → 无 llm_usage", "llm_usage" not in out)

    stub.calls.clear()
    signals = await llm_client.extract_evidence_signals(http_images(1))
    check("A3 直接调用抽取函数 → 返回 None", signals is None, f"实得 {signals}")
    check("A3 直接调用抽取函数 → 零调用", len(stub.calls) == 0, f"实得 {len(stub.calls)} 次")

    # 张数上限置 0 等同于彻底关闭视觉
    settings.llm_enabled = True
    settings.vision_max_images = 0
    stub.calls.clear()
    out = await evidence_node(make_state("V-A4", http_images(2)))
    check("A4 张数上限为 0 → 视同关闭（llm_disabled）",
          out["evidence"].source == "llm_disabled", f"实得 {out['evidence'].source}")
    check("A4 张数上限为 0 → 零调用", len(stub.calls) == 0, f"实得 {len(stub.calls)} 次")
    settings.vision_max_images = 2


# ============================================================
# B 组：闸门开启 —— 调用、限流与降级
# ============================================================
async def run_gate_open() -> None:
    print()
    print("【B 组】闸门开启（配置成本护栏与降级路径）")
    settings.llm_enabled = True
    settings.dashscope_api_key = "test-key-not-used"
    settings.vision_max_images = 2
    settings.vision_include_buyer_note = False

    # B1 正常路径 + 张数截断
    stub.raise_error = None
    stub.calls.clear()
    out = await evidence_node(make_state("V-B1", http_images(3), note="商品有质量问题"))
    rep = out["evidence"]
    check("B1 正常抽取 → source=llm", rep.source == "llm", f"实得 {rep.source}")
    check("B1 抽取到质量主张信号", rep.has_quality_claim is True)
    check("B1 抽取到诉求类型", rep.claim_type == "quality", f"实得 {rep.claim_type}")
    check("B1 恰好调用 1 次", len(stub.calls) == 1, f"实得 {len(stub.calls)} 次")
    check("B1 图片张数受上限约束（3 → 2）", len(stub.calls[0]) == 2, f"实得 {len(stub.calls[0])}")
    check("B1 产出一条用量计量", len(out.get("llm_usage", [])) == 1
          and out["llm_usage"][0].get("model") == settings.llm_vision_model,
          f"实得 {out.get('llm_usage')}")

    # B2 输出无法解析为 JSON → 降级
    stub.text = "抱歉，我无法完成该请求。"
    stub.calls.clear()
    out = await evidence_node(make_state("V-B2", http_images(1)))
    check("B2 非 JSON 输出 → 降级为 llm_failed",
          out["evidence"].source == "llm_failed", f"实得 {out['evidence'].source}")
    check("B2 降级后不产出信号", out["evidence"].has_quality_claim is False)
    check("B2 降级后不写用量", "llm_usage" not in out)

    # B3 调用抛异常 → 降级且不向上抛
    stub.raise_error = TimeoutError("simulated timeout")
    stub.calls.clear()
    out = await evidence_node(make_state("V-B3", http_images(1)))
    check("B3 超时/异常 → 降级为 llm_failed",
          out["evidence"].source == "llm_failed", f"实得 {out['evidence'].source}")
    check("B3 异常不向调用方抛出（已吞掉并降级）", True)
    stub.raise_error = None

    # B4 非 200（桩返回 (None, {})）
    original_text = stub.text
    stub.text = None
    stub.calls.clear()
    out = await evidence_node(make_state("V-B4", http_images(1)))
    check("B4 非 200 → 降级为 llm_failed",
          out["evidence"].source == "llm_failed", f"实得 {out['evidence'].source}")
    stub.text = original_text

    # B5 混合图片：只送 http 的那张
    stub.text = original_text  # 恢复为合法 JSON，避免与 B2/B4 的降级路径混淆
    stub.calls.clear()
    out = await evidence_node(make_state("V-B5", ["demo://evidence/1", *http_images(1)]))
    check("B5 混合图片 → 只送 http 图片", len(stub.calls[0]) == 1, f"实得 {stub.calls}")
    check("B5 有效图片数仍按全部计数", out["evidence"].image_count == 2,
          f"实得 {out['evidence'].image_count}")

    # B6 全部为非 http 图片 → 不调用（无可送模型的图片）
    stub.calls.clear()
    out = await evidence_node(make_state("V-B6", ["demo://evidence/1", "data/local/x.jpg"]))
    check("B6 全非 http → source=rule（有图但不可送模型）",
          out["evidence"].source == "rule", f"实得 {out['evidence'].source}")
    check("B6 全非 http → 零调用", len(stub.calls) == 0, f"实得 {len(stub.calls)} 次")

    # B7 带代码块的 JSON → 容错解析成功
    stub.text = '```json\n{"has_quality_claim": false, "claim_type": "wrong_item", "confidence": 0.5}\n```'
    stub.calls.clear()
    out = await evidence_node(make_state("V-B7", http_images(1)))
    check("B7 代码块包裹的 JSON 可解析", out["evidence"].source == "llm"
          and out["evidence"].claim_type == "wrong_item",
          f"source={out['evidence'].source} claim_type={out['evidence'].claim_type}")

    # B8 非法取值收敛：confidence 越界 / claim_type 不在白名单
    stub.text = json.dumps({"has_quality_claim": "yes", "claim_type": "not_a_real_type", "confidence": 9.9})
    stub.calls.clear()
    signals = await llm_client.extract_evidence_signals(http_images(1))
    check("B8 非法 claim_type 被收敛为 None", signals is not None and signals.claim_type is None,
          f"实得 {signals.claim_type if signals else None}")
    check("B8 confidence 收敛到 [0,1]", signals is not None and signals.confidence == 1.0,
          f"实得 {signals.confidence if signals else None}")

    # B9 买家留言默认不送模型（隐私边界）
    stub.text = original_text
    settings.vision_include_buyer_note = False
    captured: list[str] = []
    stub.calls.clear()

    def _capture(image_refs, prompt, *, timeout):  # noqa: ANN001
        captured.append(prompt)
        return stub.text, stub.usage

    llm_client._invoke_vision = _capture  # type: ignore[assignment]
    await evidence_node(make_state("V-B9", http_images(1), note="我的手机号是 13800000000"))
    llm_client._invoke_vision = stub  # type: ignore[assignment]
    check("B9 默认不把买家留言送模型（个人信息不出境）",
          captured and "13800000000" not in captured[0], f"prompt 片段={captured[:1]}")


# ============================================================
# C 组：模型信号**不得**影响判定（业务红线）
# ============================================================
async def run_no_effect_on_decision(graph) -> None:
    print()
    print("【C 组】模型信号不影响判定（核心红线）")

    oid_a, oid_b = "T-VIS-OFF", "T-VIS-ON"
    for oid in (oid_a, oid_b):
        seed_order(oid, pay_amount=1800.0, logistics_status=LogisticsStatus.SIGNED)
        seed_logistics(oid, logistics_status=LogisticsStatus.SIGNED)
        seed_profile(oid, buyer_credit_score=700, buyer_refund_rate_90d=0.05, refund_count_90d=0)

    def req(oid: str) -> RefundRequest:
        return RefundRequest(order_id=oid, pay_amount=1800.0, claim_amount=1800.0,
                             refund_reason_code=RefundReason.QUALITY, claim_images=http_images(2))

    # 第一次：闸门关闭
    settings.llm_enabled = False
    off = await graph.ainvoke({"request": req(oid_a)}, config={"configurable": {"thread_id": oid_a}})

    # 第二次：闸门开启，桩返回"图片有质量问题迹象"
    settings.llm_enabled = True
    stub.raise_error = None
    stub.text = json.dumps({"has_quality_claim": True, "claim_type": "quality",
                            "visible_defect": True, "confidence": 0.95, "summary": "明显破损"})
    stub.calls.clear()
    on = await graph.ainvoke({"request": req(oid_b)}, config={"configurable": {"thread_id": oid_b}})
    await graph.ainvoke(
        Command(resume={"decision": "maintain", "reviewer": "tester"}),
        config={"configurable": {"thread_id": oid_b}},
    )

    check("C1 模型确实介入了链路（信号有变化）",
          off.get("evidence").has_quality_claim is False
          and on.get("evidence").has_quality_claim is True,
          f"off={off.get('evidence').has_quality_claim} on={on.get('evidence').has_quality_claim}")
    check("C2 结论不因模型而改变",
          off.get("conclusion") == on.get("conclusion"),
          f"{off.get('conclusion')} vs {on.get('conclusion')}")
    check("C3 命中规则不因模型而改变",
          sorted(off.get("hits", [])) == sorted(on.get("hits", [])),
          f"{sorted(off.get('hits', []))} vs {sorted(on.get('hits', []))}")
    check("C4 大额质量仍为转人工（红线未被绕过）",
          on.get("conclusion") == "to_human", f"实得 {on.get('conclusion')}")


# ============================================================
# 主流程
# ============================================================
async def amain() -> int:
    original = {
        "llm_enabled": settings.llm_enabled,
        "dashscope_api_key": settings.dashscope_api_key,
        "vision_max_images": settings.vision_max_images,
        "vision_include_buyer_note": settings.vision_include_buyer_note,
        "audit_db_enabled": settings.audit_db_enabled,
    }
    settings.audit_db_enabled = False  # 本测试不涉及审计落库

    ckpt_path = PROJECT_ROOT / "data" / "test_vision_ckpt.sqlite"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.unlink(missing_ok=True)
    checkpointer = await make_checkpointer(str(ckpt_path))
    graph = build_graph(checkpointer)

    # 用桩替换底层调用：本测试全程**不联网、不产生费用**
    llm_client._invoke_vision = stub  # type: ignore[assignment]
    print("视觉调用已替换为本机桩函数（零网络、零费用）")
    print("-" * 72)

    try:
        await run_gate_closed()
        await run_gate_open()
        await run_no_effect_on_decision(graph)
    finally:
        llm_client._invoke_vision = _real_invoke  # type: ignore[assignment]
        for key, value in original.items():
            setattr(settings, key, value)
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
