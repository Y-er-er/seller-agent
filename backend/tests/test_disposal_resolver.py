"""处置结果解析器测试 —— 纯函数，零外部依赖。

覆盖：诉求 × 结论 的完整映射、枚举/字符串入参、未知结论与未知诉求兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import ClaimType  # noqa: E402
from app.rules.disposal import DISPOSAL_RESULTS, resolve_disposal  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def main() -> int:
    # ---- 枚举完整性 ----
    check("处置结果枚举含 7 项", set(DISPOSAL_RESULTS) == {
        "refund", "reship", "reject", "manual", "pending_evidence", "exchange", "partial_refund",
    }, str(DISPOSAL_RESULTS))

    # ---- auto_pass × 诉求分类 ----
    cases = [
        (ClaimType.REFUND, "refund"),
        (ClaimType.RETURN_REFUND, "refund"),
        (ClaimType.RESHIP, "reship"),
        (ClaimType.EXCHANGE, "exchange"),
        (ClaimType.PARTIAL_REFUND, "partial_refund"),
    ]
    for ct, expected in cases:
        check(f"auto_pass + {ct.value} → {expected}", resolve_disposal(ct, "auto_pass") == expected)

    # ---- 其他结论（与诉求无关）----
    check("auto_reject → reject", resolve_disposal(ClaimType.REFUND, "auto_reject") == "reject")
    check("auto_reject + reship → reject（补发也被拒）",
          resolve_disposal(ClaimType.RESHIP, "auto_reject") == "reject")
    check("request_evidence → pending_evidence",
          resolve_disposal(ClaimType.REFUND, "request_evidence") == "pending_evidence")
    check("to_human → manual", resolve_disposal(ClaimType.RESHIP, "to_human") == "manual")

    # ---- 字符串入参与未知兜底 ----
    check("字符串入参 'reship' 等价枚举", resolve_disposal("reship", "auto_pass") == "reship")
    check("未知结论 → manual（不猜测）", resolve_disposal("refund", "some_new_conclusion") == "manual")
    check("未知诉求 → 默认退款", resolve_disposal("mystery_type", "auto_pass") == "refund")

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    if _failures:
        for f in _failures:
            print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
