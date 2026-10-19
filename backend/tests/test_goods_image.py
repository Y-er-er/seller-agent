"""商品宣传图生成测试 —— **假图像客户端 + 假服务，零计费**。

覆盖重点：
  A 前置校验：商品不存在 → 404；IMAGE_MODEL 未配置 → 503（**明确报错，不假装能生成**）
  B 提示词：文本模型可用时用它写的；不可用时**回落模板**（不阻断生图）
  C 成功路径：生成 → **下载转存** → 登记为商品图片 → 留痕
  D 失败路径：生图失败 / 下载失败 → **明确报错**（绝不静默当作成功）

运行（无需 MySQL、无网络、**不调用任何模型**）：
    python backend/tests/test_goods_image.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.goods import image_ai  # noqa: E402
from app.goods.service import GoodsError  # noqa: E402
from app.llm import image_gen  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


GOODS = {
    "id": 6, "goods_no": "G-0001", "title": "便携榨汁杯 350ml",
    "category": "厨房小电", "price": 99.0, "description": "一键启动 · 40 秒出汁",
}


class FakeGoodsRepo:
    ACTOR_AI = "ai"

    def __init__(self) -> None:
        self.logs: list[dict] = []

    async def get_goods(self, goods_id, seller_id):
        return dict(GOODS) if goods_id == 6 else None

    async def insert_operation(self, **kw):
        self.logs.append(kw)
        return 1


class FakeService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def attach_media(self, seller_id, goods_id, **kw):
        self.calls.append({"seller_id": seller_id, "goods_id": goods_id, **kw})
        return {"media_id": 11, "url": "/api/seller/uploads/ai/x.png", "object_key": "ai/x.png"}


class FakeImageGen:
    """假图像客户端：可开关可用性、可注入失败。"""

    def __init__(self, *, ready=True, gen_fail="", dl_fail="") -> None:
        self.ready, self.gen_fail, self.dl_fail = ready, gen_fail, dl_fail
        self.prompts: list[str] = []
        self.downloaded: list[str] = []

    def available(self):
        return self.ready

    async def text_to_image(self, prompt, **kw):
        if self.gen_fail:
            raise image_gen.ImageGenError(self.gen_fail)
        self.prompts.append(prompt)
        return "https://dashscope-tmp/cover.png"

    async def download(self, url, **kw):
        if self.dl_fail:
            raise image_gen.ImageGenError(self.dl_fail)
        self.downloaded.append(url)
        return b"\x89PNG\r\n\x1a\n" + b"0" * 128

    # 供 `except image_gen.ImageGenError` 使用
    ImageGenError = image_gen.ImageGenError


def fake_text(text):
    async def _call(prompt):
        _call.prompt = prompt
        return text
    _call.prompt = ""
    return _call


async def main() -> int:
    fake_repo = FakeGoodsRepo()
    fake_service = FakeService()
    original = {
        "repo": image_ai.repo, "service": image_ai.service,
        "image_gen": image_ai.image_gen, "invoke_text": image_ai.invoke_text,
    }
    image_ai.repo = fake_repo
    image_ai.service = fake_service

    try:
        # ---------- A 前置校验 ----------
        print("【A 组】前置校验")
        image_ai.image_gen = FakeImageGen(ready=True)
        image_ai.invoke_text = fake_text("一只杯子")

        try:
            await image_ai.generate_cover("s", 999)
            check("A1 商品不存在 → 404", False, "竟然通过")
        except GoodsError as exc:
            check("A1 商品不存在 → 404", exc.status_code == 404, str(exc.status_code))

        image_ai.image_gen = FakeImageGen(ready=False)          # IMAGE_MODEL 未配置
        try:
            await image_ai.generate_cover("s", 6)
            check("A2 未开通图像模型 → 503 且提示去哪配置", False, "竟然通过")
        except GoodsError as exc:
            check("A2 未开通图像模型 → 503 且提示去哪配置",
                  exc.status_code == 503 and "IMAGE_MODEL" in exc.message, exc.message)

        # ---------- B 提示词 ----------
        print("\n【B 组】提示词")
        gen = FakeImageGen(ready=True)
        image_ai.image_gen = gen
        image_ai.invoke_text = fake_text("一杯鲜榨果汁，木质桌面，柔和自然光")
        out = await image_ai.generate_cover("s", 6)
        check("B1 用文本模型写出的提示词",
              gen.prompts == ["一杯鲜榨果汁，木质桌面，柔和自然光"], str(gen.prompts))
        check("B2 提示词回传给调用方", out["prompt"] == "一杯鲜榨果汁，木质桌面，柔和自然光")

        async def boom(prompt):
            raise RuntimeError("模型挂了")
        image_ai.invoke_text = boom
        gen2 = FakeImageGen(ready=True)
        image_ai.image_gen = gen2
        out2 = await image_ai.generate_cover("s", 6)
        check("B3 文本模型不可用 → 回落模板且**不阻断生图**",
              len(gen2.prompts) == 1 and "便携榨汁杯" in gen2.prompts[0], str(gen2.prompts)[:120])
        check("B4 模板提示词含「不要出现文字」的兜底要求",
              "不要出现文字" in gen2.prompts[0], gen2.prompts[0][:120])

        # ---------- C 成功路径 ----------
        print("\n【C 组】成功路径")
        image_ai.invoke_text = fake_text("提示词")
        gen3 = FakeImageGen(ready=True)
        image_ai.image_gen = gen3
        fake_service.calls.clear()
        out3 = await image_ai.generate_cover("s", 6, hint="要蓝色调")
        check("C1 生成了图", gen3.prompts and out3["url"].startswith("/api/seller/uploads/"))
        check("C2 **下载转存**（不是直接用临时 URL）",
              gen3.downloaded == ["https://dashscope-tmp/cover.png"], str(gen3.downloaded))
        check("C3 登记为商品图片（复用 attach_media）",
              len(fake_service.calls) == 1
              and fake_service.calls[0]["goods_id"] == 6
              and fake_service.calls[0]["content_type"] == "image/png",
              str(fake_service.calls)[:160])
        check("C4 登记时 actor=ai（AI 的动作同等留痕）",
              fake_service.calls[0]["actor"] == "ai", str(fake_service.calls[0].get("actor")))
        check("C5 另记一条操作日志",
              any(l["action"] == "ai_generate_cover" for l in fake_repo.logs))

        # ---------- D 失败路径 ----------
        print("\n【D 组】失败路径")
        image_ai.image_gen = FakeImageGen(ready=True, gen_fail="生图失败：InvalidParameter size 格式错误")
        try:
            await image_ai.generate_cover("s", 6)
            check("D1 生图失败 → 明确报错", False, "竟然通过")
        except GoodsError as exc:
            check("D1 生图失败 → 明确报错（含服务端原因）",
                  exc.code == "image_gen_failed" and "size" in exc.message, exc.message)

        image_ai.image_gen = FakeImageGen(ready=True, dl_fail="下载超时")
        try:
            await image_ai.generate_cover("s", 6)
            check("D2 下载失败 → 明确报错（不静默）", False, "竟然通过")
        except GoodsError as exc:
            check("D2 下载失败 → 明确报错（不静默）",
                  exc.code == "image_gen_failed", exc.code)
    finally:
        image_ai.repo = original["repo"]
        image_ai.service = original["service"]
        image_ai.image_gen = original["image_gen"]
        image_ai.invoke_text = original["invoke_text"]

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
