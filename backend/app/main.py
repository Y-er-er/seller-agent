"""FastAPI 接入层。

端点清单：
    GET  /api/health                  健康检查（进程 + 依赖可用性）
    POST /api/requests                提交申请并执行图（**幂等**：同号重复提交返回既有结论）
    GET  /api/requests/{order_id}     查看当前状态（含是否挂起待复核）
    POST /api/review/{order_id}       提交人工复核决定并恢复执行
    GET  /api/reviews/pending         待复核队列（M4 复核工作台）
    GET  /api/audits                  审计记录分页查询（M5）
    GET  /api/audits/stats            审计聚合统计（M5 一致率报表）
    GET  /api/audits/{request_id}     单笔申请的审计明细
    POST /api/qa                      口径问答（客服释疑，**不参与判定**）
    GET  /api/qa/knowledge            口径知识库条目（供工作台提示可问什么）
    GET  /api/dev/scenarios           演示场景清单（⚠️ 非业务能力）
    POST /api/dev/seed                注入演示取数（⚠️ 非业务能力）

图节点为 async（取数需并行 I/O），故端点一律使用异步调用（ainvoke）。
本文件只做"接入"：参数校验 + 调用图/仓储；判定逻辑一律在 graph / rules 内，
状态裁剪一律在 graph.queries 内。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from app import console
from app.chat import cost_gate, orchestrator
from app.chat import reply as chat_reply
from app.chat import session as chat_session
from app.chat import state as chat_state
from app.chat import tools as chat_tools
from app.config import settings
from app.config_validation import log_warnings
from app.goods.service import GoodsError
from app.assistant.routes import router as assistant_router
from app.knowledge.routes import router as knowledge_router
from app.orders.routes import router as orders_router
from app.service_chat.routes import buyer_router as service_buyer_router
from app.service_chat.routes import seller_router as service_seller_router
from app.seller.routes import goods_error_handler
from app.seller.routes import router as seller_router
from app.seller.auth_routes import router as auth_router
from app.db import connect, get_decisions_by_request, get_stats, list_decisions, run_sync
from app.demo_seed import backfill_case_goods, demo_active, ensure_demo_order, load_demo_orders
from app.dev_seed import apply_scenario, real_api_configured, scenario_options, suggest_amount
from app.graph.builder import close_graph, get_graph
from app.graph.queries import (
    STATUS_PENDING_REVIEW,
    get_thread_state,
    list_pending_reviews,
    summarize_result,
    thread_config,
)
from app.llm.client import text_available, vision_available
from app.models import RefundRequest
from app.qa import answer_question, list_entries

logger = logging.getLogger(__name__)


@asynccontextmanager
def _log_runtime_mode() -> None:
    """启动期打印**当前运行模式**（几行，便于一眼确认环境态）。

    为什么值得单独打：`DATA_SOURCE` 决定取数走演示夹具还是真实 API，
    这是最容易"搞错环境"的一项配置（开发当生产、生产当演示都要命），必须让它在启动日志里显影。

    ⚠️ 为什么用 `warning` 而不是 `info`：本项目未配置 logging handler，
    Python 的 `logging.lastResort` 只放行 WARNING 及以上 —— 用 info 会**完全不显示**（实测踩到）。
    模式行"看不见"等于没做，故统一用 warning（与 `config_validation.log_warnings` 同一做法）。
    """
    mode = settings.resolved_data_source
    declared = (settings.data_source or "").strip().lower()
    via = "显式配置" if declared in ("local", "remote") else "自动推断"

    if mode == "local":
        logger.warning(
            "数据源模式 = local（%s）：取数走**进程内夹具**（演示数据，非真实业务数据）", via
        )
    else:
        missing = [
            name
            for name, val in (
                ("ORDER_API_BASE", settings.order_api_base),
                ("LOGISTICS_API_BASE", settings.logistics_api_base),
                ("AFTERSALE_API_BASE", settings.aftersale_api_base),
            )
            if not val
        ]
        if missing:
            # 这是真问题：remote 却不配 base_url，取数会全失败（本层不会回落到夹具）
            logger.error(
                "数据源模式 = remote（%s）但 %s 未配置：对应取数将一律失败，**不会回落到夹具**",
                via,
                " / ".join(missing),
            )
        else:
            logger.warning("数据源模式 = remote（%s）：取数走真实业务 API", via)

    logger.warning(
        "对象存储 = %s（COS 配置齐全：%s）",
        (settings.storage_backend or "local").strip().lower(),
        settings.storage_configured,
    )

    # 图像生成：同样属于"环境态必须可见"的一项（留空即未开通，生图端点会明确报错）
    from app.llm import image_gen

    logger.warning(
        "图像生成 = %s（模型 %s / 尺寸 %s）",
        "已启用" if image_gen.available() else "未启用（IMAGE_MODEL 留空或未配置密钥）",
        settings.image_model or "—",
        settings.image_size,
    )


async def lifespan(_app: FastAPI):
    """启动期自检：把配置中的语义冲突提前暴露到日志（不阻断启动）；
    打印运行模式（数据源 / 存储）；
    注入演示假订单（仅本地 mock 模式），供买家 H5 扮演买家 + 卖家智能体自演演练；
    关闭期释放检查点连接（否则 aiosqlite 的非守护线程会让进程无法退出）。"""
    log_warnings(logger)
    _log_runtime_mode()
    load_demo_orders()
    # 工具注册表**在启动期就绪**：确认卡的措辞（会产生费用 / 不可撤销）与"工具是否存在"
    # 都要查注册表 —— 若等到第一次对话才注册，启动后先点一张待确认卡会被误判成
    # "工具已不存在"而直接把卡作废（不可恢复）。
    from app.assistant import loop as assistant_loop

    assistant_loop.ensure_tools_registered()
    # 历史工单补齐商品编号（商品 × 售后联动的关联键；幂等、失败不阻断启动）
    await backfill_case_goods()
    # 上次进程遗留的"执行中"任务标为中断 —— 后台协程随进程没了，不能假装还在跑
    from app.db.task_repo import mark_interrupted

    await mark_interrupted()

    # 回收站到期清理（默认 7 天）：启动时跑一次，避免长期不打开回收站导致无限堆积
    try:
        from app.goods import service as goods_service

        await goods_service.cleanup_expired_trash()
    except Exception as exc:  # noqa: BLE001 —— 清理是旁路，失败不阻断启动
        logger.warning("回收站到期清理失败（已忽略）：%s: %s", type(exc).__name__, exc)

    # 登录：建表 + 首次启动创建初始账号（只在账号表为空时创建，**不改已有密码**）。
    # 失败不阻断启动 —— 服务仍可跑其它域（但卖家控制台会停在登录窗，日志里有明确原因）。
    try:
        from app.seller import auth as seller_auth

        await seller_auth.bootstrap()
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录模块初始化失败（卖家控制台将无法登录）：%s: %s", type(exc).__name__, exc)
    yield
    await close_graph()


app = FastAPI(title="电商售后智能体", version="0.6.0", lifespan=lifespan)

# 放行跨域：复核工作台是免构建的静态页面（file:// 或独立静态端口），与后端不同源。
# 来源列表由 .env 的 CORS_ORIGINS 控制（默认 "*"，仅本地开发）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # ⚠️ 必须含 PATCH / DELETE：商品域用它们做「改商品 / 删商品」。
    #    少了这两个方法时，跨源前端（file:// 双击打开）的请求会被浏览器直接拦掉，
    #    而且报错在控制台里很隐蔽（表现为 fetch failed，看不出是 CORS）。
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# 卖家控制台（商品域）
# 业务错误统一转成明确状态码 + 中文原因，不裸抛 500。
# ------------------------------------------------------------
app.add_exception_handler(GoodsError, goods_error_handler)
# 登录：所有 `/api/seller/*` 都依赖它解析出的 seller_id（见 app/seller/deps.py）
app.include_router(auth_router)
app.include_router(seller_router)
# AI 经营助手（阶段二）：工具注册表 + 待确认动作 + 对话循环
app.include_router(assistant_router)
# 知识库（RAG）：卖家上传资料 → 分块向量化 → 助手按需检索引用
app.include_router(knowledge_router)
app.include_router(orders_router)
app.include_router(service_buyer_router)
app.include_router(service_seller_router)

# 查询类端点的分页上限，防止一次拉取过多（复核台/看板均为人工查阅场景）
_MAX_PAGE_SIZE = 200


class ReviewPayload(BaseModel):
    """人工复核决定。"""

    decision: str = "maintain"  # maintain（维持）/ override（改判）
    reviewer: str = ""


# 时间参数接受两种写法：纯日期，或带时分秒（空格 / T 分隔均可）
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")


def _normalize_datetime(value: str | None, *, end_of_day: bool = False, field: str = "") -> str | None:
    """把查询参数规整为可直接与 `created_at`(DATETIME) 比较的字符串。

    ⚠️ 为什么必须规整：`created_at <= '2026-10-13'` 等价于 `<= '2026-10-13 00:00:00'`，
    会**把当天数据全部排出范围**。因此纯日期形式需补齐时分秒：
    起始补 `00:00:00`，结束补 `23:59:59`（"含当天"的直觉语义）。
    """
    if not value:
        return None
    raw = value.strip()
    if _DATE_ONLY.match(raw):
        return f"{raw} {'23:59:59' if end_of_day else '00:00:00'}"
    if _DATE_TIME.match(raw):
        return raw.replace("T", " ")
    raise HTTPException(
        status_code=400,
        detail=f"{field} 格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，实得：{value}",
    )


# ============================================================
# 提交串行化（幂等保护）
# ============================================================
# 同一受理号的提交必须串行化：前端连点、客户端重试、网络重发都会产生并发同号请求，
# 若不加保护，两个请求会同时通过"是否已受理"的检查，造成重复判定与重复处置。
# 用**弱引用字典**：锁在无人引用时自动回收，长期运行不会积累内存。
_submit_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _submit_lock(order_id: str) -> asyncio.Lock:
    """取该受理号的提交锁（不存在则创建）。

    ⚠️ 调用方必须持有返回值（强引用），否则弱引用字典中的锁会被提前回收，保护失效。
    """
    lock = _submit_locks.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        _submit_locks[order_id] = lock
    return lock


# ============================================================
# 受理与复核
# ============================================================


@app.post("/api/requests")
async def submit_request(request: RefundRequest) -> dict[str, Any]:
    """提交一笔「仅退款」申请并执行判定。

    ⚠️ **幂等保证（涉及资金安全，务必保留）**：
    `thread_id = 受理号`，LangGraph 会从既有检查点**续跑**；而 `hits` / `audit_events`
    是 `operator.add` 追加型字段 —— 重复提交会让命中规则**累加**（实测 R2 → R2,R2 → R2,R2,R2），
    同时重复写审计行、重复触发处置。在真实资金场景下等同于**重复退款**。

    因此：该受理号已存在状态时，**直接返回既有结论**，并标记 `idempotent_replay=true`，
    不重新判定、不重复留痕、不重复处置。

    局限：本保护为**单进程**（进程内锁）。若将来多进程/多实例部署，需换分布式锁
    （Redis 等）或改为数据库唯一约束兜底。
    """
    async with _submit_lock(request.order_id):
        graph = await get_graph()

        # 已受理过（含挂起中）→ 返回既有状态，绝不重复执行
        existing = await get_thread_state(graph, request.order_id)
        if existing is not None:
            return {**existing, "idempotent_replay": True}

        result = await graph.ainvoke(
            {"request": request}, config=thread_config(request.order_id)
        )
        return {**summarize_result(result), "idempotent_replay": False}


@app.get("/api/requests/{order_id}")
async def get_request_state(order_id: str) -> dict[str, Any]:
    """查看某笔申请的当前状态（含是否挂起）。"""
    graph = await get_graph()
    state = await get_thread_state(graph, order_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到受理号：{order_id}")
    return state


@app.post("/api/review/{order_id}")
async def review(order_id: str, payload: ReviewPayload) -> dict[str, Any]:
    """提交人工复核决定并恢复图执行。"""
    graph = await get_graph()
    state = await get_thread_state(graph, order_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到受理号：{order_id}")
    if state.get("status") != STATUS_PENDING_REVIEW:
        raise HTTPException(status_code=409, detail="该申请当前未处于待复核状态")

    result = await graph.ainvoke(
        Command(resume={"decision": payload.decision, "reviewer": payload.reviewer}),
        config=thread_config(order_id),
    )
    return summarize_result(result)


# ============================================================
# 复核工作台（M4）
# ============================================================


@app.get("/api/reviews/pending")
async def list_reviews_pending(
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE, description="返回条数上限"),
) -> dict[str, Any]:
    """列出当前挂起待人工复核的申请（最近活动优先）。

    数据源是检查点而非审计表：`to_human` 路径在 `human_review` 处挂起时，
    业务审计尚未写入（见 `graph/queries.py` 模块说明）。
    """
    graph = await get_graph()
    items = await list_pending_reviews(graph, limit=limit)
    return {"total": len(items), "items": items}


# ============================================================
# 审计查询（M5）
# ============================================================


@app.get("/api/audits")
async def list_audit_records(
    conclusion: str | None = Query(default=None, description="按结论过滤，如 to_human"),
    order_id: str | None = Query(default=None, description="按订单号过滤"),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """分页查询审计记录（最新在前）。

    查询失败**不静默返回空**——数据库不可用时明确返回 503，
    避免把"库挂了"误读成"没有数据"。
    """
    try:
        return await list_decisions(
            conclusion=conclusion, order_id=order_id, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001 — 转为明确的 HTTP 语义
        raise HTTPException(
            status_code=503, detail=f"审计库不可用（{type(exc).__name__}）：{exc}"
        ) from exc


# ⚠️ 路由顺序：本端点必须声明在 `/api/audits/{request_id}` **之前**。
# FastAPI 按声明顺序匹配，否则 `/api/audits/stats` 会被 `{request_id}` 捕获成受理号 "stats"。
@app.get("/api/audits/stats")
async def audit_stats(
    since: str | None = Query(default=None, description="起始时间（含）：YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"),
    until: str | None = Query(default=None, description="结束时间（含）：同上，纯日期时含当天"),
) -> dict[str, Any]:
    """判定结论聚合统计（M5）。

    核心指标 `agreement_rate` = 人工**维持**数 /（维持 + 改判）数，
    即"规则结论被人工认可的比例"——这正是影子模式调优所需的**一致率**。
    无人工复核样本时返回 `null`（而非 0，避免误读为"完全不一致"）。
    """
    since_norm = _normalize_datetime(since, field="since")
    until_norm = _normalize_datetime(until, end_of_day=True, field="until")
    if since_norm and until_norm and since_norm > until_norm:
        raise HTTPException(status_code=400, detail="since 不能晚于 until")

    try:
        return await get_stats(since=since_norm, until=until_norm)
    except Exception as exc:  # noqa: BLE001 — 同上，转明确语义
        raise HTTPException(
            status_code=503, detail=f"审计库不可用（{type(exc).__name__}）：{exc}"
        ) from exc


@app.get("/api/audits/{request_id}")
async def get_audit_records(request_id: str) -> dict[str, Any]:
    """查询某个受理号的全部审计记录（按写入顺序）。"""
    try:
        items = await get_decisions_by_request(request_id)
    except Exception as exc:  # noqa: BLE001 — 同上
        raise HTTPException(
            status_code=503, detail=f"审计库不可用（{type(exc).__name__}）：{exc}"
        ) from exc

    if not items:
        raise HTTPException(status_code=404, detail=f"未找到审计记录：{request_id}")
    return {"request_id": request_id, "total": len(items), "items": items}


# ============================================================
# 口径问答（客服释疑 —— ⚠️ 不参与资格判定，见 app/qa/ 模块说明）
# ============================================================


class QaPayload(BaseModel):
    """口径问答请求。"""

    question: str
    # 可选：带上受理号则一并解释"这笔申请为什么这么判"
    order_id: str | None = None


# ⚠️ 路由顺序：`/api/qa/knowledge` 与 `/api/qa` 无冲突（一个 GET 一个 POST），
# 但为保持与 audits 端点一致的可读性，仍把静态路径写在前面。
@app.get("/api/qa/knowledge")
async def qa_knowledge() -> dict[str, Any]:
    """列出全部标准口径条目，供工作台提示"可以问什么"。"""
    items = list_entries()
    return {"total": len(items), "items": items}


@app.post("/api/qa")
async def qa_ask(payload: QaPayload) -> dict[str, Any]:
    """口径问答：解释判定依据、回答规则口径问题。

    **边界**：本端点只做解释与释疑，不改变任何判定结论、不触发任何节点、不写任何状态。
    事实来自规则（确定性解释 + 关键词口径检索）；`QA_LLM_POLISH` 与 `LLM_ENABLED`
    同时开启时才由模型润色措辞（**产生计费调用**），返回值中的 `model_used` 可核对是否发生调用。
    """
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    return await answer_question(question, order_id=(payload.order_id or None))


# ============================================================
# 买家侧对话（H5，P1）—— 独立前缀，便于灰度与下线
# ============================================================
#
# 回退：`CHAT_ENABLED=false` 时这些端点返回 503，系统退回原有控制台形态，
#       判定链路（既有 12 个端点）完全不受影响。
# P1.2 先落地**非流式**两条 + 会话快照；流式（SSE）与上传在 P1.3。


class H5CreateCase(BaseModel):
    """创建对话会话。

    `demo`：**演示模式**标记——仅当服务端 `DEMO_ANY_ORDER=true` 时生效（否则 403）。
    命中时由 `demo_seed.ensure_demo_order()` 按订单号**确定性合成**订单/物流/画像事实，
    响应体带 `demo.synthetic=true`；界面**必须**如实标注"合成数据"，不得伪装成真实订单。
    """

    order_id: str
    demo: bool = False


class H5Message(BaseModel):
    """买家发一条消息。"""

    text: str


def _require_chat_enabled() -> None:
    """对话入口总开关检查（关闭时明确 503，不静默失败）。

    ⚠️ 以**路由依赖**（`Depends`）方式挂载，而不是写在函数体里：
    FastAPI 会**先解析请求体再进入函数**，若写在体内，入口关闭时用户会先撞上
    "缺少 file" 之类的 422 校验错误，看不到"入口未启用"的真正原因（真实冒烟发现）。
    """

    if not settings.chat_enabled:
        raise HTTPException(status_code=503, detail="对话入口当前未启用（CHAT_ENABLED=false）")


@app.post("/api/h5/cases", dependencies=[Depends(_require_chat_enabled)])
async def h5_create_case(payload: H5CreateCase) -> dict[str, Any]:
    """创建（或幂等返回）一个买家侧对话会话。

    会话与工单同体：`case_id = order_id`，与判定图 `thread_id`、审计检索键同值。
    闸门暂停时**拒绝新会话**并返回原因（不静默失败）。

    `demo=true`（演示模式）：先确保该订单可被受理——未预置的订单号按**确定性夹具**注入事实。
    服务端未开启（`DEMO_ANY_ORDER=false`）时明确 403，不静默降级为"数据不足"。
    """
    demo_info: dict[str, Any] | None = None
    if payload.demo:
        if not settings.demo_any_order:
            raise HTTPException(
                status_code=403,
                detail="服务端未启用「任意订单受理」（DEMO_ANY_ORDER=false）",
            )
        demo_info = ensure_demo_order(payload.order_id)

    try:
        result = await chat_session.create_session(payload.order_id)
    except chat_session.SessionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    if demo_info is not None:
        result["demo"] = demo_info
    return result


@app.get("/api/h5/cases/{case_id}", dependencies=[Depends(_require_chat_enabled)])
async def h5_get_case(case_id: str) -> dict[str, Any]:
    """会话快照：状态、消息、槽位、护栏记录（卷宗「对话历史」的数据源）。"""
    try:
        return await chat_session.load_session(case_id)
    except chat_session.SessionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/h5/cases/{case_id}/messages", dependencies=[Depends(_require_chat_enabled)])
async def h5_post_message(case_id: str, payload: H5Message) -> dict[str, Any]:
    """买家发消息 → 跑一次完整对话往返（护栏 → agentic 循环 → 结论 → 落库）。

    P1.2 为非流式；P1.3 改为 SSE 逐字返回（D6 决策）。
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    try:
        return await orchestrator.handle_buyer_message(case_id, text)
    except chat_session.SessionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ---- P1.3：上传举证 / 转人工 ----

