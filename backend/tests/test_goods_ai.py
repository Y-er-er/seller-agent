"""商品域 AI 能力测试 —— **假模型，零计费**。

覆盖重点：
  A 文案生成：正常生成 / 清理引号 / 风格校验 / 模型不可用报错 / 结果不写库
  B 定价建议：**模型建议低于成本必须被修正**（硬边界）/ 区间倒挂纠正 /
              输出不可解析时回落规则估算 / 同类目价位只用自己的数据

运行（无需 MySQL、无网络、**不调用模型**）：
    python backend/tests/test_goods_ai.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.goods import ai as goods_ai  # noqa: E402
from app.goods.service import GoodsError  # noqa: E402

S = "test-seller"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


GOODS = {
    "id": 1, "goods_no": "G-0001", "title": "便携榨汁杯 350ml", "category": "厨房小电",
    "price": 99.0, "cost_price": 38.0, "stock": 3, "stock_alert": 10, "status": "on_shelf",
    "description": "一键启动 · 40 秒出汁", "media": [{"id": 1}],
}
PEERS = [
    {**GOODS, "id": 2, "goods_no": "G-0002", "title": "同类A", "price": 85.0},
    {**GOODS, "id": 3, "goods_no": "G-0003", "title": "同类B", "price": 99.0},
]
OTHER = {**GOODS, "id": 4, "goods_no": "G-0004", "title": "别类", "category": "数码", "price": 30.0}


class FakeRepo:
    ACTOR_AI = "ai"

    def __init__(self) -> None:
        self.logs: list[dict] = []

    async def get_goods(self, goods_id, seller_id):
        return dict(GOODS) if goods_id == 1 else None

    async def list_goods(self, seller_id, **kw):
        return [dict(GOODS), *(dict(g) for g in PEERS), dict(OTHER)]

    async def insert_operation(self, **kw):
        self.logs.append(kw)
        return len(self.logs)


def fake_model(text):
    """返回一个假的 `_invoke_text`：不碰网络、不产生计费。"""
    async def _call(prompt: str):
        _call.prompt = prompt
        return text
    _call.prompt = ""
    return _call


async def main() -> int:
    repo = FakeRepo()
    goods_ai.repo = repo
    original_invoke = goods_ai._invoke_text
    original_available = goods_ai.text_available
    goods_ai.text_available = lambda: True

    try:
        # ---------- A 文案生成 ----------
        print("【A 组】文案生成")
        goods_ai._invoke_text = fake_model('  "一杯鲜果汁，出门也能喝。40 秒出汁 · USB 充电。"  ')
        out = await goods_ai.generate_copy(S, 1, "口语")
        check("A1 生成成功且去除引号空白",
              out["copy"] == "一杯鲜果汁，出门也能喝。40 秒出汁 · USB 充电。", out["copy"])
        check("A2 回传风格与模型名", out["style"] == "口语" and out["model"], str(out)[:80])
        check("A3 生成动作已留痕（AI 可见）",
              any(l["action"] == "ai_generate_copy" for l in repo.logs), str(repo.logs[:1]))
        check("A4 prompt 含广告法约束", "绝对化用语" in goods_ai._invoke_text.prompt)
        check("A5 prompt 含商品事实", "便携榨汁杯" in goods_ai._invoke_text.prompt)

        try:
            await goods_ai.generate_copy(S, 1, "喊麦")
            check("A6 非法风格被拒", False, "竟然通过")
        except GoodsError as exc:
            check("A6 非法风格被拒", exc.code == "invalid_field", exc.code)

        try:
            await goods_ai.generate_copy(S, 999)
            check("A7 商品不存在 → 404", False, "竟然通过")
        except GoodsError as exc:
            check("A7 商品不存在 → 404", exc.status_code == 404, str(exc.status_code))

        goods_ai._invoke_text = fake_model(None)          # 模型返回空
        try:
            await goods_ai.generate_copy(S, 1)
            check("A8 模型返回空 → 明确报错（不静默）", False, "竟然通过")
        except GoodsError as exc:
            check("A8 模型返回空 → 明确报错（不静默）", exc.status_code == 503, str(exc.status_code))

        goods_ai.text_available = lambda: False
        try:
            await goods_ai.generate_copy(S, 1)
            check("A9 模型未启用 → 明确报错", False, "竟然通过")
        except GoodsError as exc:
            check("A9 模型未启用 → 明确报错", exc.code == "model_unavailable", exc.code)
        goods_ai.text_available = lambda: True

        # ---------- B 定价建议 ----------
        print("\n【B 组】定价建议")
        goods_ai._invoke_text = fake_model(
            '```json\n{"min": 95, "max": 109, "suggested": 99, "reason": "同类均价 ¥92，成本 ¥38。"}\n```'
        )
        price = await goods_ai.suggest_price(S, 1)
        check("B1 正常建议解析（含代码块容错）",
              price["suggested"] == 99 and price["min"] == 95 and price["max"] == 109
              and price["source"] == "model", str(price)[:120])
        check("B2 同类目均价只算自己的商品（排除别类）",
              price["peer_avg"] == 92.0 and price["peer_count"] == 2,
              f"avg={price['peer_avg']} count={price['peer_count']}")
        check("B3 毛利百分比", price["margin_percent"] == 61.6, str(price["margin_percent"]))

        # **硬边界**：模型建议价低于成本 → 必须被修正
        goods_ai._invoke_text = fake_model('{"min": 10, "max": 20, "suggested": 15, "reason": "低价引流"}')
        low = await goods_ai.suggest_price(S, 1)
        check("B4 低于成本的建议被修正（硬边界）",
              low["suggested"] >= 38.0 and low["adjusted"] is True and "低于成本" in low["reason"],
              str(low)[:140])
        check("B5 修正后区间下限也被抬高", low["min"] >= 38.0, str(low["min"]))

        # 区间倒挂 → 纠正
        goods_ai._invoke_text = fake_model('{"min": 200, "max": 100, "suggested": 150, "reason": "x"}')
        swapped = await goods_ai.suggest_price(S, 1)
        check("B6 区间倒挂被纠正", swapped["min"] <= swapped["suggested"] <= swapped["max"], str(swapped)[:100])

        # 输出不可解析 → 回落规则估算
        goods_ai._invoke_text = fake_model("我觉得卖 99 元比较合适。")
        fallback = await goods_ai.suggest_price(S, 1)
        check("B7 不可解析 → 回落规则估算并标明来源",
              fallback["source"] == "rule_fallback" and "未使用模型" in fallback["reason"],
              str(fallback)[:120])
        check("B8 规则估算不低于成本", fallback["suggested"] >= 38.0, str(fallback["suggested"]))

        # 字段缺失 → 回落
        goods_ai._invoke_text = fake_model('{"reason": "只有理由没有数字"}')
        missing = await goods_ai.suggest_price(S, 1)
        check("B9 关键字段缺失 → 回落规则估算", missing["source"] == "rule_fallback", str(missing)[:80])

        check("B10 定价动作已留痕",
              any(l["action"] == "ai_suggest_price" for l in repo.logs))
    finally:
        goods_ai._invoke_text = original_invoke
        goods_ai.text_available = original_available

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
