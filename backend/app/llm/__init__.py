"""模型层（可选，可全局关闭）。

对外只暴露三个入口：
- `text_available` / `get_text_llm` —— 文本模型（口径问答的可选润色）；
- `vision_available` —— 视觉闸门判断；
- `extract_evidence_signals` —— 举证图片信号抽取（**计费**，受闸门约束）。

`LLM_ENABLED=false` 时以上全部安全返回 None/False，调用方必须走确定性降级路径。
"""

from app.llm.client import (
    VisionSignals,
    extract_evidence_signals,
    get_text_llm,
    text_available,
    vision_available,
)

__all__ = [
    "VisionSignals",
    "extract_evidence_signals",
    "get_text_llm",
    "text_available",
    "vision_available",
]
