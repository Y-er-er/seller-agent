"""模型调用失败的**可诊断性**测试 —— 不连库、不调模型。

来历（2026-10-21 实测）：卖家连问两次只拿到「抱歉，我这边调用模型时出错了」，
而当时唯一的日志是 `logger.warning` 走 stdout —— 终端一滚屏原因就没了，事后只能靠
`llm_usage` 里"这两次一条记录都没有"反推出「请求在计费前就被拒了」，却仍不知道是
400（消息格式）还是 429（限流）。这组测试钉住三条补丁：

  S 组 `_skeleton`：把请求压成"角色 + tool_calls + id + 长度"骨架，异常一眼可判
  D 组 `_dump_model_failure`：失败现场落盘，服务重启也不丢
  E 组 `_ensure_call_ids`：空 tool_call_id 就地补齐，掐掉一类"不计 token 的 400"

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_loop_model_error.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import loop  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402
from app.chat.llm_adapter import ToolCall  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def test_skeleton() -> None:
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "联想与显卡差多少钱"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "", "name": "list_goods", "arguments": {}},
            {"id": "c2", "name": "list_goods", "arguments": {}},
        ]},
        {"role": "tool", "tool_call_id": "", "name": "list_goods", "content": "没有"},
    ]
    sk = loop._skeleton(msgs)
    check("S1 骨架标出空 tool_call id", "EMPTY" in sk, sk)
    check("S2 骨架标出 tool 消息缺配对 id", "[id=EMPTY]" in sk, sk)
    check("S3 骨架含内容长度", "(len=" in sk, sk)
    check("S4 骨架按角色顺序串联",
          sk.index("system") < sk.index("user") < sk.index("assistant") < sk.index("tool"), sk)


def test_dump_model_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "assistant_model_error.log"
        original = loop._MODEL_ERROR_LOG
        loop._MODEL_ERROR_LOG = tmp
        try:
            ctx = AssistantContext(seller_id="s", thread_id="t", view="admin", selected_goods_no="")
            loop._dump_model_failure(
                ctx, ValueError("InvalidParameter: url error"),
                [{"role": "user", "content": "问一句"}],
            )
            text = tmp.read_text(encoding="utf-8")
            check("D1 失败现场已落盘", tmp.exists() and "InvalidParameter" in text, text[:200])
            check("D2 记录线程与模型", "thread=t" in text and "model=" in text, text[:200])
            check("D3 记录消息骨架", "messages:" in text, text[:200])
        finally:
            loop._MODEL_ERROR_LOG = original


def test_ensure_call_ids() -> None:
    # 空 id 的调用会被补上；已有 id 的保持不变
    turn = type("T", (), {})()
    turn.tool_calls = [
        ToolCall(name="list_goods", arguments={}, id=""),
        ToolCall(name="get_goods", arguments={}, id="keep_me"),
    ]
    loop._ensure_call_ids(turn)
    ids = [c.id for c in turn.tool_calls]
    check("E1 空 id 被补齐", ids[0] and ids[0].startswith("call_"), ids[0])
    check("E2 已有 id 不被覆盖", ids[1] == "keep_me", ids[1])
    check("E3 补齐的 id 唯一", ids[0] != ids[1], str(ids))


def main() -> None:
    test_skeleton()
    test_dump_model_failure()
    test_ensure_call_ids()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
