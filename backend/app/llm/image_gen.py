"""通义万相文生图（DashScope，**异步任务**）。

调用形态是两段式：`POST .../text2image/image-synthesis`（带 `X-DashScope-Async: enable`）
拿到 `task_id`，再轮询 `GET /api/v1/tasks/{task_id}` 直到 `SUCCEEDED` ——
因为一张图要 15～60 秒，同步请求会超时。

⚠️ 三条**官方文档写明、踩了会很惨**的注意点：

1. **返回的图片 URL 约 5 分钟过期** —— 绝不能把它直接存进商品表，
   必须**立刻下载转存**到自己的对象存储（`ObjectStorage` 端口）。否则明天打开就是死链。
2. `size` 格式是 `1024*1024`（**星号**）；写成小写 `x` 会报 `InvalidParameter`。
3. 该端点**只接受图像生成模型名**；填文本模型名会返回 `url error`
   （与阶段二踩的 `qwen3.7-plus` 端点不匹配是同一类问题）。

参考：https://help.aliyun.com/zh/model-studio/text-to-image-v2-api-reference
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks"


class ImageGenError(Exception):
    """图像生成失败（配置缺失 / 任务失败 / 超时）。API 层据此报明确原因。"""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def available() -> bool:
    """图像生成是否可用：模型总开关 + 密钥 + **已配置图像模型名**。

    ⚠️ `IMAGE_MODEL` 留空即视为未开通 —— 宁可明确报错，也不假装能生成。
    """
    return bool(settings.llm_enabled and settings.dashscope_api_key and settings.image_model)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",     # 不带这个头会走同步、必然超时
    }


async def _notify(callback: Any, pct: int, stage: str) -> None:
    """安全地报告进度：进度上报失败**绝不能**影响生成本身。"""
    if callback is None:
        return
    try:
        await callback(pct, stage)
    except Exception as exc:  # noqa: BLE001
        logger.debug("上报生图进度失败（忽略）：%s", exc)


async def text_to_image(
    prompt: str, *, size: str = "", timeout: float = 0.0, on_progress: Any = None
) -> str:
    """提交生成任务并轮询到完成，返回**临时图片 URL**。

    `on_progress(pct, stage)` 可选：用于把进度写进任务中心（30→80 之间按轮询次数推进）。

    ⚠️ 调用方必须**立刻下载转存**（URL 约 5 分钟过期）。
    """
    text = (prompt or "").strip()
    if not text:
        raise ImageGenError("生图描述为空", 400)
    if not settings.dashscope_api_key:
        raise ImageGenError("未配置 DASHSCOPE_API_KEY，无法调用图像模型", 503)
    if not settings.image_model:
        raise ImageGenError(
            "未配置 IMAGE_MODEL（图像生成模型名，如 wanx2.1-t2i-turbo）："
            "请在阿里云百炼开通图像模型后填入 .env", 503
        )

    payload: dict[str, Any] = {
        "model": settings.image_model,
        "input": {"prompt": text},
        "parameters": {"n": 1, "size": size or settings.image_size},
    }
    total_timeout = timeout or float(settings.image_gen_timeout)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(_SUBMIT_URL, json=payload, headers=_headers())
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as exc:
            # 把服务端的 code/message 透出来 —— 官方这两处提示（url error 等）是定位关键
            raise ImageGenError(f"提交生图任务失败：{_explain(exc)}", 502) from exc
        except Exception as exc:  # noqa: BLE001
            raise ImageGenError(f"提交生图任务失败：{type(exc).__name__}: {exc}", 502) from exc

        task_id = str((body.get("output") or {}).get("task_id") or "")
        if not task_id:
            raise ImageGenError(f"提交生图任务未返回 task_id：{str(body)[:200]}", 502)
        await _notify(on_progress, 30, "生成中")

        deadline = time.monotonic() + total_timeout
        polls = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(float(settings.image_poll_interval))
            polls += 1
            # 粗粒度进度：30 → 80，按轮询次数递进（生图耗时不可预知，只能给个"在动"的信号）
            await _notify(on_progress, min(80, 30 + polls * 4), "生成中")
            try:
                poll = await client.get(
                    f"{_TASK_URL}/{task_id}",
                    headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                )
                poll.raise_for_status()
                out = poll.json().get("output") or {}
            except Exception as exc:  # noqa: BLE001 —— 单次轮询失败不放弃，继续重试到超时
                logger.warning("轮询生图任务失败（继续重试）：%s: %s", type(exc).__name__, exc)
                continue

            status = str(out.get("task_status") or "")
            if status == "SUCCEEDED":
                results = out.get("results") or []
                url = str((results[0] or {}).get("url") or "") if results else ""
                if not url:
                    raise ImageGenError("任务成功但未返回图片地址", 502)
                return url
            if status == "FAILED":
                raise ImageGenError(
                    f"生图失败：{out.get('code') or ''} {out.get('message') or ''}".strip(), 502
                )

        raise ImageGenError(f"生图超时（等待 {total_timeout:.0f}s 仍未完成）", 504)


async def download(url: str, *, timeout: float = 30.0) -> bytes:
    """从临时 URL 下载图片字节。**必须紧接着 `text_to_image` 调用**（URL 会过期）。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
    if not data:
        raise ImageGenError("下载到的图片为空", 502)
    return data


def _explain(exc: httpx.HTTPStatusError) -> str:
    """把 DashScope 的错误体转成人话 —— 它的 message 往往直接点明原因。"""
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001
        return f"HTTP {exc.response.status_code}"
    code = body.get("code") or ""
    message = body.get("message") or ""
    return f"{exc.response.status_code} {code} {message}".strip()
