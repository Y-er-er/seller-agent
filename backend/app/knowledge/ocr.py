"""图片 OCR —— 让截图与扫描件也能进知识库。

为什么不装 OCR 引擎（tesseract / paddleocr）：

1. **要系统级依赖**（tesseract 还得装二进制），而本项目对"新增依赖"有明确约束；
2. **本来就有能读图的通路**：`app/llm/client.py` 的 `_invoke_vision` 走 DashScope
   多模态接口，图片以 base64 data URI 直接传即可 —— **零新增依赖、零新密钥**。

代价（必须说清）：视觉模型调用**计费**，与本项目的举证图片识别同一性质。
本层如实返回 token 用量，由调用方计量；开关关闭时**明确报错**，不静默返回空文本
（否则会被误判成"图片里没字"，卖家会以为资料白传了）。
"""

from __future__ import annotations

import asyncio
import base64
import logging

from app.config import settings
from app.llm.client import _invoke_vision, vision_available

logger = logging.getLogger(__name__)

#: 单张图片原始字节上限。base64 会膨胀约 1.33 倍，再大请求会被服务端拒，
#: 与其让用户等一个看不懂的 400，不如在本地就拦下来并告诉他怎么处理。
MAX_IMAGE_BYTES = 7 * 1024 * 1024

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: OCR 提示词：**只转写，不许发挥**。
#: 这一条不能省：模型很容易把"表格里模糊的数字"脑补成合理值，
#: 而知识库里的数字是要被当真执行的（比如"退货 15 天"）。
_OCR_PROMPT = (
    "请把这张图片里的**全部文字**原样转写出来，用于建立可检索的知识库。\n"
    "要求：\n"
    "1. 只转写，**不要总结、不要润色、不要补充图片里没有的内容**；\n"
    "2. 保留原来的段落与换行；表格按「列1 | 列2」逐行输出；\n"
    "3. 看不清的字用「□」代替，**不要猜测**；\n"
    "4. 如果图片里确实没有文字，只输出「（无文字）」。"
)


class OcrError(RuntimeError):
    """OCR 失败（未启用 / 图片过大 / 模型无返回）。调用方转 400 或降级提示。"""


def _data_uri(filename: str, data: bytes) -> str:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = _MIME.get(suffix, "image/png")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


async def image_to_text(
    filename: str, data: bytes, *, timeout: float | None = None
) -> tuple[str, int]:
    """把图片转成文字，返回 `(文本, token 用量)`。

    ⚠️ 触发**计费**调用。未启用视觉模型时立刻抛 `OcrError`（不发请求）。
    """
    if not vision_available():
        raise OcrError(
            "图片识别需要视觉模型：当前 LLM_ENABLED 关闭 / 未配置 DASHSCOPE_API_KEY / "
            "VISION_MAX_IMAGES=0。请开启后重启，或把内容整理成文本再上传。"
        )
    if not data:
        raise OcrError(f"图片「{filename}」是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise OcrError(
            f"图片太大（{len(data) // 1024} KB），上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB。"
            "请压缩或截取需要识别的那部分再上传。"
        )

    ref = _data_uri(filename, data)
    # `_invoke_vision` 是同步调用（SDK 原生），放线程池里跑，避免阻塞事件循环
    text, usage = await asyncio.to_thread(
        _invoke_vision, [ref], _OCR_PROMPT,
        timeout=float(timeout or settings.vision_timeout * 3),
    )
    if not text or not text.strip():
        raise OcrError("图片识别没有返回内容（可能是图片过小、过模糊或格式不受支持）")

    cleaned = text.strip()
    if cleaned in ("（无文字）", "(无文字)"):
        raise OcrError("这张图片里没有识别到文字，无法入库")

    tokens = int((usage or {}).get("total_tokens") or 0)
    logger.info("OCR 完成：%s 识别 %d 字 tokens=%d", filename, len(cleaned), tokens)
    return cleaned, tokens
