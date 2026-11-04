"""知识库测试 —— **全部离线**（注入假向量化，不发任何外部请求、零计费）。

覆盖要点：
  A 组 解析与分块：格式支持 / 空文件拒绝 / Markdown 章节识别 / 长文按句切分并保留重叠
  B 组 存储与检索：入库 / 语义命中 / seller 隔离 / 列表聚合 / 删除 / 越权删除被拒
  C 组 助手工具：只读不需确认 / 命中带出处 / 无结果引导上传 / 开关关闭明确报错
  D 组 装配：端点已挂载、工具已注册

⚠️ 用的是**假 Embedder**（字符二元组词袋 + 余弦），所以能离线验证"检索逻辑本身"，
   但不代表真实 DashScope 向量的效果 —— 后者只能在真机上验。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import loop, registry  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402
from app.config import settings  # noqa: E402
from app.knowledge import ingest  # noqa: E402
from app.knowledge.store import (  # noqa: E402
    DEFAULT_KB,
    KnowledgeError,
    KnowledgeStore,
    set_store,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


class FakeEmbedder:
    """确定性假向量：字符二元组词袋 → 归一化。

    为什么用二元组而不是纯随机：要让"退货时限"和"退货时限是 15 天"相似度更高，
    检索逻辑（排序、过滤、截断）才测得出来。纯随机向量测不出任何有意义的行为。
    """

    def __init__(self, dim: int = 96) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            clean = "".join(str(text or "").split())
            for i in range(len(clean) - 1):
                gram = clean[i:i + 2]
                bucket = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16) % self.dim
                vec[bucket] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            vectors.append([v / norm for v in vec])
        return vectors, len(texts) * 7          # 假装每条 7 个 token

    async def embed_empty(self) -> list[float]:
        return [0.0] * self.dim


def group_a_ingest() -> None:
    print("【A 组】解析与分块")

    chunks = ingest.parse_document("policy.md", "# 售后政策\n\n退货需在签收后 15 天内提出。\n\n补发条件：质量问题三选一。".encode("utf-8"))
    check("A1 Markdown 解析出分块", len(chunks) >= 1, f"{len(chunks)} 块")
    check("A2 首个分块带章节标题", chunks[0]["section_title"] == "售后政策", chunks[0]["section_title"])
    check("A3 分块带 chunk_index 序号", [c["chunk_index"] for c in chunks] == list(range(len(chunks))))

    try:
        ingest.parse_document("empty.txt", b"")
        check("A4 空文件被拒绝", False, "竟然通过")
    except ingest.IngestError as exc:
        check("A4 空文件被拒绝", "空" in str(exc), str(exc))

    try:
        ingest.parse_document("bad.xlsx", b"xx")
        check("A5 不支持的格式被拒绝", False, "竟然通过")
    except ingest.IngestError as exc:
        check("A5 不支持的格式被拒绝", "不支持" in str(exc), str(exc))

    # 长文按句切分：块长受控、且相邻块有重叠（重叠是"边界句仍能读懂"的保证）
    long_text = "".join(f"第{i}条规定：退款需在签收后十五天内提出，超时不候。" for i in range(1, 40))
    pieces = ingest._split_with_overlap(long_text, 200, 60)
    check("A6 长文被切成多块且每块不超上限",
          len(pieces) > 1 and all(len(p) <= 200 for p in pieces), f"{len(pieces)} 块")
    check("A7 相邻块保留重叠上下文",
          any(pieces[i][:20] in pieces[i - 1] for i in range(1, len(pieces))))

    # Word：现场用 python-docx 造一个，验证"段落 + 表格"两条路都通
    try:
        from docx import Document

        doc = Document()
        doc.add_heading("发货规则", level=1)
        doc.add_paragraph("付款后 48 小时内发出。")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "情形"
        table.cell(0, 1).text = "处理"
        table.cell(1, 0).text = "缺货"
        table.cell(1, 1).text = "补发或退款"
        buf = io.BytesIO()
        doc.save(buf)
        doc_chunks = ingest.parse_document("rule.docx", buf.getvalue())
        joined = " ".join(c["text"] for c in doc_chunks)
        titles = " ".join(c["section_title"] for c in doc_chunks)
        # 标题进 metadata（section_title）而不是正文 —— 这是刻意的：
        # 正文只留内容，检索结果用 section_title 标"出自哪一节"
        check("A8 Word 段落与表格都能解析，标题进章节元数据",
              "48 小时" in joined and "缺货" in joined and "发货规则" in titles,
              (joined + " | " + titles)[:100])
    except ImportError:      # pragma: no cover —— 环境已装
        check("A8 Word 段落与表格都能解析", False, "python-docx 未安装")


async def group_b_store(tmp: Path) -> None:
    print("\n【B 组】存储与检索")
    store = KnowledgeStore(path=tmp, embedder=FakeEmbedder(), collection_name="test_knowledge")
    set_store(store)

    text = (
        "# 售后政策\n\n退货需在签收后 15 天内提出，超时不予受理。\n\n"
        "# 补发规则\n\n质量问题可选择补发，补发运费由商家承担。\n\n"
        "# 发货时效\n\n付款后 48 小时内发出，节假日顺延。"
    )
    chunks = ingest.parse_document("售后政策.md", text.encode("utf-8"))
    out = await store.add_document("default", "售后政策.md", chunks)
    check("B1 入库返回文档号与分块数", bool(out["doc_id"]) and out["chunks"] == len(chunks),
          str(out))

    hit = await store.search("default", "退货时限是多久", 3)
    check("B2 检索命中相关资料", bool(hit["items"]) and hit["items"][0]["filename"] == "售后政策.md",
          str([i["filename"] for i in hit["items"]]))
    top = hit["items"][0]["text"]
    check("B3 命中的是「退货/时限」那段，而不是随便一段", "15 天" in top or "退货" in top, top[:60])
    check("B4 相似度按降序返回",
          [i["similarity"] for i in hit["items"]] == sorted(
              [i["similarity"] for i in hit["items"]], reverse=True))
    check("B5 计量 token 有回传", hit["tokens"] > 0, str(hit["tokens"]))

    # seller 隔离：另一个卖家既搜不到，也看不到清单
    other = await store.search("another-seller", "退货时限是多久", 3)
    check("B6 卖家隔离：别家搜不到这些资料", other["items"] == [], str(other["items"])[:60])
    check("B7 卖家隔离：别家清单为空", store.list_documents("another-seller") == [])

    empty = await store.search("default", "   ", 3)
    check("B8 空查询直接返回空（不发向量化请求）", empty == {"items": [], "tokens": 0})

    docs = store.list_documents("default")
    check("B9 清单按文档聚合", len(docs) == 1 and docs[0]["chunks"] == len(chunks), str(docs))
    stats = store.stats("default")
    check("B10 统计数字与清单一致",
          stats["documents"] == 1 and stats["chunks"] == len(chunks), str(stats))

    # 越权删除：别家来删，必须报"不存在"而不是真删掉
    try:
        store.delete_document("another-seller", out["doc_id"])
        check("B11 越权删除被拒绝", False, "竟然删掉了")
    except KnowledgeError as exc:
        check("B11 越权删除被拒绝", "不存在" in str(exc), str(exc))
    check("B12 越权删除后资料仍在", len(store.list_documents("default")) == 1)

    removed = store.delete_document("default", out["doc_id"])
    check("B13 正常删除清掉全部分块", removed == len(chunks), f"删了 {removed} 块")
    after = await store.search("default", "退货时限", 3)
    check("B14 删除后检索不到", after["items"] == [], str(after["items"])[:60])

    try:
        store.delete_document("default", "not-exist")
        check("B15 删除不存在的文档明确报错", False, "竟然通过")
    except KnowledgeError:
        check("B15 删除不存在的文档明确报错", True)


async def group_c_tools(tmp: Path) -> None:
    print("\n【C 组】助手工具")
    loop.ensure_tools_registered()

    spec = registry.get("search_knowledge")
    check("C1 检索工具已注册", spec is not None)
    check("C2 检索工具只读（不需确认）", spec is not None and not spec.needs_confirm)
    check("C3 检索工具失败可重试（只读，重试安全）",
          spec is not None and spec.failure_policy == "retryable", spec.failure_policy if spec else "")
    check("C4 工具归入「知识库」域", spec is not None and spec.domain == registry.DOMAIN_KNOWLEDGE)

    store = KnowledgeStore(path=tmp, embedder=FakeEmbedder(), collection_name="test_tools")
    set_store(store)
    chunks = ingest.parse_document(
        "发货规则.md",
        "# 发货与物流\n\n正常 48 小时内发货，大促期间 72 小时。\n\n# 快递\n\n默认发顺丰，偏远地区改邮政。".encode("utf-8"),
    )
    await store.add_document("default", "发货规则.md", chunks)

    ctx = AssistantContext("default", "t-thread")
    out = await spec.handler(ctx, query="多久发货", top_k=3)
    check("C5 命中时回复带出处与编号", "发货规则.md" in out.content and "[1]" in out.content,
          out.content[:80])
    check("C6 命中时返回结构化结果供审计", out.data.get("count", 0) >= 1, str(out.data.get("count")))
    check("C6b 命中时提示「片段无关就别硬答」",
          "不要用常识补" in out.content, out.content[-60:])

    # 空库：另起一个独立库，验证「还没有资料」这条引导（假向量对任何中文都有弱相关，
    # 所以"查不到"这条分支只能靠空库来构造 —— 真实模型下才会自然出现）
    empty_store = KnowledgeStore(
        path=tmp.parent / "empty", embedder=FakeEmbedder(), collection_name="test_empty"
    )
    set_store(empty_store)
    miss = await spec.handler(ctx, query="退货时限是多久", top_k=3)
    check("C7 空库时明确告知没有资料", "还没有任何资料" in miss.content, miss.content[:80])
    check("C8 空库时引导去上传（不是编造答案）",
          "上传" in miss.content and miss.data.get("empty_kb") is True, miss.content[-60:])

    # 恢复成有资料的库，继续测清单工具
    set_store(KnowledgeStore(path=tmp, embedder=FakeEmbedder(), collection_name="test_tools"))
    list_spec = registry.get("list_knowledge_docs")
    listed = await list_spec.handler(ctx)
    check("C9 清单工具列出文件名与片段数",
          "发货规则.md" in listed.content and "片段" in listed.content, listed.content[:80])

    # 开关关闭：必须明确说"已关闭"，不能伪装成"没查到"
    original = settings.knowledge_enabled
    settings.knowledge_enabled = False
    try:
        closed = await spec.handler(ctx, query="多久发货", top_k=3)
        check("C10 功能关闭时明确报「已关闭」",
              "关闭" in closed.content and not closed.ok, closed.content[:80])
    finally:
        settings.knowledge_enabled = original


async def group_d_wiring() -> None:
    print("\n【D 组】装配")
    from app.main import app

    paths = set(app.openapi()["paths"].keys())
    for path in ("/api/seller/knowledge", "/api/seller/knowledge/upload",
                 "/api/seller/knowledge/search", "/api/seller/knowledge/{doc_id}"):
        check(f"D {path} 已挂载", path in paths)

    names = {s.name for s in registry.all_specs()}
    check("D5 知识域工具在能力清单里",
          {"search_knowledge", "list_knowledge_docs", "delete_knowledge_doc"} <= names,
          str(sorted(names)))

    prompt = loop.build_system_prompt(AssistantContext("default", "t"))
    check("D6 系统提示要求「店里规矩以知识库为准」",
          "search_knowledge" in prompt and "知识库" in prompt and "绝不要用常识编" in prompt)
    check("D7 系统提示说明删除资料只能通过工具（不能嘴上说已删）",
          "delete_knowledge_doc" in prompt and "已删除" in prompt)


async def group_e_multikb(tmp: Path) -> None:
    """多知识库 / 标签 / 改归属 —— 让"售后政策"和"平台规则"互不干扰。"""
    print("\n【E 组】多知识库与标签")
    store = KnowledgeStore(path=tmp, embedder=FakeEmbedder(), collection_name="test_multikb")
    set_store(store)

    after = ingest.parse_document(
        "售后政策.md",
        "# 退货政策\n\n签收后 15 天内可退货，需保持吊牌完整。\n\n"
        "# 补发\n\n质量问题可补发，运费由商家承担。".encode("utf-8"),
    )
    platform = ingest.parse_document(
        "平台规则.md",
        "# 违规处罚\n\n虚假发货首次扣 2 分，罚款 100 元。\n\n"
        "# 发货时限\n\n付款后 48 小时内必须有物流揽收记录。".encode("utf-8"),
    )

    out1 = await store.add_document(
        "default", "售后政策.md", after, kb="售后政策", tags="退货， 时效 ,,退货"
    )
    out2 = await store.add_document(
        "default", "平台规则.md", platform, kb="平台规则", tags="处罚,发货"
    )
    out3 = await store.add_document("default", "临时笔记.txt",
                                   ingest.parse_document("临时笔记.txt", "备注：周末不发货。".encode("utf-8")))

    check("E1 入库进指定知识库", out1["kb"] == "售后政策" and out2["kb"] == "平台规则", str(out1["kb"]))
    check("E2 不指定知识库时落默认库", out3["kb"] == DEFAULT_KB, out3["kb"])
    # 标签归一化：中文逗号也拆开、去空白、去重
    check("E3 标签归一化（中文逗号/去空白/去重）",
          out1["tags"] == "退货,时效", repr(out1["tags"]))

    kbs = store.kb_summary("default")
    check("E4 知识库清单从文档聚合（不含空的库）",
          {k["kb"] for k in kbs} == {"售后政策", "平台规则", DEFAULT_KB}, str(kbs))
    check("E4b 清单带份数与片段数",
          all(k["documents"] >= 1 and k["chunks"] >= 1 for k in kbs), str(kbs))
    stats = store.stats("default")
    check("E4c 统计里的知识库个数与清单一致", stats["kbs"] == len(kbs), str(stats))

    scoped = await store.search("default", "发货时限是多久", 5, kb="平台规则")
    check("E5 限定知识库检索只返回该库片段",
          bool(scoped["items"]) and all(i["kb"] == "平台规则" for i in scoped["items"]),
          str([(i["kb"], i["filename"]) for i in scoped["items"]]))
    check("E5b 命中的确实是平台规则那份",
          all(i["filename"] == "平台规则.md" for i in scoped["items"]),
          str([i["filename"] for i in scoped["items"]]))

    only_after = store.list_documents("default", kb="售后政策")
    check("E6 清单按知识库筛选", [d["filename"] for d in only_after] == ["售后政策.md"],
          str([d["filename"] for d in only_after]))

    # 改归属：整份文档一起走，不能一半在旧库一半在新库
    moved = store.update_document_meta("default", out3["doc_id"], kb="售后政策", tags="备注")
    check("E7 改归属返回新的库名", moved["kb"] == "售后政策", str(moved))
    check("E7b 改归属后原库清单里没有它、新库清单里有",
          "临时笔记.txt" not in [d["filename"] for d in store.list_documents("default", kb=DEFAULT_KB)]
          and "临时笔记.txt" in [d["filename"] for d in store.list_documents("default", kb="售后政策")])
    check("E8 改归属同时改标签", moved["tags"] == "备注", moved["tags"])
    hit_new = await store.search("default", "周末不发货", 5, kb="售后政策")
    check("E9 改库后能按新库检索到（不是只改了清单显示）",
          any(i["filename"] == "临时笔记.txt" for i in hit_new["items"]),
          str([i["filename"] for i in hit_new["items"]]))

    # 只传一个字段时另一个不动
    tags_only = store.update_document_meta("default", out2["doc_id"], tags="处罚,时效")
    check("E10 只改标签时库名不变", tags_only["kb"] == "平台规则" and tags_only["tags"] == "处罚,时效",
          str(tags_only))

    try:
        store.update_document_meta("another-seller", out2["doc_id"], kb="别人的库")
        check("E11 越权改归属被拒绝", False, "竟然改成了")
    except KnowledgeError as exc:
        check("E11 越权改归属被拒绝", "不存在" in str(exc), str(exc))
    check("E11b 越权改归属后资料仍在原库",
          store.list_documents("default", kb="平台规则") != [])

    try:
        store.update_document_meta("default", "not-exist", kb="随便")
        check("E12 改不存在的文档明确报错", False, "竟然通过")
    except KnowledgeError:
        check("E12 改不存在的文档明确报错", True)

    none = await store.search("default", "退货", 5, kb="不存在的库")
    check("E13 检索不存在的库返回空（不报错、不串库）", none["items"] == [], str(none["items"])[:60])


async def group_f_ocr(tmp: Path) -> None:
    """OCR：**全部离线**（把视觉调用换成桩，不发请求、零计费）。"""
    print("\n【F 组】图片识别（OCR）")
    from app.knowledge import ocr

    original_vision = ocr.vision_available
    original_invoke = ocr._invoke_vision
    calls: list[tuple] = []

    def fake_invoke(images, prompt, **kwargs):
        calls.append((images, prompt, kwargs))
        return "# 售后须知\n\n退货需在签收后 15 天内提出。", {"total_tokens": 321}

    ocr._invoke_vision = fake_invoke          # type: ignore[assignment]

    # 1) 视觉关闭：必须**明确报错**，且一个请求都不发
    ocr.vision_available = lambda: False      # type: ignore[assignment]
    try:
        try:
            await ocr.image_to_text("scan.png", b"\x89PNG\r\n")
            check("F1 视觉未启用时明确报错", False, "竟然通过了")
        except ocr.OcrError as exc:
            check("F1 视觉未启用时明确报错", "视觉模型" in str(exc), str(exc)[:60])
        check("F1b 未启用时没有发出任何模型请求", calls == [], f"{len(calls)} 次")
    finally:
        ocr.vision_available = original_vision   # type: ignore[assignment]

    try:
        try:
            await ocr.image_to_text("scan.png", b"")
            check("F2 空图片被拒绝", False, "竟然通过了")
        except ocr.OcrError as exc:
            check("F2 空图片被拒绝", "空" in str(exc), str(exc)[:40])

        try:
            await ocr.image_to_text("big.png", b"x" * (ocr.MAX_IMAGE_BYTES + 1))
            check("F3 超大图片在本地就拦下", False, "竟然通过了")
        except ocr.OcrError as exc:
            check("F3 超大图片在本地就拦下", "太大" in str(exc), str(exc)[:40])
        check("F3b 被拦下的图片没有发请求（不白花钱）", calls == [], f"{len(calls)} 次")

        text, tokens = await ocr.image_to_text("scan.png", b"\x89PNG\r\n fake")
        check("F4 识别成功返回文本", "15 天" in text, text[:40])
        check("F5 如实回传 token 用量（供计量）", tokens == 321, str(tokens))
        check("F6 图片以 base64 data URI 传给视觉模型",
              calls and str(calls[-1][0][0]).startswith("data:image/png;base64,"),
              str(calls[-1][0][0])[:40] if calls else "没调用")
        check("F7 OCR 提示词禁止发挥（只转写不润色）",
              "不要润色" in calls[-1][1] and "□" in calls[-1][1])

        # 识别结果要能接上**同一套切分规则**（否则与文本资料片段粒度不一致，排序没有可比性）
        chunks = ingest.split_text(text, chunk_size=700, chunk_overlap=100,
                                  section_title="（图片识别）")
        check("F8 识别结果走同一套切分规则",
              len(chunks) >= 1 and chunks[0]["section_title"] == "（图片识别）"
              and "15 天" in chunks[0]["text"], str(chunks)[:80])

        # 模型返回空 / 只有"（无文字）"：必须报错，不能静默入库一份空资料
        ocr._invoke_vision = lambda *a, **k: ("   ", {})   # type: ignore[assignment]
        try:
            await ocr.image_to_text("blank.png", b"\x89PNG")
            check("F9 模型无返回时明确报错", False, "竟然通过了")
        except ocr.OcrError as exc:
            check("F9 模型无返回时明确报错", "没有返回内容" in str(exc), str(exc)[:40])

        ocr._invoke_vision = lambda *a, **k: ("（无文字）", {"total_tokens": 12})  # type: ignore[assignment]
        try:
            await ocr.image_to_text("empty.png", b"\x89PNG")
            check("F10 图里没文字时明确报错（不塞空资料）", False, "竟然通过了")
        except ocr.OcrError as exc:
            check("F10 图里没文字时明确报错（不塞空资料）", "没有识别到文字" in str(exc), str(exc)[:40])
    finally:
        ocr._invoke_vision = original_invoke    # type: ignore[assignment]


async def group_g_delete_tool(tmp: Path) -> None:
    """删除工具的**确认卡语义**：不可逆动作绝不能绕过人在环。"""
    print("\n【G 组】删除资料工具")
    loop.ensure_tools_registered()
    spec = registry.get("delete_knowledge_doc")
    check("G1 删除工具已注册", spec is not None)
    check("G2 删除工具必须确认（destructive）", spec is not None and spec.needs_confirm and spec.destructive)
    check("G3 确认卡摘要模板写明删哪份", spec is not None and "删除知识库资料" in spec.summary_template,
          spec.summary_template if spec else "")
    check("G4 按资料名去重（同一份只该有一张卡）",
          spec is not None and spec.idempotent_by == ("filename", "kb"), str(spec.idempotent_by if spec else ""))

    store = KnowledgeStore(path=tmp, embedder=FakeEmbedder(), collection_name="test_deltool")
    set_store(store)
    ctx = AssistantContext("default", "t-del")

    await store.add_document("default", "售后政策.md",
                             ingest.parse_document("售后政策.md", "# 退货\n\n15 天内可退。".encode("utf-8")),
                             kb="售后政策")
    doc_id = store.list_documents("default")[0]["doc_id"]

    impact = await spec.impact_builder(ctx, {"filename": "售后政策.md"})
    check("G5 影响明细写明位置与片段数",
          "售后政策" in impact and "片段" in impact and "无法恢复" in impact, impact[:80])

    missing = await spec.impact_builder(ctx, {"filename": "根本没有.md"})
    check("G6 名字对不上时影响明细如实说「也不会删任何东西」",
          "没有找到" in missing and "不会删除" in missing, missing[:60])

    # 执行阶段：名字不存在 → 必须明确"没有删除任何东西"
    bad = await spec.handler(ctx, filename="根本没有.md")
    check("G7 名字不存在时拒绝且说明没删任何东西",
          not bad.ok and "没有删除任何东西" in bad.content, bad.content[:60])
    check("G8 拒绝后资料仍在", len(store.list_documents("default")) == 1)

    # 只说主名（不带扩展名）也能定位到
    ok = await spec.handler(ctx, filename="售后政策")
    check("G9 只说主名也能删对（卖家常不带扩展名）",
          ok.ok and ok.data.get("removed", 0) >= 1, ok.content[:60])
    check("G10 删除后清单为空", store.list_documents("default") == [])

    # 同名资料存在于两个库：宁可不动，也不能删错
    await store.add_document("default", "规则.md",
                             ingest.parse_document("规则.md", "# A\n\n甲内容。".encode("utf-8")), kb="售后政策")
    await store.add_document("default", "规则.md",
                             ingest.parse_document("规则.md", "# B\n\n乙内容。".encode("utf-8")), kb="平台规则")
    ambiguous = await spec.handler(ctx, filename="规则.md")
    check("G11 同名资料跨库时拒绝执行（不删错）",
          not ambiguous.ok and "同名" in ambiguous.content, ambiguous.content[:60])
    check("G12 拒绝后两份都还在", len(store.list_documents("default")) == 2)
    impact2 = await spec.impact_builder(ctx, {"filename": "规则.md"})
    check("G13 重名的影响明细提前警告" , "拒绝执行" in impact2, impact2[:80])

    # 用 kb 消歧后就能删对那一份
    scoped = await spec.handler(ctx, filename="规则.md", kb="平台规则")
    check("G14 指定知识库后能删对那一份",
          scoped.ok and scoped.data.get("kb") == "平台规则", scoped.content[:60])
    left = store.list_documents("default")
    check("G15 另一份同名资料没被连带删除",
          len(left) == 1 and left[0]["kb"] == "售后政策", str(left)[:80])

    # 清单工具按库分组
    list_spec = registry.get("list_knowledge_docs")
    listed = await list_spec.handler(ctx)
    check("G16 清单工具按知识库分组展示", "【售后政策】" in listed.content, listed.content[:80])


async def amain() -> int:
    group_a_ingest()

    tmp = Path(tempfile.mkdtemp(prefix="kb-test-"))
    try:
        await group_b_store(tmp / "b")
        await group_c_tools(tmp / "c")
        await group_e_multikb(tmp / "e")
        await group_f_ocr(tmp / "f")
        await group_g_delete_tool(tmp / "g")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        set_store(None)

    await group_d_wiring()

    print(f"\n结果：{'全部通过' if not failures else f'{len(failures)} 项失败'}")
    for item in failures:
        print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
