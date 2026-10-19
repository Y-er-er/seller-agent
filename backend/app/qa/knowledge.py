"""口径知识库：加载 + 关键词检索。

为什么不做向量检索（RAG）：
- 本知识库条目量级很小（十几条），**关键词计分**已足够准确，且**完全确定性**；
- 向量检索需要额外依赖与**计费**的 embedding 调用，与本项目纪律 8（依赖最适配）不符；
- 方案 §12 已明确：检索结果**不得**驱动资格判定，只用于解释释疑。
如后续条目规模显著增长，再评估引入向量检索。

加载期做严格校验（与 `app/rules/loader.py` 同思路）：字段缺失、ID 重复等
一律在**加载期**抛 `ValueError`，避免写错关键词导致条目静默失效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import PROJECT_ROOT

_KNOWLEDGE_FILE = PROJECT_ROOT / "config" / "qa_knowledge.yaml"

# 归一化时要去掉的标点与空白（中文标点一并处理，否则"金额闸门？"匹配不到"金额闸门"）
_PUNCT = re.compile(r"[\s\u3000,，。.、;；:：!！?？'\"“”‘’()（）\[\]【】<>《》/\\|~`@#$%^&*\-_+=]+")


@dataclass(frozen=True)
class KnowledgeEntry:
    """一条标准口径。"""

    id: str
    title: str
    keywords: tuple[str, ...]
    answer: str
    related_rules: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeBase:
    """整份口径知识库。"""

    version: str
    entries: tuple[KnowledgeEntry, ...]


def _normalize(text: str) -> str:
    """归一化：转小写 + 去标点空白，便于中文子串匹配。"""
    return _PUNCT.sub("", (text or "").strip().lower())


def _require(entry: dict[str, Any], key: str, index: int) -> Any:
    if key not in entry or entry[key] in (None, "", []):
        raise ValueError(f"[qa_knowledge] entries[{index}] 缺少必填项：{key}")
    return entry[key]


def load_knowledge(path: Path | None = None) -> KnowledgeBase:
    """加载口径知识库并做加载期校验。"""
    target = path or _KNOWLEDGE_FILE
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("[qa_knowledge] 顶层必须是映射（version / entries）")

    items = raw.get("entries")
    if not isinstance(items, list) or not items:
        raise ValueError("[qa_knowledge] entries 必须是非空列表")

    entries: list[KnowledgeEntry] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"[qa_knowledge] entries[{index}] 必须是映射")

        entry_id = str(_require(item, "id", index))
        if entry_id in seen_ids:
            raise ValueError(f"[qa_knowledge] 条目 ID 重复：{entry_id}")
        seen_ids.add(entry_id)

        keywords = tuple(str(k).strip() for k in _require(item, "keywords", index) if str(k).strip())
        if not keywords:
            raise ValueError(f"[qa_knowledge] {entry_id} 的 keywords 全为空")

        entries.append(
            KnowledgeEntry(
                id=entry_id,
                title=str(_require(item, "title", index)).strip(),
                keywords=keywords,
                answer=str(_require(item, "answer", index)).strip(),
                related_rules=tuple(str(r).strip() for r in (item.get("related_rules") or [])),
            )
        )

    return KnowledgeBase(version=str(raw.get("version") or ""), entries=tuple(entries))


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    """进程内只加载一次（与规则表同样处理）。"""
    return load_knowledge(_KNOWLEDGE_FILE)


def search(question: str, *, limit: int = 3) -> list[tuple[KnowledgeEntry, int]]:
    """按关键词命中计分检索口径条目，返回 [(条目, 得分)]（得分高者在前）。

    计分规则：命中的关键词**越长，权重越高**——"金额闸门"比"金额"更能定位到具体条目。
    标题命中额外加权，因为标题是条目的主旨。
    得分为 0 的条目不返回（宁可不答，也不给出不相干的口径）。
    """
    normalized = _normalize(question)
    if not normalized:
        return []

    scored: list[tuple[KnowledgeEntry, int]] = []
    for entry in get_knowledge_base().entries:
        score = 0
        for keyword in entry.keywords:
            key = _normalize(keyword)
            if key and key in normalized:
                score += len(key)
        title_norm = _normalize(entry.title)
        if title_norm and title_norm in normalized:
            score += len(title_norm) * 2
        if score > 0:
            scored.append((entry, score))

    # 得分降序；同分时按 ID 稳定排序，保证结果可复现
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored[:limit]


def list_entries() -> list[dict[str, Any]]:
    """列出全部条目（供工作台展示"可以问什么"）。"""
    return [
        {
            "id": entry.id,
            "title": entry.title,
            "keywords": list(entry.keywords),
            "related_rules": list(entry.related_rules),
        }
        for entry in get_knowledge_base().entries
    ]
