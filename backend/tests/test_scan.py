"""经营巡检测试 —— **纯函数 + 假仓储，零外部依赖**。

这次的重点是两条踩过的坑：
  A 时间字段可能是**字符串**（仓储 `_normalize` 会转 ISO）——只认 `datetime` 会让
    「售后待接手」这一类**永远扫不出来**（静默少一类，界面看起来"巡检不完整"）
  B 某一类查询失败**不能拖垮整体**（巡检是旁路，缺一类好过全崩）

运行：python backend/tests/test_scan.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import scan as sc  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def group_a() -> None:
    print("【A 组】时间解析（仓储会返回字符串，不能只认 datetime）")
    now = datetime.now()
    check("A1 datetime 对象", round(sc._hours_since(now - timedelta(hours=5)) or -1) == 5)
    check("A2 ISO 字符串",
          round(sc._hours_since((now - timedelta(hours=3)).isoformat()) or -1) == 3)
    check("A3 MySQL 格式 YYYY-MM-DD HH:MM:SS",
          round(sc._hours_since((now - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")) or -1) == 8)
    check("A4 None / 空串 → None",
          sc._hours_since(None) is None and sc._hours_since("") is None)
    check("A5 无法解析 → None", sc._hours_since("不是时间") is None)


class FakeGoods:
    STATUS_DRAFT = "draft"

    def __init__(self, low=None, drafts=None, trash=None):
        self.low, self.drafts, self.trash = low or [], drafts or [], trash or []

    async def list_low_stock(self, seller_id, limit=20):
        return self.low

    async def list_goods(self, seller_id, **kw):
        return self.drafts

    async def list_trash(self, seller_id, limit=50):
        return self.trash


class FakeChat:
    def __init__(self, cases=None):
        self.cases = cases or []

    async def list_cases_by_workflow(self, status, limit=50):
        return self.cases


class FakeTask:
    TASK_FAILED = "failed"

    def __init__(self, tasks=None):
        self.tasks = tasks or []

    async def list_tasks(self, seller_id, **kw):
        return self.tasks


class BoomGoods:
    """所有查询都失败 —— 验证"单项失败不拖垮整体"。"""

    STATUS_DRAFT = "draft"

    async def list_low_stock(self, *a, **k):
        raise RuntimeError("DB 挂了")

    async def list_goods(self, *a, **k):
        raise RuntimeError("DB 挂了")

    async def list_trash(self, *a, **k):
        raise RuntimeError("DB 挂了")


async def group_b() -> None:
    print("\n【B 组】五类判定")
    now = datetime.now()
    sc.goods_repo = FakeGoods(
        low=[{"goods_no": "G-1", "title": "低库存品", "stock": 2, "stock_alert": 10}],
        drafts=[{"goods_no": "G-2", "title": "缺文案品", "description": "", "price": 9}],
        trash=[{"goods_no": "G-3", "title": "快到期", "days_since": 6}],
    )
    sc.chat_repo = FakeChat([{
        "case_id": "C1", "escalated_reason": "buyer_request",
        "created_at": (now - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S"),
    }])
    sc.task_repo = FakeTask([{"title": "批量失败", "target": "G-1", "error": "部分失败"}])

    r = await sc.scan("s")
    kinds = r["by_kind"]
    check("B1 低库存", kinds.get("low_stock") == 1, str(kinds))
    check("B2 草稿不完整", kinds.get("draft_incomplete") == 1, str(kinds))
    check("B3 售后待接手（**字符串时间也要能算**）", kinds.get("aftersale_waiting") == 1, str(kinds))
    check("B4 任务失败", kinds.get("task_failed") == 1, str(kinds))
    check("B5 回收站到期", kinds.get("trash_expiring") == 1, str(kinds))
    check("B6 high 级别排在最前", r["issues"][0]["level"] == "high", r["issues"][0]["kind"])
    check("B7 每条都带建议动作", all(i.get("suggest") is not None for i in r["issues"]))

    sc.chat_repo = FakeChat([{
        "case_id": "C2",
        "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }])
    r2 = await sc.scan("s")
    check("B8 售后不足 4 小时不报（不是一有转人工就报）",
          "aftersale_waiting" not in r2["by_kind"], str(r2["by_kind"]))

    sc.goods_repo = BoomGoods()
    r3 = await sc.scan("s")
    check("B9 某类查询失败不拖垮整体（缺一类好过全崩）",
          isinstance(r3.get("count"), int), str(r3)[:100])


def group_c() -> None:
    print("\n【C 组】规则自述（让边界可知）")
    info = sc.describe_rules()
    check("C1 列出 5 类检查项", info["count"] == 5 and len(info["rules"]) == 5, str(info["count"]))
    check("C2 每类都有判定标准与级别",
          all(r.get("rule") and r.get("level") for r in info["rules"]))
    check("C3 明确列出「不检查」的方面（不让人以为它都看过了）",
          len(info.get("not_covered") or []) >= 3, str(info.get("not_covered"))[:80])
    low = [r for r in info["rules"] if r["kind"] == "low_stock"]
    check("C4 断货规则写清了「不看阈值」",
          bool(low) and "不看阈值" in low[0]["rule"], low[0]["rule"] if low else "缺失")


async def main() -> int:
    original = (sc.goods_repo, sc.chat_repo, sc.task_repo)
    try:
        group_a()
        await group_b()
        group_c()
    finally:
        sc.goods_repo, sc.chat_repo, sc.task_repo = original

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