# 上传白名单与限额（P1：**本地 mock 存储**，不接对象存储、不外发、不做真伪判断）
ALLOWED_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024          # 单文件 5MB
_UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "data" / "h5_uploads"   # 已在 .gitignore


def _safe_dir_name(value: str) -> str:
    """把外部传入的 case_id 收敛为安全目录名（**防目录穿越**）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value or "")[:64] or "unknown"


@app.post("/api/h5/cases/{case_id}/attachments", dependencies=[Depends(_require_chat_enabled)])
async def h5_upload_attachment(case_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """上传举证材料（P1.3）。

    **边界**：只做接收与登记，**不做真伪 / 质量判断**（业务红线：质量问题一律转人工）。
    存储为本地 mock；登记走与对话循环**同一个工具**（`register_evidence`），
    确保模型在后续轮次读到的材料与页面上传的一致。
    """
    # 会话必须存在——避免为不存在的会话静默创建目录（脏数据）
    try:
        snapshot = await chat_session.load_session(case_id)
    except chat_session.SessionError as exc:
        # 必须转成 HTTP 状态码；否则 SessionError 会裸抛成 500（真实冒烟才发现）
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    case = snapshot["session"]

    raw_name = Path(file.filename or "").name        # 只取文件名，剥掉任何路径成分
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少文件名")
    suffix = Path(raw_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{suffix or '(无扩展名)'}；仅支持 {'/'.join(sorted(ALLOWED_UPLOAD_SUFFIXES))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB）")

    # 内容摘要作为文件名与引用标识：**同一文件重复上传天然幂等**（不产生重复材料）
    digest = hashlib.sha256(data).hexdigest()[:16]
    ref = f"h5://{case_id}/{digest}{suffix}"

    target_dir = _UPLOAD_ROOT / _safe_dir_name(case_id)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{digest}{suffix}").write_bytes(data)
    except OSError as exc:
        # 落盘失败**不得**当成功——否则后续判定会以为材料已到手
        raise HTTPException(status_code=503, detail=f"材料保存失败：{exc}") from exc

    # 登记到槽位（与对话循环读材料的路径一致）
    ctx = chat_tools.ToolContext(
        case_id=case_id,
        order_id=str(case.get("order_id") or case_id),
        last_seq=int(case.get("last_seq") or 0),
    )
    registered = await chat_tools.register_evidence(ctx, {"evidence_refs": [ref]})
    if not registered.ok:
        # 落盘成功但登记失败：如实告知（材料在，但判定时可能读不到），不假装成功
        raise HTTPException(status_code=503, detail=f"材料登记失败：{registered.error or '未知原因'}")

    return {
        "ref": ref,
        "filename": raw_name,
        "size": len(data),
        "content_type": file.content_type or "",
        "registered": True,
    }


class H5Escalate(BaseModel):
    """买家主动转人工（原因码由后端固定，此处仅供附注）。"""

    note: str = ""


@app.post("/api/h5/cases/{case_id}/escalate", dependencies=[Depends(_require_chat_enabled)])
async def h5_escalate(case_id: str, payload: H5Escalate | None = None) -> dict[str, Any]:
    """买家主动转人工（P1.3）。终态操作，**幂等**（已是终态则原样返回）。"""
    try:
        snapshot = await chat_session.load_session(case_id)      # 会话不存在 → 404
    except chat_session.SessionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    case = snapshot["session"]

    already_terminal = chat_state.is_terminal(case["status"])
    ok = await chat_session.escalate(case_id, chat_state.ESCALATE_BUYER_REQUEST)
    if not ok:
        raise HTTPException(status_code=503, detail="转人工失败，请稍后重试")

    reply_text = chat_reply.render_escalate(chat_state.ESCALATE_BUYER_REQUEST)
    # 仅在**首次**转入终态时落回执，避免重复点击刷出多条相同消息
    seq_note = ""
    if not already_terminal:
        posted = await chat_session.append_agent_message(
            case_id, reply_text, guardrail_event=chat_state.ESCALATE_BUYER_REQUEST
        )
        seq_note = posted.get("seq", "")

    return {
        "case_id": case_id,
        "status": chat_state.ESCALATED,
        "reason": chat_state.ESCALATE_BUYER_REQUEST,
        "already_terminal": already_terminal,
        "reply": reply_text,
        "seq": seq_note,
        "note": (payload.note if payload else "") or "",
    }


# ---- P1.3：流式（SSE）----
#
# ⚠️ **诚实标注**：P1 的模型调用是**非流式**的（`agentic` 循环内部一次性取回该轮
#    文本），因此这里的 SSE 是**确定性分段推送**（把已生成的回复按字符切段逐条发出），
#    **不是模型 token 级流**。它的价值是：① 前端有渐进式反馈（进度播报 + 打字效果）；
#    ② 协议已就位，P3 若启用模型流式，只需把分段来源换成真实 delta，前端无需改动。
#    故响应元数据里显式带 `stream_mode="segmented"`，避免把"分段"当成"token 流"。

SSE_SEGMENT_CHARS = 12      # 每段字符数（分段推送粒度）
SSE_SEGMENT_DELAY = 0.02    # 段间延时（秒）—— 仅为视觉节奏，不影响内容


def _sse(event: str, data: dict[str, Any]) -> str:
    """组装一条 SSE 帧（`event:` + `data:` + 空行）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_segments(text: str, size: int = SSE_SEGMENT_CHARS) -> list[str]:
    """把回复切成若干段用于渐进推送（**不改内容、不加删字**）。"""
    body = text or ""
    if not body:
        return []
    return [body[i : i + size] for i in range(0, len(body), size)]


