"""模型客户端工厂 —— 本方案的"皮"。

纪律 9：模型名称、密钥全部来自 `.env`，本文件不出现任何硬编码取值。
设计约束（方案 §8）：
- 模型**不参与任何阈值判定**；本模块只被 evidence / dispose / qa 三个节点调用。
- `LLM_ENABLED=false` 时所有函数安全返回 None/False，调用方必须降级为规则或模板路径。
- ⚠️ 本模块所有真实调用都会**产生计费**；`LLM_ENABLED=false` 时不发生任何调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def text_available() -> bool:
    """文本模型是否可用（开关打开且已配置密钥）。"""
    return settings.llm_enabled and bool(settings.dashscope_api_key)


def vision_available() -> bool:
    """视觉模型是否可用（开关打开、已配置密钥，且未把图片张数上限设为 0）。"""
    return settings.llm_enabled and bool(settings.dashscope_api_key) and settings.vision_max_images > 0


def get_text_llm():
    """获取文本模型客户端（千问）。

    返回 None 表示不可用——调用方必须自行降级，**不得**假设返回值非空。
    温度固定 0：抽取类任务要求最大稳定性（方案 §8 约束 4）。
    """
    if not text_available():
        return None

    # 延迟导入：未启用模型时不加载重依赖
    from langchain_community.chat_models import ChatTongyi

    return ChatTongyi(
        model=settings.llm_text_model,
        dashscope_api_key=settings.dashscope_api_key,
        temperature=0,
    )


# ============================================================
# 视觉：举证图片信号抽取
# ============================================================

# 提示词的两条硬约束，直接对应方案 §11 的边界声明：
#   ① 只做**客观事实抽取**，不做质量判定 —— 判定权始终在规则引擎；
#   ② 强制 JSON 输出，便于解析且避免模型自由发挥影响链路稳定性。
_EVIDENCE_PROMPT = (
    "你是售后举证图片的**信息抽取器**。\n"
    "严格约束：\n"
    "1. 只做客观描述与信息抽取，**不得**给出通过/驳回建议，**不得**判断责任归属；\n"
    "2. 只输出一个 JSON 对象，不要任何解释文字、不要代码块标记。\n"
    "JSON 字段：\n"
    '{"has_quality_claim": true|false, "claim_type": "quality|damage|wrong_item|not_received|other|null", '
    '"visible_defect": true|false, "confidence": 0.0~1.0, "summary": "不超过30字的客观描述"}\n'
    "字段含义：has_quality_claim=图片是否呈现质量问题迹象（含破损/污渍/变形等）；"
    "claim_type=图片主要支持的诉求类型，无法判断填 null；confidence=你对上述判断的把握；"
    "summary=客观描述，不要主观评价。"
)

# 允许的 claim_type 取值（与 RefundReason 的语义对齐但不强绑定）
_CLAIM_TYPES = {"quality", "damage", "wrong_item", "not_received", "other"}


@dataclass(frozen=True)
class VisionSignals:
    """视觉模型输出的**辅助信号**（不参与任何阈值判定）。"""

    has_quality_claim: bool = False
    claim_type: str | None = None
    visible_defect: bool = False
    confidence: float = 0.0
    summary: str = ""
    model: str = ""
    image_count: int = 0
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出中稳健地取出 JSON 对象。

    模型偶尔会包 ```json 代码块或加前后缀说明——不做严谨解析就会整体降级，
    因此这里做一次"取首个 {...} 块"的容错。
    """
    if not text:
        return None
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


#: 公开别名：跨模块复用（商品域 AI 定价建议也要解析模型返回的 JSON）。
#: 保留私有名为兼容既有调用，两者指向同一函数。
parse_json_object = _parse_json_object


def _invoke_vision(image_refs: list[str], prompt: str, *, timeout: float) -> tuple[str | None, dict[str, Any]]:
    """同步调用视觉模型（在**线程池**中执行，避免阻塞事件循环）。

    返回 (模型文本, 用量信息)；任何失败都返回 (None, {}) —— 由调用方降级。
    """
    from dashscope import MultiModalConversation

    content: list[dict[str, str]] = [{"image": ref} for ref in image_refs]
    content.append({"text": prompt})
    response = MultiModalConversation.call(
        model=settings.llm_vision_model,
        messages=[{"role": "user", "content": content}],
        api_key=settings.dashscope_api_key,
        timeout=timeout,
    )
    if getattr(response, "status_code", None) != 200:
        logger.warning(
            "视觉模型返回非 200（%s）：%s", getattr(response, "status_code", "?"),
            getattr(response, "message", ""),
        )
        return None, {}

    try:
        text = response.output.choices[0].message.content[0]["text"]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None, {}

    usage = getattr(response, "usage", None)
    usage_dict: dict[str, Any] = {}
    if usage is not None:
        for key in ("input_tokens", "output_tokens", "total_tokens", "image_count"):
            value = getattr(usage, key, None)
            if value is not None:
                usage_dict[key] = value

    return text, usage_dict


async def extract_evidence_signals(
    image_urls: list[str],
    *,
    buyer_note: str = "",
) -> VisionSignals | None:
    """从举证图片抽取**辅助信号**（质量主张迹象等）。

    **不参与判定**：结果只写入 `EvidenceReport` 与审计，规则引擎完全不知道它的存在。

    ⚠️ 本函数触发**计费**调用；开关未开启时立刻返回 None（不发生调用）。
    三重保护措施：
      1. `vision_available()` 闸门（总开关 + 密钥 + 张数上限）；
      2. 只接受 `http(s)://` 图片，且**张数上限** `VISION_MAX_IMAGES`；
      3. 超时 / 异常 / 非 200 / JSON 解析失败 → 一律返回 None，**绝不影响判定**。
    """
    if not vision_available() or not image_urls:
        return None

    refs = [u for u in image_urls if isinstance(u, str) and u.lower().startswith(("http://", "https://"))]
    refs = refs[: settings.vision_max_images]
    if not refs:
        # 全非 http（如 demo:// 占位 / 本地路径 / COS key）→ 无处可送，直接降级
        return None

    prompt = _EVIDENCE_PROMPT
    if buyer_note and settings.vision_include_buyer_note:
        # 默认不送留言：个人信息出境必须单独评估（方案 §11）
        prompt += f"\n\n【可参考的买家留言】{buyer_note[:200]}"

    started = time.perf_counter()
    try:
        text, usage = await asyncio.to_thread(
            _invoke_vision, refs, prompt, timeout=settings.vision_timeout
        )
    except Exception as exc:  # noqa: BLE001 — 视觉是旁路信号，任何异常都必须降级
        logger.warning("视觉调用失败（已降级，不影响判定）：%s: %s", type(exc).__name__, exc)
        return None

    latency_ms = int((time.perf_counter() - started) * 1000)
    data = _parse_json_object(text or "")
    if data is None:
        logger.warning("视觉输出无法解析为 JSON（已降级）：%r", (text or "")[:120])
        return None

    claim_type = data.get("claim_type")
    if isinstance(claim_type, str):
        claim_type = claim_type.strip().lower()
        if claim_type not in _CLAIM_TYPES:
            claim_type = None
    else:
        claim_type = None

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    return VisionSignals(
        has_quality_claim=bool(data.get("has_quality_claim")),
        claim_type=claim_type,
        visible_defect=bool(data.get("visible_defect")),
        confidence=max(0.0, min(1.0, confidence)),
        summary=str(data.get("summary") or "")[:60],
        model=settings.llm_vision_model,
        image_count=len(refs),
        latency_ms=latency_ms,
        usage=usage,
    )
