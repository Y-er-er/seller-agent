"""商品 × 售后联动的统计测试 —— **纯函数 + 假仓储，零外部依赖**。

覆盖重点（都是"口径"问题，算错了会给出误导性的经营判断）：
  A `_summarize` 纯函数：空集 / 退货率口径 / 未判定不算"没退货" / 转人工计数 / 原因分布
  B `stats_for_goods`：商品不存在 → 404；无关联工单 → **如实说明原因**而不是返回一堆 0
  C `health_ranking`：**批量聚合**（B10 钉住"不再逐商品查"，B11 钉住无商品时不发空 IN 查询）

运行（无需 MySQL、无网络、不调用模型）：
    python backend/tests/test_aftersale_stats.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.goods import aftersale_stats  # noqa: E402
from app.goods.service import GoodsError  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class FakeGoodsRepo:
    def __init__(self, goods):
        self.goods = goods

    async def get_goods(self, goods_id, seller_id):
        return dict(self.goods) if self.goods and self.goods["id"] == goods_id else None

    async def list_goods(self, seller_id, **kw):
        return [dict(self.goods)] if self.goods else []


class FakeChatRepo:
    """假售后仓储。

    `list_cases_by_goods` 保留 —— `stats_for_goods`（单品页）仍走它；
    `aggregate_cases_by_goods` 是**批量版**，健康度榜走这条。
    两个计数器用来钉住 N+1 不再回来（见 B10）。
    """

    def __init__(self, cases):
        self.cases = cases
        self.batch_calls = 0
        self.per_goods_calls = 0

    async def list_cases_by_goods(self, goods_no, limit=200):
        self.per_goods_calls += 1
        return [c for c in self.cases if c.get("goods_no") == goods_no][:limit]

    async def aggregate_cases_by_goods(self, goods_nos):
        """与 `_aggregate_cases_by_goods` 同口径：无工单的商品**不出现在返回里**。"""
        self.batch_calls += 1
        out = {}
        for no in goods_nos:
            rows = [c for c in self.cases if c.get("goods_no") == no]
            if not rows:
                continue
            costly = sum(
                1 for c in rows if str(c.get("decision_result") or "") in ("refund", "reship")
            )
            out[no] = {"total": len(rows), "costly": costly}
        return out


def group_a() -> None:
    print("【A 组】聚合口径（纯函数）")

    empty = aftersale_stats._summarize([])
    check("A1 空集：工单数为 0", empty["total"] == 0)
    # 关键：没有工单时退货率必须是 None 而不是 0 —— "没数据"与"退货率为零"是两件事
    check("A2 空集：退货率为 None（不是 0）", empty["refund_rate"] is None, str(empty["refund_rate"]))

    cases = [
        {"decision_result": "refund"},
        {"decision_result": "refund"},
        {"decision_result": "reship"},
        {"decision_result": "reject"},
        {"decision_result": None},                       # 还没判定
        {"decision_result": "manual", "escalated_reason": "rule_to_human",
         "workflow_status": "escalated"},
    ]
    s = aftersale_stats._summarize(cases)
    check("A3 工单总数", s["total"] == 6, str(s["total"]))
    check("A4 赔付类 = 退款 2 + 补发 1", s["costly_count"] == 3, str(s["costly_count"]))
    # 补发也算履约失败，必须计入退货率分子
    check("A5 退货率 = 3/6 = 50%", s["refund_rate"] == 50.0, str(s["refund_rate"]))
    # 关键：未判定的工单归入 processing，**不被当作"没退货"**（否则会稀释退货率）
    check("A6 未判定归入 processing 且不被当作没退货",
          s["processing"] == 1 and s["outcomes"]["processing"] == 1, str(s["outcomes"]))
    check("A7 转人工计数", s["escalated"] == 1, str(s["escalated"]))
    check("A8 转人工原因分布", s["reasons"] == {"rule_to_human": 1}, str(s["reasons"]))
    check("A9 结果分布完整",
          s["outcomes"]["refund"] == 2 and s["outcomes"]["reship"] == 1
          and s["outcomes"]["reject"] == 1 and s["outcomes"]["manual"] == 1, str(s["outcomes"]))

    # 全是赔付类 → 100%
    all_costly = aftersale_stats._summarize([{"decision_result": "refund"}] * 4)
    check("A10 全赔付 → 100%", all_costly["refund_rate"] == 100.0, str(all_costly["refund_rate"]))

    # 终态 ESCALATED 也算转人工（不能只看 workflow_status）
    by_status = aftersale_stats._summarize([{"status": "ESCALATED"}])
    check("A11 status=ESCALATED 也计入转人工", by_status["escalated"] == 1, str(by_status["escalated"]))


async def group_b() -> None:
    print("\n【B 组】商品维度查询")
    goods = {"id": 1, "goods_no": "G-0001", "title": "测试品", "category": "测试"}
    aftersale_stats.goods_repo = FakeGoodsRepo(goods)

    # B1 商品不存在 → 404
    aftersale_stats.chat_repo = FakeChatRepo([])
    try:
        await aftersale_stats.stats_for_goods("s", 999)
        check("B1 商品不存在 → 404", False, "竟然通过")
    except GoodsError as exc:
        check("B1 商品不存在 → 404", exc.status_code == 404, str(exc.status_code))

    # B2 无关联工单 → note 说明原因，而不是返回一堆 0 让人以为"很健康"
    out = await aftersale_stats.stats_for_goods("s", 1)
    check("B2 无关联工单时给出说明",
          out["total"] == 0 and out["refund_rate"] is None and "goods_no" in out["note"],
          str(out)[:140])
    check("B3 无关联工单时 recent_cases 为空", out["recent_cases"] == [])

    # B4 有关联工单 → 正确聚合
    aftersale_stats.chat_repo = FakeChatRepo([
        {"case_id": "DEMO001", "goods_no": "G-0001", "decision_result": "refund"},
        {"case_id": "DEMO002", "goods_no": "G-0001", "decision_result": "reject"},
        {"case_id": "DEMO003", "goods_no": "G-9999", "decision_result": "refund"},   # 别的商品
    ])
    out = await aftersale_stats.stats_for_goods("s", 1)
    check("B5 只统计该商品的工单（不串到别的商品）",
          out["total"] == 2 and out["refund_rate"] == 50.0, str(out["total"]) + " " + str(out["refund_rate"]))
    check("B6 最近工单含 case_id",
          [c["case_id"] for c in out["recent_cases"]] == ["DEMO001", "DEMO002"],
          str(out["recent_cases"]))
    check("B7 有关联工单时不显示 note", out["note"] == "")

    # B8 健康度榜：排除样本量不足的
    aftersale_stats.goods_repo = FakeGoodsRepo(goods)
    ranking = await aftersale_stats.health_ranking("s", limit=3, min_cases=2)
    check("B8 健康度榜按退货率排序且过滤小样本",
          len(ranking) == 1 and ranking[0]["goods_no"] == "G-0001", str(ranking))
    ranking_loose = await aftersale_stats.health_ranking("s", limit=3, min_cases=1)
    check("B9 放宽样本门槛后仍只含有关联工单的商品", len(ranking_loose) == 1, str(ranking_loose))

    # B10 N+1 回归：健康度榜**不许**再逐商品查。
    # 为什么这条必须存在：连接层每次查询新建一条 MySQL 连接（远程库握手实测约 45ms），
    # 逐商品查 = 每个商品一次握手，22 件商品就要 ~1 秒，"概览"由此慢成 0.95s。
    # 而且它**随商品数线性恶化**，所以这是正确性问题，不是"优化一下"。
    fake = FakeChatRepo([
        {"goods_no": "G-0001", "decision_result": "refund"},
        {"goods_no": "G-0001", "decision_result": "reject"},
        {"goods_no": "G-9999", "decision_result": "refund"},
    ])
    aftersale_stats.chat_repo = fake
    await aftersale_stats.health_ranking("s", limit=3, min_cases=1)
    check("B10 健康度榜只发 1 次批量查询、0 次逐商品查询",
          fake.batch_calls == 1 and fake.per_goods_calls == 0,
          f"batch={fake.batch_calls} per_goods={fake.per_goods_calls}")

    # B11 无商品时不该白跑一次 SQL（空 IN 列表是语法错误，必须提前返回）
    empty = FakeChatRepo([])
    aftersale_stats.goods_repo = FakeGoodsRepo(None)
    aftersale_stats.chat_repo = empty
    out_empty = await aftersale_stats.health_ranking("s", limit=3)
    check("B11 没有商品时直接返回空榜，且不发起查询",
          out_empty == [] and empty.batch_calls == 0,
          f"{out_empty} batch={empty.batch_calls}")


async def main() -> int:
    group_a()
    await group_b()
    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