@app.post("/api/h5/cases/{case_id}/messages/stream", dependencies=[Depends(_require_chat_enabled)])
async def h5_post_message_stream(case_id: str, payload: H5Message) -> StreamingResponse:
    """买家发消息 → **SSE 流式**返回（P1.3）。

    事件序列：`status`（确定性进度播报）→ `meta`（结论元数据）→ `delta`×N（分段正文）
    → `done`（收尾）。任一阶段出错 → `error`（明确错误码，不静默中断）。
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    async def _gen():
        # ① 进度播报：**确定性文案**（呈现层），不含判断成分，无需调用模型
        yield _sse("status", {"phase": "thinking", "message": "正在核对订单与物流…"})
        try:
            result = await orchestrator.handle_buyer_message(case_id, text)
        except chat_session.SessionError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.message, "status": exc.status_code})
            return
        except Exception as exc:  # noqa: BLE001 —— 流内异常必须转为 error 事件，不能断在半途
            logger.exception("H5 流式对话失败：%s", exc)
            yield _sse("error", {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"})
            return

        reply_text = str(result.get("reply") or "")
        # ② 元数据：结论相关字段与护栏状态（前端据此渲染卡片 / 提示条）
        yield _sse(
            "meta",
            {
                "case_id": result.get("case_id", case_id),
                "accepted": bool(result.get("accepted")),
                "kind": result.get("kind"),
                "code": result.get("code"),
                "escalated": bool(result.get("escalated")),
                "status": result.get("status"),
                "round": result.get("round"),
                "max_rounds": result.get("max_rounds"),
                "stop_reason": result.get("stop_reason"),
                "tool_calls": len(result.get("tool_audits") or []),
                "gate": result.get("gate"),
                "notice": result.get("notice") or "",   # 接管等"非会话消息"的界面提示
                "stream_mode": "segmented",   # 诚实标注：非 token 级流
            },
        )
        # ③ 分段推送正文
        segments = _split_segments(reply_text)
        for seg in segments:
            yield _sse("delta", {"text": seg})
            if SSE_SEGMENT_DELAY > 0:
                await asyncio.sleep(SSE_SEGMENT_DELAY)
        # ④ 收尾：带上结论对象，供前端渲染"判定依据"折叠区
        yield _sse(
            "done",
            {
                "case_id": result.get("case_id", case_id),
                "chars": len(reply_text),
                "segments": len(segments),
                "decision": result.get("decision"),
                "stream_mode": "segmented",
            },
        )

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 商家售后指挥台（第二步）—— 收件箱 / 指派 / 介入 / 确认 / 驳回
# ============================================================


class ConsoleConfirm(BaseModel):
    """确认判定（可选附注确认人）。"""

    reviewer: str = ""


class ConsoleReject(BaseModel):
    """驳回判定（可选附注理由）。"""

    reason: str = ""


class ConsoleMessage(BaseModel):
    """管理员以人工身份给买家发一条消息。"""

    text: str
    author: str = ""


class ConsoleClose(BaseModel):
    """人工结案（可选附注）。"""

    note: str = ""


@app.get("/api/console/cases", dependencies=[Depends(_require_chat_enabled)])
async def console_list_cases(status: str | None = Query(default=None)) -> dict[str, Any]:
    """收件箱列表（`status` 过滤工单状态；不传返回全部）。"""
    return {"items": await console.list_cases(status)}


@app.get("/api/console/cases/{case_id}", dependencies=[Depends(_require_chat_enabled)])
async def console_get_case(case_id: str) -> dict[str, Any]:
    """单笔详情：工单 + 完整对话历史 + 槽位 + 护栏记录。"""
    try:
        return await console.get_detail(case_id)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/dispatch", dependencies=[Depends(_require_chat_enabled)])
async def console_dispatch(case_id: str) -> dict[str, Any]:
    """指派智能体：标记处理中并跑一轮智能体处理。"""
    try:
        return await console.dispatch(case_id)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/intervene", dependencies=[Depends(_require_chat_enabled)])
async def console_intervene(case_id: str) -> dict[str, Any]:
    """管理员介入（接管），暂停智能体自主（接管后智能体不再自动应答）。"""
    try:
        return await console.intervene(case_id)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/messages", dependencies=[Depends(_require_chat_enabled)])
async def console_post_message(case_id: str, payload: ConsoleMessage) -> dict[str, Any]:
    """管理员以**人工身份**直接回复买家（首次发言即接管）。"""
    try:
        return await console.post_human_message(case_id, payload.text, author=payload.author)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/takeover", dependencies=[Depends(_require_chat_enabled)])
async def console_takeover(case_id: str) -> dict[str, Any]:
    """明确接管：把「已转人工」的工单归属到人工。

    用于修正历史数据里"已转人工、处理者却仍是智能体或为空"的工单。
    """
    try:
        return await console.takeover(case_id)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/resume", dependencies=[Depends(_require_chat_enabled)])
async def console_resume(case_id: str) -> dict[str, Any]:
    """把「已转人工」的工单**退回智能体**继续处理。

    瞬时故障（工具/模型不可用）排除后不必重开工单；买家主动要求人工时返回 409。
    """
    try:
        return await console.resume_agent(case_id)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/close", dependencies=[Depends(_require_chat_enabled)])
async def console_close(case_id: str, payload: ConsoleClose | None = None) -> dict[str, Any]:
    """人工结案：管理员处理完毕后把工单置为已完结。"""
    try:
        return await console.close_case(case_id, note=(payload.note if payload else ""))
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/confirm", dependencies=[Depends(_require_chat_enabled)])
async def console_confirm(case_id: str, payload: ConsoleConfirm | None = None) -> dict[str, Any]:
    """确认智能体的判定（退款/补发/拒绝落地，处置走影子/双闸门）。"""
    try:
        return await console.confirm(case_id, reviewer=(payload.reviewer if payload else ""))
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/api/console/cases/{case_id}/reject", dependencies=[Depends(_require_chat_enabled)])
async def console_reject(case_id: str, payload: ConsoleReject | None = None) -> dict[str, Any]:
    """驳回智能体的判定 → 转人工。"""
    try:
        return await console.reject(case_id, reason=(payload.reason if payload else ""))
    except console.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ============================================================
# 健康检查
# ============================================================


def _ping_db() -> None:
    """同步探活（在线程池中执行，避免阻塞事件循环）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """健康检查：进程存活 + 各依赖可用性。

    **不抛异常**：各依赖状态以字段返回，便于运维与工作台判断"具体哪一项不可用"，
    而不是得到一个笼统的 500。`status=degraded` 表示审计库不可达（判定仍可运行，
    因为审计是旁路；但审计数据会缺失）。
    """
    db_ok, db_error = True, ""
    if settings.audit_db_enabled and settings.mysql_uri:
        try:
            await run_sync(_ping_db)
        except Exception as exc:  # noqa: BLE001 — 探活失败要作为状态返回，不向上抛
            db_ok, db_error = False, f"{type(exc).__name__}: {exc}"

    return {
        "status": "ok" if db_ok else "degraded",
        "audit_db": {
            "enabled": settings.audit_db_enabled,
            "configured": bool(settings.mysql_uri),
            "reachable": db_ok,
            "error": db_error,
        },
        # 模型可用性：总开关与密钥齐备才发起调用（这是"能调用"，不是"已调用"）
        "llm": {
            "enabled": settings.llm_enabled,
            "text_available": text_available(),
            "vision_available": vision_available(),
            # 图片张数上限一并暴露：便于核对"为什么没调用"（上限为 0 时等同关闭视觉）
            "vision_max_images": settings.vision_max_images,
            "vision_include_buyer_note": settings.vision_include_buyer_note,
        },
        # 口径问答：润色需 polish 与总开关同时开启（此时才产生计费调用）
        "qa": {
            "polish_enabled": settings.qa_llm_polish and settings.llm_enabled,
            "max_sources": settings.qa_max_sources,
        },
        "shadow_mode": settings.shadow_mode,
        # 可插拔适配层：**当前实际跑在哪个模式** —— 开发期最该一眼看到的信息。
        # mode=local 表示取数走进程内夹具（演示数据），remote 表示走真实业务 API。
        "data_source": {
            "mode": settings.resolved_data_source,
            "declared": (settings.data_source or "").strip().lower() or "(自动推断)",
            "bases": {
                "order": bool(settings.order_api_base),
                "logistics": bool(settings.logistics_api_base),
                "profile": bool(settings.aftersale_api_base),
            },
        },
        "storage": {
            "backend": (settings.storage_backend or "local").strip().lower(),
            "cos_configured": settings.storage_configured,
        },
        # 演示辅助（**非业务能力**）：任意订单受理是否可用；接入真实业务 API 后 active 自动为 false
        "demo": {
            "any_order": settings.demo_any_order,
            "active": demo_active(),
        },
        "checkpoint": settings.checkpoint_sqlite_path,
        # 对话入口（P1）：护栏参数与成本闸门状态。
        # 成本闸门**始终评估**（即便对话入口关闭），便于运维随时掌握当月等效消耗与是否已暂停。
        "chat": {
            "enabled": settings.chat_enabled,
            "guardrails": {
                "max_rounds": settings.max_rounds,
                "max_msg_chars": settings.max_msg_chars,
                "max_tools_per_round": settings.max_tools_per_round,
                "max_slot_ask": settings.max_slot_ask,
                "session_timeout_hours": settings.session_timeout_hours,
            },
            "cost_gate": (await cost_gate.evaluate()).to_dict(),
        },
    }


