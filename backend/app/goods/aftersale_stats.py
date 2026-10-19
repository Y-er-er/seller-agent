"""商品 × 售后：按商品聚合售后统计。

数据来源是 `conversation_case.goods_no` —— 受理时从订单事实带入的**关联键**
（见 `docs/契约_外部数据源_v1.md` §一 的 `goods_no`）。

**全程无模型参与**：纯确定性聚合，同输入同结论。这也是它能作为"商品健康度"依据的原因。

两处刻意的口径：

1. **退货率 = （退款 + 补发）/ 工单总数** —— 补发也是履约失败，算进"这个商品让我赔了"。
2. **未判定的工单不当作"没退货"** —— 它们单独归入 `processing`，
   否则"刚提交还没判"会把退货率稀释成好看的数字（那是自欺）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import chat_repo
from app.db import goods_repo as goods_repo
from app.goods.service import GoodsError

logger = logging.getLogger(__name__)

#: 判定结果里，哪些算"这个商品让我付出了成本"
_COSTLY_OUTCOMES = ("refund", "reship")


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """把工单列表聚合成统计。**纯函数**（便于单测，不碰数据库）。"""
    total = len(cases)
    outcomes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    escalated = 0

    for case in cases:
        result = str(case.get("decision_result") or "") or "processing"
        outcomes[result] = outcomes.get(result, 0) + 1
        if case.get("workflow_status") == "escalated" or case.get("status") == "ESCALATED":
            escalated += 1
        reason = str(case.get("escalated_reason") or "")
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1

    costly = sum(outcomes.get(key, 0) for key in _COSTLY_OUTCOMES)
    return {
        "total": total,
        "outcomes": outcomes,
        "escalated": escalated,
        "reasons": reasons,
        # 没有工单时退货率给 None 而不是 0 —— "没数据"与"退货率为零"是两件事
        "refund_rate": (round(costly / total * 100, 1) if total else None),
        "costly_count": costly,
        "processing": outcomes.get("processing", 0),
    }


async def stats_for_goods(seller_id: str, goods_id: int, *, recent_limit: int = 5) -> dict[str, Any]:
    """某个商品的售后统计（工单数 / 退货率 / 结果分布 / 问题分布 / 最近工单）。"""
    goods = await goods_repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    goods_no = str(goods.get("goods_no") or "")
    cases = await chat_repo.list_cases_by_goods(goods_no, limit=200) if goods_no else []
    summary = _summarize(cases)

    return {
        "goods_no": goods_no,
        "title": goods.get("title"),
        "category": goods.get("category") or "",
        **summary,
        "recent_cases": [
            {
                "case_id": c.get("case_id"),
                "order_id": c.get("order_id"),
                "status": c.get("status"),
                "workflow_status": c.get("workflow_status"),
                "assignee": c.get("assignee"),
                "escalated_reason": c.get("escalated_reason"),
                "decision_result": c.get("decision_result"),
                "decision_amount": c.get("decision_amount"),
                "created_at": c.get("created_at"),
            }
            for c in cases[:recent_limit]
        ],
        # 无关联工单时**如实说明原因**，而不是显示一堆 0 让人以为"这商品很健康"
        "note": "" if cases else (
            "该商品暂无关联的售后工单。"
            "（订单需带上 `goods_no` 才会关联；见 docs/契约_外部数据源_v1.md §一）"
        ),
    }


async def health_ranking(seller_id: str, *, limit: int = 3, min_cases: int = 1) -> list[dict[str, Any]]:
    """按退货率排出的「商品健康度」榜（概览用）。

    `min_cases` 用于过滤样本太小的商品 —— 1 单里退了 1 单就是 100%，
    直接上榜会误导；这是**统计口径**，不是"美化数据"（前端会标注样本量）。
    """
    goods_list = await goods_repo.list_goods(seller_id, scope="all", limit=200)
    ranking: list[dict[str, Any]] = []
    for goods in goods_list:
        goods_no = str(goods.get("goods_no") or "")
        if not goods_no:
            continue
        cases = await chat_repo.list_cases_by_goods(goods_no, limit=200)
        if len(cases) < min_cases:
            continue
        summary = _summarize(cases)
        if not summary["total"]:
            continue
        ranking.append({
            "goods_no": goods_no,
            "title": goods.get("title"),
            "total": summary["total"],
            "refund_rate": summary["refund_rate"],
            "costly_count": summary["costly_count"],
        })
    ranking.sort(key=lambda item: (item["refund_rate"] or 0, item["total"]), reverse=True)
    return ranking[:limit]
