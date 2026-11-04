"""向量库封装（Chroma 持久化）—— 知识库的存取层。

移植自 RAG 项目的 `app/core/engine.py`（`SemanticSearchEngine`），保留其
"collection + metadata 过滤 + 相似度换算"的骨架，但按本项目的约束改造：

1. **手动传向量**（`embeddings=`），不把 embedding 绑在 collection 上 ——
   测试可注入假向量，全链路离线（见 `embedding.py` 顶部注释）。
2. **按 seller_id 隔离**：每条分块的 metadata 都带 `seller_id`，检索/列表/删除
   全部按它过滤。现在恒为 `default`（登录只预留接口位），将来接多卖家不用改这里。
3. **按知识库（kb）分组**：同一卖家可以有多个知识库（如「售后政策」「发货规则」），
   检索可限定某个库。知识库清单**从文档 metadata 聚合**得出，不单独建表 ——
   没有资料的库等于不存在，这个取舍避免了"空库幽灵"与一套额外的存储。
4. **相似度换算保持与 RAG 项目一致**：`similarity = 1 / (1 + distance)`，仅用于
   界面展示与日志；**不做阈值裁剪** —— 阈值容易把"弱相关但确实提到"的片段砍掉，
   让模型自己判断"这段能不能答"更可靠。

⚠️ 持久化目录由 `KNOWLEDGE_DIR` 决定；Chroma 会在该目录下写文件，
   删除文档请走 `delete_document`，不要手工删目录（会与进程内句柄不一致）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, settings
from app.knowledge.embedding import Embedder, get_embedder

logger = logging.getLogger(__name__)

#: 未指定知识库时的归属。界面与工具都用这个名字，避免出现"空库名"这种隐形状态
DEFAULT_KB = "默认知识库"


def _resolve_dir(raw: str) -> Path:
    """把配置里的相对路径解析到 backend/ 下（与 upload_dir 的处理方式一致）。"""
    path = Path(raw or "./data/knowledge")
    if not path.is_absolute():
        path = PROJECT_ROOT / "backend" / path
    return path


def normalize_kb(kb: str) -> str:
    """知识库名归一化：空值 → 默认库；限长，避免 metadata 里塞进异常长的名字。"""
    return (str(kb or "").strip() or DEFAULT_KB)[:60]


class KnowledgeError(RuntimeError):
    """知识库业务错误（未启用 / 未配置 / 文档不存在）。调用方转 400。"""


class KnowledgeStore:
    """一个进程内共享的向量库实例。

    Chroma 的 `PersistentClient` 在**同一路径上重复创建**会拿到同一份底层数据，
    但句柄不宜多开；因此由 `get_store()` 做单例，测试则直接构造独立实例
    （各自用临时目录）。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        embedder: Embedder | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.path = Path(path) if path else _resolve_dir(settings.knowledge_dir)
        self.collection_name = collection_name or settings.knowledge_collection
        self.embedder: Embedder = embedder or get_embedder()
        self._client: Any = None
        self._collection: Any = None
        self._lock = threading.Lock()

    # ---------- 连接（惰性：不在 import 期创建目录） ----------

    @property
    def collection(self) -> Any:
        if self._collection is None:
            with self._lock:
                if self._collection is None:
                    import chromadb

                    self.path.mkdir(parents=True, exist_ok=True)
                    self._client = chromadb.PersistentClient(path=str(self.path))
                    # cosine：文本相似度的常规选择，距离范围 0~2，换算成 similarity 更直观
                    self._collection = self._client.get_or_create_collection(
                        name=self.collection_name,
                        metadata={"hnsw:space": "cosine"},
                    )
        return self._collection

    # ---------- 过滤条件 ----------

    @staticmethod
    def _where(seller_id: str, kb: str = "") -> dict[str, Any]:
        """组装过滤条件：卖家隔离是**必带**的，知识库是可选的第二维。"""
        if kb:
            return {"$and": [{"seller_id": seller_id}, {"kb": normalize_kb(kb)}]}
        return {"seller_id": seller_id}

    # ---------- 写入 ----------

    async def add_document(
        self,
        seller_id: str,
        filename: str,
        chunks: list[dict[str, Any]],
        *,
        kb: str = "",
        tags: str = "",
    ) -> dict[str, Any]:
        """把一份文档的全部分块入库。

        返回 `{doc_id, filename, kb, tags, chunks, tokens}`。
        """
        if not chunks:
            raise KnowledgeError("没有可入库的内容")

        texts = [str(c.get("text") or "") for c in chunks]
        vectors, tokens = await self.embedder.embed(texts)
        if len(vectors) != len(texts):
            raise KnowledgeError(
                f"向量化结果与文本条数不一致（{len(vectors)} vs {len(texts)}），已放弃入库"
            )

        doc_id = uuid.uuid4().hex[:12]
        kb_name = normalize_kb(kb)
        tag_text = _clean_tags(tags)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        metadatas = []
        for index, chunk in enumerate(chunks):
            metadatas.append({
                "seller_id": seller_id,
                "doc_id": doc_id,
                "kb": kb_name,
                "tags": tag_text,
                "filename": filename,
                "chunk_index": int(chunk.get("chunk_index", index)),
                "page_number": int(chunk.get("page_number") or 0),
                "section_title": str(chunk.get("section_title") or "")[:120],
                "created_at": created_at,
            })
        ids = [f"{doc_id}-{index}" for index in range(len(chunks))]

        self.collection.add(ids=ids, documents=texts, embeddings=vectors, metadatas=metadatas)
        logger.info(
            "知识库入库：seller=%s kb=%s doc=%s 文件=%s 分块=%d tokens=%d",
            seller_id, kb_name, doc_id, filename, len(ids), tokens,
        )
        return {
            "doc_id": doc_id, "filename": filename, "kb": kb_name, "tags": tag_text,
            "chunks": len(ids), "tokens": tokens,
        }

    def delete_document(self, seller_id: str, doc_id: str) -> int:
        """删除一份文档的全部分块。**先校验归属**，避免越权删别家的资料。"""
        ids, metadatas = self._chunks_of(seller_id, doc_id)
        if not ids:
            raise KnowledgeError("这份资料不存在（可能已被删除）")
        self.collection.delete(ids=ids)
        logger.info("知识库删除：seller=%s doc=%s 分块=%d", seller_id, doc_id, len(ids))
        return len(ids)

    def update_document_meta(
        self, seller_id: str, doc_id: str, *, kb: str | None = None, tags: str | None = None
    ) -> dict[str, Any]:
        """改文档的知识库归属 / 标签（整份文档的所有分块一起改）。

        为什么整份文档一起改：kb 与 tags 是**文档级**属性，只改一部分分块会造成
        "同一份资料一半在这个库、一半在另一个库"——检索时结果来自半个文档，很诡异。
        """
        ids, metadatas = self._chunks_of(seller_id, doc_id)
        if not ids:
            raise KnowledgeError("这份资料不存在（可能已被删除）")

        merged_list = []
        for metadata in metadatas:
            merged = dict(metadata or {})
            if kb is not None:
                merged["kb"] = normalize_kb(kb)
            if tags is not None:
                merged["tags"] = _clean_tags(tags)
            merged_list.append(merged)
        self.collection.update(ids=ids, metadatas=merged_list)
        logger.info("知识库改归属：seller=%s doc=%s kb=%s", seller_id, doc_id, kb)
        return {
            "doc_id": doc_id,
            "kb": merged_list[0].get("kb") or DEFAULT_KB,
            "tags": merged_list[0].get("tags") or "",
            "chunks": len(ids),
        }

    # ---------- 读取 ----------

    async def search(
        self, seller_id: str, query: str, top_k: int | None = None, *, kb: str = ""
    ) -> dict[str, Any]:
        """语义检索。返回 `{items: [...], tokens: int}`，`items` 按相关度降序。"""
        text = (query or "").strip()
        if not text:
            return {"items": [], "tokens": 0}

        total = self._count(seller_id, kb)
        if total == 0:
            return {"items": [], "tokens": 0}

        limit = max(1, min(int(top_k or settings.knowledge_top_k), 20))
        vectors, tokens = await self.embedder.embed([text])
        if not vectors:
            return {"items": [], "tokens": 0}

        result = self.collection.query(
            query_embeddings=vectors,
            n_results=min(limit, total),
            where=self._where(seller_id, kb),
            include=["documents", "metadatas", "distances"],
        )

        items: list[dict[str, Any]] = []
        ids = (result.get("ids") or [[]])[0]
        for index, chunk_id in enumerate(ids):
            distance = float((result.get("distances") or [[0.0]])[0][index])
            metadata = ((result.get("metadatas") or [[]])[0][index] or {})
            items.append({
                "id": chunk_id,
                "text": ((result.get("documents") or [[]])[0][index] or ""),
                "similarity": round(1 / (1 + distance), 4),
                "distance": round(distance, 4),
                "filename": metadata.get("filename") or "",
                "doc_id": metadata.get("doc_id") or "",
                "kb": metadata.get("kb") or DEFAULT_KB,
                "tags": metadata.get("tags") or "",
                "section_title": metadata.get("section_title") or "",
                "page_number": int(metadata.get("page_number") or 0),
                "chunk_index": int(metadata.get("chunk_index") or 0),
            })
        return {"items": items, "tokens": tokens}

    def list_documents(self, seller_id: str, *, kb: str = "") -> list[dict[str, Any]]:
        """按文档聚合的清单（只取 metadata，不拉正文，省内存）。"""
        result = self.collection.get(
            where=self._where(seller_id, kb), include=["metadatas"]
        )
        grouped: dict[str, dict[str, Any]] = {}
        for metadata in (result.get("metadatas") or []):
            meta = metadata or {}
            doc_id = str(meta.get("doc_id") or "")
            if not doc_id:
                continue
            item = grouped.setdefault(doc_id, {
                "doc_id": doc_id,
                "filename": meta.get("filename") or "（未命名）",
                "kb": meta.get("kb") or DEFAULT_KB,
                "tags": meta.get("tags") or "",
                "chunks": 0,
                "created_at": meta.get("created_at") or "",
            })
            item["chunks"] += 1
        return sorted(
            grouped.values(),
            key=lambda d: (d.get("kb") or "", d.get("created_at") or ""),
            reverse=True,
        )

    def kb_summary(self, seller_id: str) -> list[dict[str, Any]]:
        """知识库清单：**从文档 metadata 聚合**（没有资料的库不出现）。"""
        grouped: dict[str, dict[str, Any]] = {}
        for doc in self.list_documents(seller_id):
            name = doc.get("kb") or DEFAULT_KB
            item = grouped.setdefault(name, {"kb": name, "documents": 0, "chunks": 0})
            item["documents"] += 1
            item["chunks"] += int(doc.get("chunks") or 0)
        return sorted(grouped.values(), key=lambda x: (-x["documents"], x["kb"]))

    def stats(self, seller_id: str) -> dict[str, Any]:
        docs = self.list_documents(seller_id)
        kbs = self.kb_summary(seller_id)
        return {
            "documents": len(docs),
            "chunks": sum(int(d.get("chunks") or 0) for d in docs),
            "kbs": len(kbs),
            "enabled": bool(settings.knowledge_enabled),
            "embedding_model": settings.embedding_model,
            "dimensions": int(settings.embedding_dimensions),
            "ocr_enabled": bool(settings.llm_enabled and settings.dashscope_api_key),
        }

    # ---------- 内部 ----------

    def _chunks_of(self, seller_id: str, doc_id: str) -> tuple[list[str], list[dict[str, Any]]]:
        """取某文档的全部分块 id 与 metadata，并**校验归属**。

        越权删改一律报"不存在"——不暴露"这份资料存在，但不是你的"。
        """
        result = self.collection.get(where={"doc_id": doc_id}, include=["metadatas"])
        ids = list(result.get("ids") or [])
        metadatas = list(result.get("metadatas") or [])
        if not ids:
            return [], []
        owners = {str((m or {}).get("seller_id") or "") for m in metadatas}
        if owners and seller_id not in owners:
            return [], []
        return ids, metadatas

    def _count(self, seller_id: str, kb: str = "") -> int:
        """当前范围的分块总数（Chroma 的 count() 不支持条件，只能 get 后取长度）。"""
        result = self.collection.get(where=self._where(seller_id, kb), include=[])
        return len(result.get("ids") or [])


def _clean_tags(raw: str) -> str:
    """标签归一化：去空白、去重、限长（Chroma metadata 只存标量）。"""
    parts = [p.strip() for p in str(raw or "").replace("，", ",").split(",")]
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part[:20])
    return ",".join(unique[:10])


# ============================================================
# 单例
# ============================================================

_STORE: KnowledgeStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> KnowledgeStore:
    """进程内共享的向量库实例（惰性创建）。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = KnowledgeStore()
    return _STORE


def set_store(store: KnowledgeStore | None) -> None:
    """替换实例（测试用：注入临时目录 + 假向量，避免碰真实数据）。"""
    global _STORE
    _STORE = store


def require_enabled() -> None:
    """知识库开关检查。关闭时**明确拒绝**，不静默返回空结果。"""
    if not settings.knowledge_enabled:
        raise KnowledgeError("知识库功能当前已关闭（KNOWLEDGE_ENABLED=false）")