# ============================================================
# 开发演示（⚠️ 非业务能力，见 app/dev_seed.py 的边界声明）
# ============================================================


class SeedPayload(BaseModel):
    """演示种子参数。"""

    order_id: str
    scenario: str
    pay_amount: float | None = None  # 不传则用场景建议金额


@app.get("/api/dev/scenarios")
async def dev_scenarios() -> dict[str, Any]:
    """列出可用的演示场景（供工作台下拉预填）。

    已接入真实业务 API 时返回空列表——演示种子不应与真实链路混用。
    """
    if real_api_configured():
        return {"available": False, "reason": "已配置真实业务 API", "items": []}
    return {"available": True, "reason": "", "items": scenario_options()}


@app.post("/api/dev/seed")
async def dev_seed(payload: SeedPayload) -> dict[str, Any]:
    """为本地演示注入取数数据（**仅 mock 模式**）。"""
    if real_api_configured():
        raise HTTPException(
            status_code=409, detail="已配置真实业务 API，不允许注入演示数据"
        )

    spec_amount = scenario_options()
    known = {item["key"] for item in spec_amount}
    if payload.scenario not in known:
        raise HTTPException(
            status_code=400,
            detail=f"未知场景：{payload.scenario}（可用：{sorted(known)}）",
        )

    amount = (
        payload.pay_amount
        if payload.pay_amount is not None
        else suggest_amount(payload.scenario)
    )
    apply_scenario(payload.order_id, payload.scenario, amount)
    return {"order_id": payload.order_id, "scenario": payload.scenario, "pay_amount": amount}


