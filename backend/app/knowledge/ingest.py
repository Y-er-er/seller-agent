"""文档解析与分块 —— 把上传的文件切成可检索的片段。

分块策略移植自 RAG 项目的 `app/core/file_parser.py`（`parse_file_path` /
`_split_with_overlap` / `_split_into_paragraphs`），并做了两处适配：

1. **吃 bytes 而不是文件路径**：本项目不落原始文件（知识库只需要文本），
   上传内容直接解析，少一次磁盘往返、也不用清理临时文件。
2. **返回纯 dict**：不绑 FastAPI 的 `UploadFile`，因此可以离线单测。

为什么要"按句子边界 + 重叠"而不是等长硬切：
- 硬切会把「退款需在签收后 15 天内提出」切成两半，检索命中一半时语义就断了；
- 重叠（默认 100 字）保证跨块的上下文不丢 —— 命中边界句子时仍能读懂前因后果。
"""

from __future__ import annotations

import io
import re
from typing import Any

#: 支持的文件后缀（与前端 accept 保持一致）
SUPPORTED_SUFFIXES = (".txt", ".md", ".markdown", ".pdf", ".docx")
SUPPORTED_LABEL = " / ".join(SUPPORTED_SUFFIXES)

#: 图片后缀：走 OCR 通路（视觉模型），**不由本模块解析** —— 见 `ocr.py`
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
IMAGE_LABEL = " / ".join(IMAGE_SUFFIXES)


class IngestError(ValueError):
    """文件无法解析（格式不支持 / 内容为空 / 损坏）。调用方转成 400。"""


def parse_document(
    filename: str,
    data: bytes,
    *,
    chunk_size: int = 700,
    chunk_overlap: int = 100,
) -> list[dict[str, Any]]:
    """把一份文件解析成分块列表。

    返回每块：`{text, chunk_index, page_number, section_title}` ——
    后三项进 Chroma 的 metadata，检索结果里用来标"出自哪份资料的哪一节"。
    """
    name = (filename or "").strip()
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in IMAGE_SUFFIXES:
        # 图片必须先 OCR 成文字才能分块 —— 这里给明确指引，而不是含糊地说"不支持"
        raise IngestError(
            f"「{name}」是图片，需要先做 OCR 识别。请直接上传图片（会自动识别文字），"
            f"由 OCR 通路处理；纯文本解析只支持 {SUPPORTED_LABEL}"
        )
    if suffix not in SUPPORTED_SUFFIXES:
        raise IngestError(
            f"不支持的文件格式：{name or '(未命名)'}，"
            f"支持 {SUPPORTED_LABEL} 以及图片 {IMAGE_LABEL}"
        )
    if not data:
        raise IngestError(f"文件「{name}」是空的，没有可入库的内容")

    sections = _read_sections(suffix, data, name)

    chunks: list[dict[str, Any]] = []
    for section in sections:
        for text in _split_with_overlap(section["text"], chunk_size, chunk_overlap):
            chunks.append({
                "text": text,
                "chunk_index": len(chunks),
                "page_number": int(section.get("page_number") or 0),
                "section_title": str(section.get("section_title") or ""),
            })
    if not chunks:
        raise IngestError(
            f"文件「{name}」里没有解析出有效文字"
            "（扫描版 PDF 需要先做 OCR：可以把页面导出为图片再上传）"
        )
    return chunks


def split_text(
    text: str, *, chunk_size: int = 700, chunk_overlap: int = 100,
    section_title: str = "",
) -> list[dict[str, Any]]:
    """把**已经是文字**的内容（OCR 结果、粘贴文本）切成可检索的分块。

    OCR 与文本解析共用同一套切分规则 —— 否则同一个知识库里，
    "扫描件"和"Word"的片段粒度会不一样，检索排序就失去可比性。
    """
    chunks: list[dict[str, Any]] = []
    for piece in _split_with_overlap(text or "", chunk_size, chunk_overlap):
        chunks.append({
            "text": piece,
            "chunk_index": len(chunks),
            "page_number": 0,
            "section_title": section_title,
        })
    if not chunks:
        raise IngestError("没有可入库的文字内容（OCR 结果为空？）")
    return chunks


