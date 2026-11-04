"""文本向量化 —— 与 DashScope 兼容端点打交道的**唯一**入口。

为什么不直接用 chromadb 自带的 `OpenAIEmbeddingFunction`：

1. **可注入**：测试注入确定性假向量 → 入库/检索全链路离线跑通，**零计费**；
   内置实现把 embedding 绑死在 collection 上，测试就难免要发真实请求。
2. **可控**：批量大小、超时、失败语义都在本项目手里。内置实现会把 HTTP 异常
   包成它自己的类型，排查时反而绕远。

⚠️ 真实调用**按 token 计费**：本层如实返回 `tokens`，由调用方决定计量与降级
（成本是配置项，不是省着不用的理由 —— 但花了多少要能被看见）。
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """向量化失败（密钥缺失 / 网络 / 端点报错）。

    刻意**不吞异常**：检索失败必须让上层如实告诉卖家"没查到"，
    而不是静默返回空结果让它以为"知识库里没有这段"。
    """


class Embedder(Protocol):
    """向量化端口。测试用确定性实现替换，生产用 DashScope。"""

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """返回 (向量列表, 消耗 token 数)。向量顺序与入参顺序**严格一致**。"""
        ...


class DashScopeEmbedder:
    """千问向量化（OpenAI 兼容 `/embeddings` 端点）。

    `text-embedding-v3` 单次最多 10 条，超了会被服务端拒 —— 所以按批切分，
    而不是一次把整篇文档塞进去。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = settings.dashscope_api_key if api_key is None else api_key
        self.model = model or settings.embedding_model
        self.base_url = (base_url or settings.dashscope_compat_base).rstrip("/")
        self.dimensions = int(dimensions or settings.embedding_dimensions)
        self.batch_size = max(1, int(batch_size or settings.embedding_batch))
        self.timeout = float(timeout or settings.embedding_timeout)

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        items = [t for t in (texts or []) if str(t or "").strip()]
        if not items:
            return [], 0
        if not self.api_key:
            raise EmbeddingError(
                "未配置 DASHSCOPE_API_KEY，无法把资料转成向量。"
                "请在 backend/.env 填入密钥后重启服务。"
            )

        vectors: list[list[float]] = []
        tokens = 0
        # 分批：向量化接口对单次条数有上限（v3 为 10 条）
        for start in range(0, len(items), self.batch_size):
            batch = items[start:start + self.batch_size]
            vectors.extend(await self._call(batch))
            tokens += self._last_tokens
        return vectors, tokens

    async def _call(self, batch: list[str]) -> list[list[float]]:
        payload = {
            "model": self.model,
            "input": batch,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"向量化请求失败：{type(exc).__name__}: {exc}") from exc

        if resp.status_code != 200:
            # 截断正文：报错原文常常很长，塞进日志与界面都不合适
            raise EmbeddingError(
                f"向量化端点返回 {resp.status_code}：{resp.text[:200]}"
            )

        try:
            body = resp.json()
            data = body.get("data") or []
            # 按 index 排序：接口**不保证**返回顺序与入参一致
            ordered = sorted(data, key=lambda d: int(d.get("index", 0)))
            vectors = [list(item["embedding"]) for item in ordered]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"向量化响应格式异常：{exc}") from exc

        if len(vectors) != len(batch):
            raise EmbeddingError(
                f"向量化返回条数不符（请求 {len(batch)} 条，返回 {len(vectors)} 条）"
            )
        self._last_tokens = int((body.get("usage") or {}).get("total_tokens") or 0)
        return vectors

    #: 最近一次批次的 token 用量（由 `embed` 汇总）
    _last_tokens: int = 0


def get_embedder() -> Embedder:
    """当前生效的向量化实现（生产恒为 DashScope）。"""
    return DashScopeEmbedder()