# ============================================================
# 根路径引导 + 前端静态托管
# ============================================================
# 说明：本服务提供 API；界面是 frontend/ 下的静态页与 Vue 构建产物（h5 / console）。
# 为免用户访问根路径撞 404、或纠结 file:// 打开，这里：
#   1) 根路径 `/` 给出引导页（列出各页面入口）；
#   2) 把 frontend/ 挂到 `/ui`，使 `http://127.0.0.1:8000/ui/dist/console.html` 可直接访问
#      （同源、无 CORS 问题）。
#   构建 h5 / console：cd frontend && npm install && npm run build（产物 frontend/dist/）。

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

_ROOT_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>商铺经营Agent</title>
<style>
 body{font:15px/1.9 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:640px;margin:64px auto;padding:0 22px;color:#1f2329}
 h1{font-size:20px;margin-bottom:4px} .ok{color:#15803d;font-size:13px}
 code{background:#f4f5f7;padding:2px 6px;border-radius:4px;font-size:13px}
 a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
 li{margin:8px 0} .muted{color:#8a9099;font-size:13px}
</style></head>
<body>
 <h1>商铺经营Agent · 后端已启动</h1>
 <div class="ok">✓ 服务运行中（本页由后端提供）</div>
 <p>本服务提供 <b>API</b>；界面入口如下（h5 / console 为 Vue 构建产物）：</p>
 <ul>
  <li><b>商铺经营Agent</b>（主界面）：<a href="/ui/seller.html">/ui/seller.html</a></li>
  <li><b>售后指挥台</b>（控制台的「售后」页内嵌的就是它）：<a href="/ui/dist/console.html">/ui/dist/console.html</a></li>
  <li><b>买家 H5</b>：<a href="/ui/dist/h5.html?order_id=DEMO001">/ui/dist/h5.html?order_id=DEMO001</a></li>
  <li><b>买家咨询（本地演示）</b>：<a href="/ui/consultation.html">/ui/consultation.html</a></li>
  <li><b>API 文档</b>：<a href="/docs">/docs</a>　|　<b>健康检查</b>：<a href="/api/health">/api/health</a></li>
 </ul>
 <p class="muted">seller.html 可直接双击打开；h5 / console 需先 <code>cd frontend &amp;&amp; npm run build</code>，再经 <code>/ui/dist/</code> 访问。</p>
</body></html>"""


@app.get("/", include_in_schema=False)
async def root_guide() -> HTMLResponse:
    """根路径引导页（非 API）。"""
    return HTMLResponse(_ROOT_HTML)


class _NoCacheStaticFiles(StaticFiles):
    """静态文件一律 no-cache：免构建静态页迭代频繁，浏览器对 iframe 内嵌页的
    启发式缓存会导致"改了样式页面不变"（本次 console.css 改版即踩中）。
    no-cache 不是不缓存，而是每次使用前回源校验（配合 ETag 命中 304，开销很小）。"""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if _FRONTEND_DIR.is_dir():
    app.mount("/ui", _NoCacheStaticFiles(directory=str(_FRONTEND_DIR), html=True), name="ui")
