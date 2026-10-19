"""商品域的 AI 能力：**文案生成**与**定价建议**。

两条设计原则（与项目既有取向一致）：

1. **数字由代码校验，措辞由模型给。**
   定价建议里模型给区间与理由，但代码必须校验边界（不低于成本、区间不倒挂），
   并把「同类目均价 / 成本 / 毛利」这类事实**算好再喂给模型** —— 不让模型凭空编市场数据。
2. **生成不等于落地。**
   生成结果只是**候选**，卖家点「采纳」才写入商品（走 `update_goods`，正常留痕）。
   因此本模块**不落库生成结果**：避免库里堆一堆没人采纳的草稿。

⚠️ 两个函数都会触发**真实计费**调用；`LLM_ENABLED=false` 或未配置密钥时明确报错（不静默返回空）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.db import goods_repo as repo
from app.goods.service import GoodsError
from app.llm.client import get_text_llm, parse_json_object, text_available

logger = logging.getLogger(__name__)

#: 生成类任务比"润色"耗时更长（要真正产出内容），超时给宽一点
_GENERATE_TIMEOUT = 30.0

#: 允许的文案风格
COPY_STYLES = ("专业", "口语", "促销")

_STYLE_HINTS = {
    "专业": "客观、克制，突出参数与使用场景，不夸张",
    "口语": "生活化、像朋友推荐，短句为主",
    "促销": "有行动号召，强调性价比与限时感（但不得使用绝对化用语）",
}


# ============================================================
# 通用：调用文本模型
# ============================================================


def _ensure_available() -> None:
    if not text_available():
        raise GoodsError(
            "model_unavailable",
            "模型未启用或未配置密钥：请检查 .env 的 LLM_ENABLED 与 DASHSCOPE_API_KEY",
            503,
        )


async def _invoke_text(prompt: str) -> str | None:
    """调用文本模型，返回纯文本；失败返回 None（不抛 —— 由调用方转成明确错误）。"""
    llm = get_text_llm()
    if llm is None:
        return None
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(llm.invoke, prompt), timeout=_GENERATE_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 —— 生成失败要能降级/报错，不该炸整个请求
        logger.warning("文本生成失败：%s: %s", type(exc).__name__, exc)
        return None
    content = getattr(response, "content", None)
    return content.strip() if isinstance(content, str) and content.strip() else None


#: 公开别名：商品**宣传图生成**（`app/goods/image_ai.py`）也要用文本模型写生图提示词。
invoke_text = _invoke_text


async def _log_ai_action(seller_id: str, action: str, target_id: str, summary: str) -> None:
    """把 AI 的生成动作记进操作日志 —— AI 用过什么能力应当是可见的。"""
    try:
        await repo.insert_operation(
            actor=repo.ACTOR_AI, action=action, target_id=target_id,
            summary=summary, undoable=False, seller_id=seller_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不该让生成结果作废
        logger.warning("记录 AI 动作失败：%s", exc)


# ============================================================
# 文案生成
# ============================================================


def _copy_prompt(goods: dict[str, Any], style: str) -> str:
    media_count = len(goods.get("media") or [])
    return (
        "你是电商商品文案写手。请为下面这件商品写一段**商品文案**。\n\n"
        "商品信息：\n"
        f"- 标题：{goods.get('title')}\n"
        f"- 类目：{goods.get('category') or '（未填）'}\n"
        f"- 售价：¥{float(goods.get('price') or 0):.2f}\n"
        f"- 现有文案：{(goods.get('description') or '（暂无）')[:200]}\n"
        f"- 商品图片：{media_count} 张\n\n"
        f"要求：\n"
        f"1. 风格：{style}（{_STYLE_HINTS.get(style, '')}）\n"
        "2. 长度 40—90 字，2—4 个卖点，可用「·」分隔\n"
        "3. **只输出文案正文**：不要标题、不要解释、不要引号、不要 markdown 标记、不要换行罗列\n"
        "4. **不得编造**商品信息里没有的内容（材质、认证、保修、销量等一律不写）\n"
        "5. 不得使用「最」「第一」「国家级」等绝对化用语"
    )


async def generate_copy(
    seller_id: str, goods_id: int, style: str = "口语"
) -> dict[str, Any]:
    """为商品生成一版**候选文案**（不写库；采纳由前端调 `PATCH` 完成）。"""
    if style not in COPY_STYLES:
        raise GoodsError("invalid_field", f"不支持的风格：{style}（可选 {'、'.join(COPY_STYLES)}）")
    _ensure_available()

    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    text = await _invoke_text(_copy_prompt(goods, style))
    if text is None:
        raise GoodsError("generate_failed", "文案生成失败（模型超时或返回为空），请重试", 503)

    # 兜底清理：模型偶尔会自作主张加引号
    clean = text.strip().strip('"').strip("'").strip("“”").strip()
    await _log_ai_action(
        seller_id, "ai_generate_copy", str(goods.get("goods_no") or goods_id),
        f"AI 生成商品文案（风格：{style}）",
    )
    return {
        "goods_no": goods.get("goods_no"),
        "style": style,
        "copy": clean,
        "model": settings.llm_text_model,
    }


# ============================================================
# 定价建议
# ============================================================


async def _peer_stats(seller_id: str, goods: dict[str, Any]) -> dict[str, Any]:
    """同类目价位（来自**卖家自己的**商品库，不含当前商品）。

    ⚠️ 刻意只用自己的数据，而不是"编一个市场行情"：本项目没有外部行情数据源，
    凭空给"市场价"就是造假。宁可说清"这是你自己同类目商品的价位"。
    """
    category = str(goods.get("category") or "").strip()
    all_goods = await repo.list_goods(seller_id, scope="all", limit=200)
    peers = [
        g for g in all_goods
        if g["id"] != goods["id"] and str(g.get("category") or "").strip() == category
        and float(g.get("price") or 0) > 0
    ]
    prices = sorted(float(g["price"]) for g in peers)
    if not prices:
        return {"count": 0, "avg": None, "min": None, "max": None}
    return {
        "count": len(prices),
        "avg": round(sum(prices) / len(prices), 2),
        "min": prices[0],
        "max": prices[-1],
    }


def _price_prompt(goods: dict[str, Any], peers: dict[str, Any]) -> str:
    cost = float(goods.get("cost_price") or 0)
    price = float(goods.get("price") or 0)
    peer_line = (
        f"- 同类目价位：均价 ¥{peers['avg']:.2f}（区间 ¥{peers['min']:.2f}—¥{peers['max']:.2f}，"
        f"共 {peers['count']} 件同类商品）"
        if peers["count"] else
        "- 同类目价位：（没有其它同类目商品，无参考）"
    )
    return (
        "你是电商定价参谋。请基于下面**已核实的事实**给出定价建议。\n\n"
        "事实（不得改动，也不得补充别的数字）：\n"
        f"- 商品：{goods.get('title')}（{goods.get('category') or '未分类'}）\n"
        f"- 当前售价：¥{price:.2f}\n"
        f"- 成本价：¥{cost:.2f}\n"
        f"{peer_line}\n"
        f"- 现有卖点：{(goods.get('description') or '（暂无）')[:150]}\n\n"
        "只输出一个 JSON 对象，**不要任何解释文字、不要代码块标记**：\n"
        '{"min": 数字, "max": 数字, "suggested": 数字, "reason": "不超过 80 字的理由"}\n\n'
        "约束：\n"
        f"1. suggested 必须 ≥ {cost:.2f}（**不得低于成本**）\n"
        "2. min ≤ suggested ≤ max，三者是合理的人民币金额（最多两位小数）\n"
        "3. reason 只能引用上面给出的数字，**不得编造**市场数据、竞品或销量"
    )


def _fallback_suggestion(cost: float, price: float, peers: dict[str, Any]) -> dict[str, Any]:
    """模型不可用/输出不可解析时的**确定性回落**：按成本加成给一个保守区间。

    为什么要有回落：定价建议不该因为"模型没说好"就整体失败 —— 卖家要的是能用的建议。
    """
    base = max(cost * 2.5, price, 1.0)
    low, high = round(base * 0.92, 2), round(base * 1.12, 2)
    reason = (
        f"按成本 ¥{cost:.2f} 的 2.5 倍估算，建议区间 ¥{low:.2f}—¥{high:.2f}。"
        + (f"同类目均价 ¥{peers['avg']:.2f}，可作为对照。" if peers["count"] else "")
        + "（此建议由规则给出，未使用模型）"
    )
    return {
        "min": low, "max": high, "suggested": round(base, 2), "reason": reason,
        "source": "rule_fallback", "adjusted": False,
    }


def _sanitize_suggestion(
    raw: dict[str, Any], cost: float, price: float, peers: dict[str, Any]
) -> dict[str, Any]:
    """校验并修正模型给的建议 —— **代码守边界，模型只出主意**。"""
    def num(key: str) -> float | None:
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            return None
        return value if value == value and abs(value) < 1e9 else None   # 过滤 NaN / 离谱值

    lo, hi, mid = num("min"), num("max"), num("suggested")
    if lo is None or hi is None or mid is None:
        return _fallback_suggestion(cost, price, peers)

    lo, hi = min(lo, hi), max(lo, hi)          # 区间倒挂直接纠正
    adjusted = False
    floor = round(cost, 2) if cost > 0 else 0.01
    if mid < floor:                             # **低于成本必须修正**：这是硬边界
        mid, adjusted = floor, True
    mid = round(mid, 2)
    lo = round(max(lo, floor), 2)
    hi = round(max(hi, mid, lo), 2)

    reason = str(raw.get("reason") or "").strip()[:200] or "模型未给出理由。"
    if adjusted:
        reason += f"（注：模型建议价低于成本 ¥{cost:.2f}，已修正为不低于成本）"
    return {
        "min": lo, "max": hi, "suggested": mid, "reason": reason,
        "source": "model", "adjusted": adjusted,
    }


async def suggest_price(seller_id: str, goods_id: int) -> dict[str, Any]:
    """给出定价建议（模型给区间与理由，**代码校验边界**）。"""
    _ensure_available()
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    cost = float(goods.get("cost_price") or 0)
    price = float(goods.get("price") or 0)
    peers = await _peer_stats(seller_id, goods)

    text = await _invoke_text(_price_prompt(goods, peers))
    parsed = parse_json_object(text) if text else None
    result = (
        _sanitize_suggestion(parsed, cost, price, peers) if parsed
        else _fallback_suggestion(cost, price, peers)
    )

    await _log_ai_action(
        seller_id, "ai_suggest_price", str(goods.get("goods_no") or goods_id),
        f"AI 定价建议：¥{result['suggested']:.2f}"
        + ("（已修正，原建议低于成本）" if result["adjusted"] else ""),
    )
    margin = None
    if result["suggested"] > 0 and cost > 0:
        margin = round((result["suggested"] - cost) / result["suggested"] * 100, 1)
    return {
        **result,
        "goods_no": goods.get("goods_no"),
        "cost_price": cost,
        "current_price": price,
        "peer_avg": peers["avg"],
        "peer_count": peers["count"],
        "margin_percent": margin,
        "model": settings.llm_text_model if result["source"] == "model" else "",
    }
