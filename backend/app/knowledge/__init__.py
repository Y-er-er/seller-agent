"""知识库 —— 卖家自建资料的**检索**能力（RAG 的"R"）。

三块，职责单一、可分别替换：

- `embedding.py` 文本 → 向量（**唯一**与向量化端点打交道的地方）
- `ingest.py`    文档 → 分块（PDF / Word / Markdown / 纯文本）
- `store.py`     分块 → 向量库（Chroma 持久化，**按 seller_id 隔离**）

设计来源：本项目参考 `E:/PycharmProject/RAG/chromadb_fastapi` 的实现思路
（`app/core/engine.py` 的 Chroma 封装、`app/core/file_parser.py` 的分块策略），
**复制适配**而来 —— 原项目一个文件都不改。

两处有意的改造（不是"照抄"）：

1. **不依赖 chromadb 内置的 OpenAIEmbeddingFunction**：改为手动传 `embeddings`。
   这样测试可以注入确定性假向量 —— 全链路离线、零计费；真实运行时才走 DashScope。
2. **检索范围按 seller_id 过滤**：与项目"登录只预留接口位"的约定一致 ——
   现在恒为 `default`，将来接登录时这里不用改。
"""

from __future__ import annotations
