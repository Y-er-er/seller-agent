"""知识域的助手工具：**检索 / 查看 / 删除卖家自己上传的资料**。

为什么这组工具重要：

在此之前，助手知道「商品现在什么价」，但**不知道店里自己定的规矩**
（售后政策、发货时效、退换条件、平台规则、操作手册）。卖家问「我们退货怎么规定」
时它只能凭常识编 —— 那是本项目最不能接受的失败方式（不可审计的答案）。

这组工具把"店里的规矩"变成**可引用的原文片段**：回答带上出处（哪份资料、哪一页/哪一节），
卖家能自己核对。与售后判定链路的"逐条依据 + 同输入同结论"是同一条原则。

三条硬约束：

1. **检索与清单只读**：不改数据，因此不进待确认流程（`needs_confirm=False`）。
2. **删除必须走确认卡**：知识库资料删了就没了（不可恢复），和删商品同一条纪律。
   且只按**文件名精确定位**，重名时拒绝执行并让模型先澄清 —— 宁可不动，不能删错。
3. **找不到就说找不到**：检索为空时如实告知，并提示"需要先把资料传到知识库"，
   **绝不让模型拿常识补位**（工具描述与返回文案都写死这一点）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.assistant.registry import (
    DOMAIN_KNOWLEDGE,
    AssistantContext,
    ToolResult,
    ToolSpec,
    register,
)
from app.knowledge.embedding import EmbeddingError
from app.knowledge.store import KnowledgeError, get_store, require_enabled

logger = logging.getLogger(__name__)


def _clip(text: str, limit: int) -> str:
    """压掉多余空白并截断 —— 回灌给模型的片段不宜太长，否则挤占上下文。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _match_docs(
    seller_id: str, filename: str, kb: str = ""
) -> list[dict[str, Any]]:
    """按文件名找文档（大小写不敏感、可带扩展名也可不带）。"""
    target = str(filename or "").strip().lower()
    if not target:
        return []
    docs = get_store().list_documents(seller_id, kb=kb)
    exact = [d for d in docs if str(d.get("filename") or "").lower() == target]
    if exact:
        return exact
    # 允许"只说主名"（卖家常说的是「售后政策」而不是「售后政策.md」）
    stem = target.rsplit(".", 1)[0]
    return [d for d in docs if str(d.get("filename") or "").lower().rsplit(".", 1)[0] == stem]


async def _search_knowledge(
    ctx: AssistantContext, query: str, top_k: int = 5, kb: str = ""
) -> ToolResult:
    """在卖家知识库里做语义检索（可限定某个知识库）。"""
    text = (query or "").strip()
    if not text:
        return ToolResult(content="检索关键词是空的，请说明要查什么。", ok=False)

    try:
        require_enabled()          # 功能关掉时**明确说关闭**，不能伪装成"没查到"
        out = await get_store().search(ctx.seller_id, text, top_k or None, kb=kb)
    except KnowledgeError as exc:
        # 关闭 / 未配置：把原因如实说清（不要伪装成"没查到"）
        return ToolResult(content=f"知识库暂时不可用：{exc}", ok=False)
    except EmbeddingError as exc:
        return ToolResult(content=f"知识库忙碌，检索没成功：{exc}。可以稍后再试一次。", ok=False)
    except Exception as exc:  # noqa: BLE001 —— 向量库异常不该把整轮对话炸掉
        logger.warning("知识库检索异常：%s: %s", type(exc).__name__, exc)
        return ToolResult(content="知识库检索时出错了，这次没查到内容。", ok=False)

    items = out.get("items") or []
    if not items:
        # 两种"没有"要分开说：「库是空的」是**去建库**，「查不到」是**换关键词或补资料**
        try:
            has_docs = bool(get_store().list_documents(ctx.seller_id))
        except Exception:  # noqa: BLE001 —— 判断空库失败就按"没查到"处理
            has_docs = True
        if not has_docs:
            hint = (
                "知识库里**还没有任何资料**。告诉卖家可以去「知识库」页面上传售后政策、"
                "平台规则、商品说明或操作手册（支持 PDF / Word / Markdown / 纯文本，"
                "也可以直接传截图或扫描件，我会先识别文字），上传后我就能按原文回答。"
            )
        elif kb:
            hint = (
                f"在知识库「{kb}」里没有检索到相关内容。\n"
                "**不要凭常识或推测回答**：可以换到其它知识库再搜一次（不传 kb 就是全部），"
                "或如实告诉卖家这个库里没有写。"
            )
        else:
            hint = (
                "知识库里没有检索到和这个问题相关的内容。\n"
                "**不要凭常识或推测回答**：告诉卖家「资料里没找到」，"
                "并提示他可以换个说法再问，或在「知识库」页面补充相关资料。"
            )
        return ToolResult(content=hint, data={"count": 0, "empty_kb": not has_docs})

    lines: list[str] = []
    for index, item in enumerate(items, 1):
        source = item.get("filename") or "未知资料"
        section = item.get("section_title") or ""
        page = item.get("page_number") or 0
        kb_name = item.get("kb") or ""
        where = source + (f" · {section}" if section else "") + (f" · 第 {page} 页" if page else "")
        where += f"（知识库：{kb_name}）" if kb_name else ""
        lines.append(f"[{index}] {where}\n{_clip(item.get('text') or '', 500)}")

    return ToolResult(
        content=(
            f"在知识库里找到 {len(items)} 段相关资料：\n\n" + "\n\n".join(lines)
            + "\n\n（引用时请说明出自哪份资料。⚠️ 如果这些片段其实**答不了**卖家的问题 —— "
              "比如只是碰巧提到了关键词 —— 就直接说「资料里没找到相关说明」，"
              "**不要用常识补全成一套规则**。片段不足以回答时，如实说资料里没写全。）"
        ),
        data={"count": len(items), "items": items},
    )


