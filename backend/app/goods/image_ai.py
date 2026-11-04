"""商品宣传图的 AI 生成 —— 文生图 + **立刻转存**，并且**异步执行**。

四步，每步都有不可省略的理由：

1. **文本模型写生图提示词** —— 把商品信息（标题/类目/卖点）转成图像 prompt，比让卖家手写省事。
   文本模型不可用时**回落模板** —— prompt 差一点，也比整条链失败强。
2. **图像模型生成**（通义万相，异步任务）。
3. **立刻下载并转存**到自己的对象存储 —— 官方返回的图片 URL **约 5 分钟就过期**，
   把 URL 直接存进商品表，等于存了一条明天的死链。
4. **登记为商品图片** —— 复用 `service.attach_media`（服务端生成 key、首张自动主图、正常留痕）。

⚠️ **为什么整条链走后台任务**：这一步要 30—60 秒。同步执行时，一旦关掉浮窗或刷新页面，
请求被浏览器取消，**生成结果就丢了**。现在投递成任务，进度写进任务中心，
**关掉界面也在跑、回来还能看**。

⚠️ 全链路**真实计费**（1 张图 + 1 次文本调用）；`IMAGE_MODEL` 未配置时**明确报错**，不假装能生成。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.db import goods_repo as repo
from app.goods import service
from app.goods.ai import invoke_text
from app.goods.service import GoodsError
from app.llm import image_gen
from app.tasks import runner as task_runner

logger = logging.getLogger(__name__)

#: 所有生图提示词都追加的兜底要求
_BG_HINT = "简洁干净的浅色背景，商品居中，专业电商商品摄影，柔和光线，画面中不要出现文字、水印或 logo"


def _fallback_prompt(goods: dict[str, Any], hint: str) -> str:
    """文本模型不可用时的模板提示词（不阻断生图）。"""
    parts = [str(goods.get("title") or "商品")]
    if goods.get("category"):
        parts.append(str(goods["category"]))
    if goods.get("description"):
        parts.append(str(goods["description"])[:60])
    if hint:
        parts.append(str(hint))
    return "，".join(parts) + "。" + _BG_HINT


def _prompt_instruction(goods: dict[str, Any], hint: str) -> str:
    return (
        "你是电商视觉设计助手。请为下面这件商品写一段**中文生图提示词**，用于生成商品宣传图。\n\n"
        f"商品：{goods.get('title')}\n"
        f"类目：{goods.get('category') or '（未填）'}\n"
        f"卖点：{(goods.get('description') or '（暂无）')[:120]}\n"
        + (f"卖家补充要求：{hint}\n" if hint else "")
        + "\n要求：\n"
        "1. 只输出**提示词本身**，60—120 字；不要解释、不要引号、不要换行罗列\n"
        "2. 描述画面主体、材质、场景、光线与构图\n"
        "3. 明确写出「不要出现文字、水印、logo」——商品图上叠字几乎总是败笔\n"
        "4. **不得编造**商品信息里没有的参数（认证、材质型号、尺寸等）"
    )


async def _cover_body(
    progress: Any, seller_id: str, goods_id: int, hint: str = ""
) -> dict[str, Any]:
    """**任务体**：真正跑生成流程（带进度回调，供任务中心展示）。"""
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    # ---- 1) 文本模型写提示词（失败回落模板，不阻断生图）----
    await progress(5, "准备提示词")
    prompt = ""
    try:
        prompt = (await invoke_text(_prompt_instruction(goods, hint)) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成生图提示词失败（回落模板）：%s: %s", type(exc).__name__, exc)
    if not prompt:
        prompt = _fallback_prompt(goods, hint)

    # ---- 2) 生成，并**立刻**下载转存（临时 URL 约 5 分钟过期）----
    temp_url = await image_gen.text_to_image(prompt, on_progress=progress)
    await progress(85, "下载转存")
    data = await image_gen.download(temp_url)

    # ---- 3) 登记（复用 attach_media：服务端生成 key、首张自动主图、正常留痕）----
    await progress(92, "登记到商品")
    try:
        saved = await service.attach_media(
            seller_id, goods_id,
            filename="ai-cover.png", data=data, content_type="image/png",
            actor=repo.ACTOR_AI,
        )
    except GoodsError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 图已生成却存不下，必须明确报错而不是静默丢弃
        raise GoodsError(
            "persist_failed", f"图片已生成但保存失败：{type(exc).__name__}: {exc}", 503
        ) from exc

    await repo.insert_operation(
        actor=repo.ACTOR_AI,
        action="ai_generate_cover",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=f"AI 生成商品宣传图（{settings.image_model}）",
        after={"url": saved["url"], "prompt": prompt[:200]},
        undoable=False,
        seller_id=seller_id,
    )
    return {
        "summary": f"已为「{goods['title']}」生成宣传图并加入商品",
        "goods_no": goods.get("goods_no"),
        "prompt": prompt,
        "url": saved["url"],
        "media_id": saved["media_id"],
        "model": settings.image_model,
        "size": settings.image_size,
    }


async def generate_cover(seller_id: str, goods_id: int, *, hint: str = "") -> dict[str, Any]:
    """**投递后台任务**并立即返回任务行（不再同步阻塞 30—60 秒）。

    ⚠️ 前置校验（商品存在 / 模型已开通）放在**投递之前**：这类错误要立刻返回，
    而不是等一个后台任务跑失败、再让用户去任务中心翻原因。
    """
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)
    if not image_gen.available():
        raise GoodsError(
            "image_unavailable",
            "图像生成未启用：需先在阿里云百炼**开通图像模型**，"
            "再把型号填入 .env 的 IMAGE_MODEL（如 wanx2.1-t2i-plus）",
            503,
        )

    return await task_runner.submit(
        "ai_cover",
        f"为「{goods.get('title')}」生成宣传图",
        _cover_body,
        target=str(goods.get("goods_no") or goods_id),
        seller_id=seller_id,
        # payload：失败后"重试"靠它重建执行体（handler 是函数引用，存不下来）
        payload={"goods_id": goods_id, "hint": hint or ""},
        goods_id=goods_id,
        hint=hint or "",
    )