# ============================================================
# 分格式读取（每个分支只负责"把文件变成若干段文本"）
# ============================================================


def _read_sections(suffix: str, data: bytes, filename: str) -> list[dict[str, Any]]:
    if suffix == ".pdf":
        return _read_pdf(data, filename)
    if suffix == ".docx":
        return _read_docx(data, filename)
    return _read_text(data, filename)


def _read_text(data: bytes, filename: str) -> list[dict[str, Any]]:
    """纯文本 / Markdown：按 Markdown 标题或中文序号行识别章节。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")

    sections: list[dict[str, Any]] = []
    title = ""
    buffer: list[str] = []
    for paragraph in _split_into_paragraphs(text):
        is_heading = bool(
            re.match(r"^(#{1,6}\s+|第.+[章节篇]|[一二三四五六七八九十]+、)", paragraph)
        )
        if is_heading and len(paragraph) <= 80:
            if buffer:
                sections.append({"text": "\n".join(buffer), "page_number": 0,
                                 "section_title": title})
                buffer = []
            title = paragraph.lstrip("# ").strip()
        else:
            buffer.append(paragraph)
    if buffer:
        sections.append({"text": "\n".join(buffer), "page_number": 0, "section_title": title})
    return sections


def _read_pdf(data: bytes, filename: str) -> list[dict[str, Any]]:
    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:  # pragma: no cover —— 环境已装，缺了要明确报错而不是静默跳过
        raise IngestError("PDF 解析库未安装（PyPDF2），暂时无法读 PDF") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"PDF「{filename}」无法解析（文件损坏或已加密）：{exc}") from exc

    sections: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 —— 单页失败不该毁掉整份文档
            text = ""
        if text:
            sections.append({"text": text, "page_number": page_number, "section_title": ""})
    return sections


def _read_docx(data: bytes, filename: str) -> list[dict[str, Any]]:
    try:
        from docx import Document
    except ImportError as exc:
        raise IngestError("Word 解析库未安装（python-docx），暂时无法读 .docx") from exc

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"Word「{filename}」无法解析（文件损坏）：{exc}") from exc

    sections: list[dict[str, Any]] = []
    title = ""
    buffer: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        if "heading" in style or "标题" in style:
            if buffer:
                sections.append({"text": "\n".join(buffer), "page_number": 0,
                                 "section_title": title})
                buffer = []
            title = text
        else:
            buffer.append(text)
    if buffer:
        sections.append({"text": "\n".join(buffer), "page_number": 0, "section_title": title})

    # 表格单独成节：单元格用 " | " 连接，保留行列关系（售后规则表常以表格出现）
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        text = "\n".join(row for row in rows if row.strip(" |"))
        if text:
            sections.append({"text": text, "page_number": 0, "section_title": "表格"})
    return sections


# ============================================================
# 切分工具
# ============================================================


def _split_into_paragraphs(text: str) -> list[str]:
    """按空行分段；单换行（中文正文常见的排版）不拆段。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n{2,}", text):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if lines:
            paragraphs.append("".join(lines))
    return paragraphs


def _split_with_overlap(text: str, max_length: int, overlap: int) -> list[str]:
    """按句子边界切分，并把上一块末尾若干字作为下一块的上下文。

    `overlap` 会被夹到 `max_length` 以内 —— 否则第二块可能整段都是重复内容，
    检索时同一段话出现两次，反而挤掉了其它有效片段。
    """
    clean = re.sub(r"[ \t]+", " ", text or "").strip()
    if not clean:
        return []
    max_length = max(80, int(max_length or 700))
    overlap = max(0, min(int(overlap or 0), max_length // 3))
    if len(clean) <= max_length:
        return [clean]

    sentences = [s.strip() for s in re.split(r"(?<=[。！？；.!?;])", clean) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_length:
            chunks.append(current)
            prefix = current[-overlap:] if overlap else ""
            current = prefix + sentence
        else:
            current += sentence
        # 单句超长（无标点的长表格行等）→ 硬切，避免死循环
        while len(current) > max_length:
            chunks.append(current[:max_length])
            current = current[max_length - overlap:] if overlap else current[max_length:]
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]