async def _list_knowledge_docs(ctx: AssistantContext, kb: str = "") -> ToolResult:
    """列出知识库里的资料（可按知识库筛选）。"""
    try:
        require_enabled()
        store = get_store()
        docs = store.list_documents(ctx.seller_id, kb=kb)
        kbs = store.kb_summary(ctx.seller_id)
    except KnowledgeError as exc:
        return ToolResult(content=f"知识库暂时不可用：{exc}", ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库清单读取失败：%s: %s", type(exc).__name__, exc)
        return ToolResult(content="知识库暂时读不到，稍后再试。", ok=False)

    if not docs:
        if kbs:
            names = "、".join(k["kb"] for k in kbs)
            return ToolResult(
                content=f"知识库「{kb}」里没有资料。现有知识库：{names}。",
                data={"count": 0},
            )
        return ToolResult(
            content=(
                "知识库里还没有资料。卖家可以在「知识库」页面上传售后政策、平台规则、"
                "商品说明或操作手册（PDF / Word / Markdown / 纯文本，也可以传截图/扫描件），"
                "之后我就能按原文回答。"
            ),
            data={"count": 0},
        )

    # 按知识库分组展示：卖家问"你有哪些资料"时，能一眼看出库的划分
    grouped: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        grouped.setdefault(doc.get("kb") or "默认知识库", []).append(doc)

    lines: list[str] = []
    for name, items in grouped.items():
        lines.append(f"【{name}】{len(items)} 份")
        for d in items:
            tag_text = f" · 标签 {d['tags']}" if d.get("tags") else ""
            lines.append(f"  · {d['filename']}（{d['chunks']} 个片段{tag_text}）")

    return ToolResult(
        content=(f"知识库共 {len(docs)} 份资料、{len(grouped)} 个知识库：\n" + "\n".join(lines)),
        data={"count": len(docs), "items": docs, "kbs": kbs},
    )


async def _delete_knowledge_doc(
    ctx: AssistantContext, filename: str, kb: str = ""
) -> ToolResult:
    """删除一份资料（**执行阶段**：确认卡点过之后才会走到这里）。"""
    try:
        require_enabled()
        docs = _match_docs(ctx.seller_id, filename, kb)
    except KnowledgeError as exc:
        return ToolResult(content=f"知识库暂时不可用：{exc}", ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除资料前查询失败：%s: %s", type(exc).__name__, exc)
        return ToolResult(content="知识库暂时读不到，这次没有删除任何东西。", ok=False)

    if not docs:
        return ToolResult(
            content=(f"没有找到叫「{filename}」的资料，**没有删除任何东西**。"
                     "可以先调用 list_knowledge_docs 看准确的文件名。"),
            ok=False,
        )
    if len(docs) > 1:
        where = "；".join(f"「{d.get('kb')}」{d['filename']}" for d in docs)
        return ToolResult(
            content=(f"找到 {len(docs)} 份同名资料（{where}），**为了不删错，这次没有删除**。"
                     "请让卖家说明要删哪个知识库里的那一份。"),
            ok=False,
        )

    doc = docs[0]
    try:
        removed = get_store().delete_document(ctx.seller_id, doc["doc_id"])
    except KnowledgeError as exc:
        return ToolResult(content=f"删除没成功：{exc}", ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除资料失败：%s: %s", type(exc).__name__, exc)
        return ToolResult(content="删除时出错了，请稍后重试。", ok=False)

    return ToolResult(
        content=(f"已删除资料《{doc['filename']}》（知识库「{doc.get('kb')}」，"
                 f"{removed} 个片段）。以后检索不到了。"),
        data={"doc_id": doc["doc_id"], "removed": removed, "kb": doc.get("kb")},
    )


async def _impact_delete(ctx: AssistantContext, args: dict[str, Any]) -> str:
    """确认卡上的影响明细：让卖家在建卡那一刻就看清"删的是哪一份、删多少"。"""
    filename = str(args.get("filename") or "")
    kb = str(args.get("kb") or "")
    try:
        docs = _match_docs(ctx.seller_id, filename, kb)
    except Exception:  # noqa: BLE001 —— 预览失败不影响"停下来等确认"
        return ""

    if not docs:
        return f"没有找到叫「{filename}」的资料；确认后也不会删除任何东西。"
    if len(docs) > 1:
        where = "；".join(f"「{d.get('kb')}」{d['filename']}" for d in docs)
        return (f"⚠️ 找到 {len(docs)} 份同名资料（{where}）。"
                "确认后会因无法确定删哪一份而**拒绝执行**，请先说明知识库。")
    doc = docs[0]
    return (f"位置：知识库「{doc.get('kb')}」· {doc['chunks']} 个片段"
            f"（{doc.get('created_at') or '—'}）\n删除后检索不到，且**无法恢复**。")


# ============================================================
# 注册
# ============================================================

_REGISTERED: set[str] = set()


def register_all() -> None:
    """注册知识域工具（幂等）。"""
    specs = [
        ToolSpec(
            name="search_knowledge", domain=DOMAIN_KNOWLEDGE,
            # 只读：失败可自动重试一次（向量库抖动很常见，重查一次就好）
            policy="retryable",
            description=(
                "检索**卖家自己上传的资料库**（售后政策、平台规则、发货与退换约定、"
                "商品说明、操作手册等），返回带出处的原文片段。"
                "卖家问「我们退货怎么规定」「按我们的政策该怎么处理」「资料里怎么写的」"
                "「售后时效是多久」时，**先调这个工具**，再按原文回答并说明出处。"
                "资料分多个知识库时，可用 kb 限定只查某个库（不传则查全部）。"
                "只读，不会改动任何数据。"
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "检索内容，用卖家问的核心词（如「退货时限」「补发条件」）"},
                "top_k": {"type": "integer", "description": "返回片段数，默认 5，最多 20"},
                "kb": {"type": "string",
                       "description": "限定知识库名（如「售后政策」）；不传 = 查全部知识库"},
            }, "required": ["query"]},
            handler=_search_knowledge,
        ),
        ToolSpec(
            name="list_knowledge_docs", domain=DOMAIN_KNOWLEDGE,
            description=(
                "查看知识库里都有哪些资料（按知识库分组，含文件名、片段数、标签、入库时间）。"
                "卖家问「你都看过我哪些资料」「知识库里有什么」时用它。"
                "**删除资料前也先用它确认准确文件名**。只读。"
            ),
            parameters={"type": "object", "properties": {
                "kb": {"type": "string", "description": "只看某个知识库（可选）"},
            }, "required": []},
            handler=_list_knowledge_docs,
        ),
        ToolSpec(
            name="delete_knowledge_doc", domain=DOMAIN_KNOWLEDGE,
            destructive=True,
            summary_template="删除知识库资料《{filename}》",
            description=(
                "删除知识库里的一份资料（整份，含全部分块）。**不可恢复，需要卖家确认。**"
                "只按**文件名精确匹配**：不确定名字时先调用 list_knowledge_docs；"
                "同名资料存在于多个知识库时会拒绝执行，需用 kb 指定。"
            ),
            parameters={"type": "object", "properties": {
                "filename": {"type": "string",
                             "description": "资料的文件名（如「售后政策.md」，可省略扩展名）"},
                "kb": {"type": "string",
                       "description": "资料所在知识库（同名时用于消歧）"},
            }, "required": ["filename"]},
            handler=_delete_knowledge_doc,
            impact_builder=_impact_delete,
            # 同一份资料只该有一张待确认卡
            idempotent_by=("filename", "kb"),
        ),
    ]
    for spec in specs:
        if spec.name in _REGISTERED:
            continue
        register(spec)
        _REGISTERED.add(spec.name)
