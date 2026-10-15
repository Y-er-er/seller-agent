"""规则表加载期校验测试。

目的：证明"规则写错会在加载期立刻失败"，而不是静默永不命中。

运行：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_rules_validator.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.rules import load_rules  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f"  {detail}"
    print(line)
    if not ok:
        failures.append(f"{name} {detail}".strip())


def expect_error(name: str, yaml_text: str, keyword: str) -> None:
    """写一份临时规则表，断言加载时抛出 ValueError 且信息包含关键字。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(yaml_text)
        tmp = Path(fh.name)
    try:
        try:
            load_rules(tmp)
        except ValueError as exc:
            ok = keyword in str(exc)
            check(name, ok, f"异常信息未包含 {keyword!r}：{exc}")
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"抛出了非 ValueError：{type(exc).__name__}: {exc}")
        else:
            check(name, False, "未抛出任何异常（校验缺失）")
    finally:
        tmp.unlink(missing_ok=True)


HEAD = 'version: "test"\nrules:\n'
RULE = (
    "  - rule_id: {rid}\n"
    "    priority: {prio}\n"
    "    conclusion: {concl}\n"
    "    condition: {cond}\n"
)


def main() -> int:
    print("A. 合法规则表应正常加载")
    good = HEAD + RULE.format(
        rid="R1", prio=10, concl="to_human",
        cond="{all: [{field: claim_amount, op: gt, value: 500}]}",
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(good)
        tmp = Path(fh.name)
    try:
        table = load_rules(tmp)
        check("A1 合法规则表可加载", len(table.rules) == 1 and table.rules[0].rule_id == "R1")
    finally:
        tmp.unlink(missing_ok=True)

    print()
    print("B. 非法规则表应在加载期失败")

    expect_error(
        "B1 未知事实字段",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amounts, op: gt, value: 500}]}"),
        "未知事实字段",
    )
    expect_error(
        "B2 未知运算符",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amount, op: greater_than, value: 500}]}"),
        "未知运算符",
    )
    expect_error(
        "B3 非法结论",
        HEAD + RULE.format(rid="R1", prio=10, concl="auto_approve",
                           cond="{all: [{field: claim_amount, op: gt, value: 500}]}"),
        "非法结论",
    )
    expect_error(
        "B4 优先级重复",
        HEAD
        + RULE.format(rid="R1", prio=10, concl="to_human",
                      cond="{all: [{field: claim_amount, op: gt, value: 500}]}")
        + RULE.format(rid="R2", prio=10, concl="auto_pass",
                      cond="{all: [{field: claim_amount, op: lte, value: 50}]}"),
        "重复",
    )
    expect_error(
        "B5 规则 ID 重复",
        HEAD
        + RULE.format(rid="R1", prio=10, concl="to_human",
                      cond="{all: [{field: claim_amount, op: gt, value: 500}]}")
        + RULE.format(rid="R1", prio=20, concl="auto_pass",
                      cond="{all: [{field: claim_amount, op: lte, value: 50}]}"),
        "ID 重复",
    )
    expect_error(
        "B6 叶子缺少 op",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amount}]}"),
        "field 与 op",
    )
    expect_error(
        "B7 组合键混用",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amount, op: gt, value: 1}], any: []}"),
        "同时出现多个组合键",
    )
    expect_error(
        "B8 缺少 value",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amount, op: gt}]}"),
        "必须提供 value",
    )
    expect_error(
        "B9 引用未定义的 .env 配置项",
        HEAD + RULE.format(rid="R1", prio=10, concl="to_human",
                           cond="{all: [{field: claim_amount, op: gt, value: \"${NOT_EXIST_VAR}\"}]}"),
        "未定义的配置项",
    )

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
