"""节点 3：举证核验（evidence）。

分层职责（方案 §11）：
- **确定性部分**：图片数量统计与可解码性校验（Pillow）；
- **可选模型部分**：用视觉模型（`LLM_VISION_MODEL`）抽取"是否含质量主张"等**辅助信号**。
  受 `LLM_ENABLED` 开关约束，开关关闭时不发生任何调用、直接降级。

⚠️ 红线：本节点**只产出事实信号**，不做质量判定、不给通过/驳回结论。
   模型输出**不参与任何阈值判定**——规则引擎完全不知道它的存在。

`source` 取值（供审计区分"模型没用上"的具体原因，避免笼统地写成"没启用"）：
    none         没有任何举证图片
    llm          模型成功抽取
    llm_disabled 闸门关闭（未授权 / 未配置密钥 / 张数上限为 0）
    llm_failed   闸门开启，但调用失败或输出无法解析（已降级）
    rule         有图片但**没有可送模型的图片**（demo 占位、本地路径、COS key 等）
"""

from __future__ import annotations

from pathlib import Path

from app.graph.state import RefundState
from app.llm.client import extract_evidence_signals, vision_available
from app.models import AuditEvent, EvidenceReport

# 能直接送多模态模型的图片形式（本地路径 / COS key 需先换取可访问 URL，本阶段不处理）
_SENDABLE_SCHEMES = ("http://", "https://")


def check_images(paths: list[str]) -> tuple[int, bool, str | None]:
    """校验举证图片：返回 (有效数量, 是否全部可解码, 失败原因)。

    本机存在的文件用 Pillow 校验；非本地路径（如 COS key）在 M0 只计数、不下载校验。
    """
    valid = 0
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            valid += 1  # 远端对象，M0 不做下载校验
            continue
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            valid += 1
        except Exception:  # Pillow 对损坏文件抛多种异常，统一视为不可解码
            return valid, False, f"图片不可解码：{path.name}"
    return valid, True, None


def _has_sendable_image(images: list[str]) -> bool:
    """是否存在可直接送模型的 http(s) 图片。"""
    return any(isinstance(u, str) and u.lower().startswith(_SENDABLE_SCHEMES) for u in images)


async def evidence_node(state: RefundState) -> dict:
    """执行举证核验，产出 EvidenceReport（异步：模型调用需在线程池中执行）。"""
    request = state.get("request")
    images = list(request.claim_images) if request else []
    buyer_note = (request.buyer_note or "") if request else ""

    image_count, decodable, invalid_reason = check_images(images)

    # ---------- 可选：模型辅助信号（计费，受闸门约束）----------
    signals = None
    if images and vision_available():
        signals = await extract_evidence_signals(images, buyer_note=buyer_note)

    if not images:
        source = "none"
    elif signals is not None:
        source = "llm"
    elif not vision_available():
        source = "llm_disabled"
    elif not _has_sendable_image(images):
        source = "rule"
    else:
        source = "llm_failed"

    report = EvidenceReport(
        image_count=image_count,
        decodable=decodable,
        invalid_reason=invalid_reason,
        # ⚠️ 仅辅助信号：不进入规则判定（规则只看 image_count / refund_reason_code）
        has_quality_claim=bool(signals.has_quality_claim) if signals else False,
        claim_type=signals.claim_type if signals else None,
        source=source,
    )

    audit_payload: dict = {
        "image_count": image_count,
        "decodable": decodable,
        "invalid_reason": invalid_reason,
        "source": source,
    }
    if signals is not None:
        # 留痕模型输出的关键信息与成本，便于事后核对"模型看到了什么"
        audit_payload["vision"] = {
            "model": signals.model,
            "image_count": signals.image_count,
            "latency_ms": signals.latency_ms,
            "has_quality_claim": signals.has_quality_claim,
            "claim_type": signals.claim_type,
            "visible_defect": signals.visible_defect,
            "confidence": signals.confidence,
            "summary": signals.summary,
        }

    update: dict = {
        "evidence": report,
        "audit_events": [
            AuditEvent(stage="evidence", message="举证核验完成", payload=audit_payload)
        ],
    }
    if signals is not None:
        # 成本计量（方案 §10）：token 用量汇总到 llm_usage，供审计与成本核算
        update["llm_usage"] = [
            {
                "stage": "evidence",
                "model": signals.model,
                "latency_ms": signals.latency_ms,
                "image_count": signals.image_count,
                **signals.usage,
            }
        ]
    return update
