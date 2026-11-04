"""经营巡检 —— 跨域扫出「需要卖家处理」的事。

**只读、只建议，不执行任何操作。** 这是刻意的边界：
巡检的价值是"让人看见该处理什么"，而不是"替人做决定"。
真正动手仍然走工具 + 确认卡那条路（不可逆动作人在环）。

同一份扫描结果供两处使用：
- 助手工具 `scan_issues`（让 AI 被问到时能给出全局视图）
- 前端待办提示（`GET /assistant/issues`，纯聚合、不调模型）

五类问题：

| 类型 | 判定 | 为什么要提 |
| --- | --- | --- |
| `low_stock` | 库存 ≤ 预警阈值 | 卖家看不到就会断货 |
| `draft_incomplete` | 草稿缺文案 或 价格 ≤ 0 | 上不了架，且自己常忘 |
| `aftersale_waiting` | 已转人工且超过 N 小时没人接 | 买家在等，越久越糟 |
| `task_failed` | 最近有任务失败 | 失败会静默，需要人知道 |
| `trash_expiring` | 回收站商品剩余 ≤ 1 天 | 到期就永久删了 |
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.db import chat_repo, goods_repo
from app.db import task_repo

logger = logging.getLogger(__name__)

#: 售后转人工超过这么久没人接，就算"该催了"
AFTERSALE_WAIT_HOURS = 4
#: 回收站剩余天数 ≤ 这个值就提醒
TRASH_WARN_DAYS = 1

#: 巡检**会**检查的 5 类问题（自述用）。
#:
#: 为什么要把它显式写出来：巡检规则是代码写死的，用户看不到"边界" —— 漏报时
#: 无法区分"规则没覆盖"和"真的没问题"。把规则明示出来，至少让漏报是**可知的**。
RULES: tuple[dict[str, Any], ...] = (
    {"kind": "low_stock", "label": "低库存 / 断货",
     "rule": "库存 = 0（断货，一律报，不看阈值）；或库存 ≤ 预警阈值（仅当阈值 > 0）",
     "level": "断货 high / 低于预警 normal"},
    {"kind": "draft_incomplete", "label": "草稿不完整",
     "rule": "状态是草稿，且缺文案 或 价格 ≤ 0", "level": "normal"},
    {"kind": "aftersale_waiting", "label": "售后待接手",
     "rule": f"已转人工，且超过 {AFTERSALE_WAIT_HOURS} 小时还没人处理", "level": "high"},
    {"kind": "task_failed", "label": "任务失败",
     "rule": "最近有失败的任务（AI 生图 / 批量操作等）", "level": "normal"},
    {"kind": "trash_expiring", "label": "回收站到期",
     "rule": "回收站里已删除 ≥ 6 天（默认保留 7 天，到期会永久删除）", "level": "high"},
)

#: 巡检**不**覆盖的方面 —— 明确说出来，比让用户以为"它都看过了"要好。
NOT_COVERED: tuple[str, ...] = (
    "商品定价是否合理（只报库存，不评判贵不贵）",
    "图片质量与合规（尺寸、违禁内容等）",
    "标题 / 文案的营销效果",
    "竞品价格对比（没有外部行情数据源）",
    "买家情绪与投诉倾向（售后域只看工单状态和等待时长）",
)


def describe_rules() -> dict[str, Any]:
    """巡检规则自述：检查哪些、判定标准、以及**不检查什么**。"""
    return {
        "rules": [dict(r) for r in RULES],
        "not_covered": list(NOT_COVERED),
        "count": len(RULES),
    }


def _hours_since(value: Any) -> float | None:
    """距现在多少小时。**兼容 datetime 与字符串**。

    ⚠️ 仓储层会把时间字段转成 ISO 字符串（`_normalize`），若这里只认 `datetime`，
    售后「待接手超 4 小时」这一类就**永远扫不出来** —— 巡检会静默少一类（实测踩到）。
    """
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if not text:
            return None
        parsed = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        value = parsed

    if not isinstance(value, datetime):
        return None
    try:
        return max(0.0, (datetime.now() - value).total_seconds() / 3600)
    except TypeError:      # naive 与 aware 混用
        return None


async def scan(seller_id: str) -> dict[str, Any]:
    """扫描一遍，返回 `{count, issues[], by_kind{}}`。

    ⚠️ 每条 issue 都带 `suggest`（建议动作）—— 只给建议，**不自动执行**：
    让 AI 有"话可说"，但决定权仍在卖家。
    """
    issues: list[dict[str, Any]] = []

    # ---- 1) 低库存 / 断货 ----
    try:
        for g in await goods_repo.list_low_stock(seller_id, limit=20):
            stock = int(g.get("stock") or 0)
            alert = int(g.get("stock_alert") or 0)
            if stock <= 0:
                # 断货比"低于自定义线"严重：买家根本下不了单
                detail = "**已断货**（库存 0），买家无法下单"
                suggest = "补货；或先下架，避免买家拍下后无法发货"
                level = "high"
            else:
                detail = f"库存 {stock}，低于预警 {alert}"
                suggest = "补货；或先下架，避免买家下单后无货"
                level = "normal"
            issues.append({
                "kind": "low_stock",
                "level": level,
                "target": str(g.get("goods_no") or ""),
                "title": str(g.get("title") or ""),
                "detail": detail,
                "suggest": suggest,
            })
    except Exception as exc:  # noqa: BLE001 —— 单项失败不影响整体巡检
        logger.warning("巡检：低库存查询失败：%s", exc)

    # ---- 2) 草稿不完整 ----
    try:
        drafts = await goods_repo.list_goods(seller_id, status=goods_repo.STATUS_DRAFT, limit=50)
        for g in drafts:
            missing: list[str] = []
            if not str(g.get("description") or "").strip():
                missing.append("文案")
            if float(g.get("price") or 0) <= 0:
                missing.append("价格")
            if missing:
                parts: list[str] = []
                if "文案" in missing:
                    parts.append("生成文案（generate_copy）")
                if "价格" in missing:
                    parts.append("补价格（update_goods）")
                issues.append({
                    "kind": "draft_incomplete",
                    "level": "normal",
                    "target": str(g.get("goods_no") or ""),
                    "title": str(g.get("title") or ""),
                    "detail": "草稿缺少：" + "、".join(missing),
                    "suggest": "；".join(parts),
                })
    except Exception as exc:  # noqa: BLE001
        logger.warning("巡检：草稿查询失败：%s", exc)

    # ---- 3) 售后转人工待接手 ----
    try:
        for case in await chat_repo.list_cases_by_workflow("escalated", limit=50):
            waited = _hours_since(case.get("created_at"))
            if waited is None or waited < AFTERSALE_WAIT_HOURS:
                continue
            issues.append({
                "kind": "aftersale_waiting",
                "level": "high",
                "target": str(case.get("case_id") or ""),
                "title": str(case.get("case_id") or ""),
                "detail": f"已转人工约 {int(waited)} 小时未处理"
                          + (f"（{case.get('escalated_reason')}）" if case.get("escalated_reason") else ""),
                "suggest": "在「售后」里接手处理；或退回智能体继续跑",
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("巡检：售后工单查询失败：%s", exc)

    # ---- 4) 失败的任务 ----
    try:
        for task in await task_repo.list_tasks(seller_id, status=task_repo.TASK_FAILED, limit=5):
            issues.append({
                "kind": "task_failed",
                "level": "normal",
                "target": str(task.get("target") or ""),
                "title": str(task.get("title") or ""),
                "detail": str(task.get("error") or "任务失败")[:120],
                "suggest": "重新发起一次；失败原因见上",
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("巡检：任务查询失败：%s", exc)

    # ---- 5) 回收站快到期 ----
    try:
        for g in await goods_repo.list_trash(seller_id, limit=50):
            since = g.get("days_since")
            if since is None:
                continue
            # 保留天数由调用方配置决定；这里用「已删天数 ≥ 保留天数 - 1」判断
            if int(since) >= 6:      # 默认保留 7 天 → 第 7 天提醒
                issues.append({
                    "kind": "trash_expiring",
                    "level": "high",
                    "target": str(g.get("goods_no") or ""),
                    "title": str(g.get("title") or ""),
                    "detail": f"已删除 {int(since)} 天，即将到期被彻底删除",
                    "suggest": "确认还要不要；要就恢复（restore_goods），不要就等它自动清理",
                })
    except Exception as exc:  # noqa: BLE001
        logger.warning("巡检：回收站查询失败：%s", exc)

    by_kind: dict[str, int] = {}
    for item in issues:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1

    issues.sort(key=lambda x: 0 if x["level"] == "high" else 1)
    return {"count": len(issues), "issues": issues, "by_kind": by_kind}
