"""知识库 API —— 卖家上传资料、查看清单、试检索、改归属。

**UI 与助手共用同一条链路**：这里的 `/search` 与助手的 `search_knowledge` 工具
走的是同一个 `KnowledgeStore`，所以"界面上搜得到"和"助手答得出"不会出现两套结果。

四个约定（与项目其他路由一致）：

1. **静态路径声明在参数路径之前**：`/upload`、`/search` 必须在 `/{doc_id}` 前面，
   否则 FastAPI 会把 "upload" 当成 doc_id 匹配掉。
2. **失败要说人话**：解析失败、密钥缺失、功能关闭都返回具体原因（400），
   不返回空列表假装"知识库里没有"。
3. **上传按后缀分流**：文本类走解析，图片走 OCR（视觉模型，**计费**），
   两条路的产物都交给同一套切分规则 —— 片段粒度一致，检索排序才有可比性。
4. **改归属整份文档一起改**：kb / tags 是文档级属性，只改一部分分块会出现
   "同一份资料一半在这个库、一半在另一个库"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import settings
from app.knowledge import ingest, ocr
from app.knowledge.embedding import EmbeddingError
from app.knowledge.store import KnowledgeError, get_store, require_enabled
from app.seller.deps import require_seller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seller/knowledge", tags=["商铺经营Agent · 知识库"])


def _fail(exc: Exception) -> None:
    """把知识库业务错误转成 HTTP 400（错误原文直接给卖家看，本来就是人话）。"""
    raise HTTPException(status_code=400, detail=str(exc))


async def _meter(tokens: int) -> None:
    """把**向量化与 OCR** 的用量计入成本计量。

    **失败只记日志**：计量是统计，不是资金动作 —— 不能因为写不进去就让上传失败
    （与"资金写通道失败必须升级人工"是两种不同性质，不要混用同一套处理）。
    """
    if tokens <= 0:
        return
    try:
        from app.chat import cost_gate

        await cost_gate.record_usage(tokens, 0, case_id=None, seq=None, persist=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库用量记账失败（忽略，不影响入库）：%s: %s", type(exc).__name__, exc)


class SearchPayload(BaseModel):
    query: str
    top_k: int = Field(default=0, description="返回条数；0 = 用配置里的默认值")
    kb: str = Field(default="", description="限定知识库；空 = 全部知识库")


class DocMetaPayload(BaseModel):
    """改文档归属 / 标签。**未传的字段不动**（与商品 PATCH 同一约定）。"""

    kb: str | None = None
    tags: str | None = None


class UploadResult(BaseModel):
    ok: bool = True
    doc_id: str = ""
    filename: str = ""
    kb: str = ""
    chunks: int = 0
    message: str = ""


# ============================================================
# 静态路径（必须在 /{doc_id} 之前）
# ============================================================


@router.get("")
async def list_knowledge(
    kb: str = "", seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """知识库现状：统计 + 知识库清单 + 文档清单（可按库筛选）。"""
    try:
        require_enabled()
        store = get_store()
        return {
            "stats": store.stats(seller_id),
            "kbs": store.kb_summary(seller_id),
            "items": store.list_documents(seller_id, kb=kb),
        }
    except KnowledgeError as exc:
        _fail(exc)


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    kb: str = Form(""),
    tags: str = Form(""),
    seller_id: str = Depends(require_seller),
) -> dict[str, Any]:
    """上传一份资料：解析（或 OCR）→ 分块 → 向量化 → 入库。

    单次上传**同步完成**：卖家上传的就是小体量规则文档（几页到几十页），
    走后台任务反而让"上传成功但还搜不到"变成常态，不如等它真的可用。
    """
    try:
        require_enabled()
    except KnowledgeError as exc:
        _fail(exc)

    filename = Path(file.filename or "").name          # 剥掉任何路径成分
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"文件「{filename}」是空的")
    if len(data) > settings.knowledge_max_upload_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"文件太大（{len(data) // 1024} KB），"
                   f"单份资料上限 {settings.knowledge_max_upload_bytes // 1024 // 1024} MB",
        )

    suffix = Path(filename).suffix.lower()
    meter_tokens = 0
    try:
        if suffix in ingest.IMAGE_SUFFIXES:
            # 图片：先 OCR 成文字，再走与文本完全相同的切分规则
            text, ocr_tokens = await ocr.image_to_text(filename, data)
            meter_tokens += ocr_tokens
            chunks = ingest.split_text(
                text,
                chunk_size=settings.knowledge_chunk_size,
                chunk_overlap=settings.knowledge_chunk_overlap,
                section_title="（图片识别）",
            )
        else:
            chunks = ingest.parse_document(
                filename, data,
                chunk_size=settings.knowledge_chunk_size,
                chunk_overlap=settings.knowledge_chunk_overlap,
            )
        result = await get_store().add_document(
            seller_id, filename, chunks, kb=kb, tags=tags
        )
    except (ingest.IngestError, ocr.OcrError, KnowledgeError, EmbeddingError) as exc:
        _fail(exc)

    await _meter(meter_tokens + int(result.get("tokens") or 0))
    where = f"「{result['kb']}」· {result['chunks']} 个片段"
    return {
        "ok": True,
        "doc_id": result["doc_id"],
        "filename": result["filename"],
        "kb": result["kb"],
        "tags": result["tags"],
        "chunks": result["chunks"],
        "message": f"已入库：{result['filename']}（{where}）",
    }


@router.post("/search")
async def search_knowledge(
    payload: SearchPayload, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """试检索：卖家自己先搜一下，确认资料能被找到（同样的结果助手也会看到）。"""
    try:
        require_enabled()
        out = await get_store().search(
            seller_id, payload.query, payload.top_k or None, kb=payload.kb
        )
    except (KnowledgeError, EmbeddingError) as exc:
        _fail(exc)

    await _meter(int(out.get("tokens") or 0))
    return {
        "query": payload.query, "kb": payload.kb,
        "items": out["items"], "count": len(out["items"]),
    }


# ============================================================
# 参数路径
# ============================================================


@router.patch("/{doc_id}")
async def update_document(
    doc_id: str, payload: DocMetaPayload, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """改一份资料的知识库归属 / 标签（未传的字段不动）。"""
    try:
        require_enabled()
        out = get_store().update_document_meta(
            seller_id, doc_id, kb=payload.kb, tags=payload.tags
        )
    except KnowledgeError as exc:
        _fail(exc)
    return {"ok": True, **out}


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """删除一份资料的全部分块（不可恢复，前端需二次确认）。"""
    try:
        require_enabled()
        removed = get_store().delete_document(seller_id, doc_id)
    except KnowledgeError as exc:
        _fail(exc)
    return {"ok": True, "removed_chunks": removed}
