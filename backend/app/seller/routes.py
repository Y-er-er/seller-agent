"""卖家控制台 API —— **HTTP 形态层**。

只做三件事：解析参数 → 调 `service` → 把业务错误转 HTTP。**不放任何业务规则**。

两个刻意的约定：

1. **静态路径必须声明在参数路径之前**
   （`/goods/bulk`、`/goods/reorder` 要在 `/goods/{goods_id}` 之前）——
   FastAPI 按注册顺序匹配，"bulk" 若落到 `{goods_id}` 会变成 int 转换失败（422）。
2. **删除类动作要求 `confirm=true` 显式确认** —— 单件与批量**一致要求**。
   确认不是前端装饰：没有它，后端直接拒绝执行。这是"人在环"在商品域的落点。
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import goods_repo as repo
from app.goods import service as goods_service
from app.goods.service import GoodsError
from app.ports import get_storage
from app.seller.deps import require_seller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seller", tags=["卖家控制台"])


# ============================================================
# 请求体模型（必须先于使用它们的端点定义）
# ============================================================


class GoodsCreate(BaseModel):
    """新建商品。"""

    title: str
    price: float = 0
    description: str = ""
    cost_price: float = 0
    stock: int = 0
    stock_alert: int = 0
    category: str = ""
    status: str = repo.STATUS_DRAFT


class GoodsPatch(BaseModel):
    """修改商品（**未传的字段不动** —— 靠 `exclude_unset` 区分"没传"与"传了 null"）。"""

    title: str | None = None
    description: str | None = None
    price: float | None = None
    cost_price: float | None = None
    stock: int | None = None
    stock_alert: int | None = None
    category: str | None = None
    status: str | None = None
    pinned: bool | None = None


class BulkPayload(BaseModel):
    """批量动作。"""

    goods_ids: list[int] = Field(default_factory=list)
    action: str
    price_delta: float = 0.0
    confirm: bool = False


class ReorderPayload(BaseModel):
    """陈列编排：按给定顺序落 sort_no。"""

    ordered_ids: list[int] = Field(default_factory=list)


class CopyPayload(BaseModel):
    """文案生成风格。"""

    style: str = "口语"


class CoverPayload(BaseModel):
    """宣传图生成的补充要求（可选）。"""

    hint: str = ""


# ============================================================
# 异常处理（在 main.py 注册）
# ============================================================


async def goods_error_handler(_request: Any, exc: GoodsError) -> JSONResponse:
    """把 `GoodsError` 转成明确的状态码 + 中文原因（不裸抛 500）。"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})


def _fail(exc: GoodsError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ============================================================
# 概览
# ============================================================


@router.get("/overview")
async def get_overview(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """概览看板：商品计数 / 低库存 / 最近操作 / **商品健康度（商品 × 售后联动）**。

    「待处理售后」不在本接口 —— 它属售后域，由工作台另行查询（避免商品域反向依赖售后域）。
    """
    try:
        data = await goods_service.overview(seller_id)
    except GoodsError as exc:
        _fail(exc)

    # 商品健康度由 **API 层**组装：`goods.service` 是商品域内层，
    # 让它去 import 售后统计会造成 service ↔ aftersale_stats 循环依赖。
    try:
        from app.goods import aftersale_stats

        data["health_ranking"] = await aftersale_stats.health_ranking(seller_id, limit=3)
    except Exception as exc:  # noqa: BLE001 —— 健康度是旁路：失败就空着，不能让概览整体挂掉
        logger.warning("商品健康度聚合失败（已降级为空）：%s: %s", type(exc).__name__, exc)
        data["health_ranking"] = []
    return data


# ============================================================
# 批量与编排（⚠️ 静态路径，必须早于 /goods/{goods_id}）
# ============================================================


@router.post("/goods/bulk")
async def bulk_goods(payload: BulkPayload, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """批量上架 / 下架 / 调价 / 删除。

    **删除必须 `confirm=true`**；其余动作不需要（可逆或影响面小）。
    """
    if payload.action == "delete" and not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="批量删除需显式确认：请在请求体中传 confirm=true（界面应先弹二次确认）",
        )
    try:
        return await goods_service.bulk_action(
            seller_id, payload.goods_ids, payload.action, price_delta=payload.price_delta
        )
    except GoodsError as exc:
        _fail(exc)


@router.post("/goods/reorder")
async def reorder_goods(
    payload: ReorderPayload, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """陈列编排：按传入的 id 顺序调整展示顺序。"""
    try:
        return await goods_service.reorder(seller_id, payload.ordered_ids)
    except GoodsError as exc:
        _fail(exc)


# ============================================================
# 商品 CRUD
# ============================================================


@router.get("/goods")
async def list_goods(
    scope: str = Query(default="all", description="shelf=仅上架（陈列视图）/ all=全量（管理视图）"),
    q: str = Query(default=""),
    status: str = Query(default=""),
    low_stock: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    seller_id: str = Depends(require_seller),
) -> dict[str, Any]:
    """商品列表。**陈列与管理共用本接口**，只是 `scope` 不同。"""
    items = await repo.list_goods(
        seller_id, scope=scope, q=q, status=status, low_stock_only=low_stock, limit=limit
    )
    return {"items": items, "total": len(items), "scope": scope}


@router.post("/goods")
async def create_goods(payload: GoodsCreate, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """新建商品（默认草稿）。"""
    try:
        return await goods_service.create_goods(
            seller_id, **payload.model_dump(exclude={"status"}), status=payload.status
        )
    except GoodsError as exc:
        _fail(exc)


@router.get("/goods/{goods_id}")
async def get_goods(goods_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """商品详情（含图片列表）。"""
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise HTTPException(status_code=404, detail=f"商品不存在：{goods_id}")
    return goods


@router.patch("/goods/{goods_id}")
async def patch_goods(
    goods_id: int, payload: GoodsPatch, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """修改商品字段（只改传了的那些）。"""
    fields = payload.model_dump(exclude_unset=True)
    try:
        return await goods_service.update_goods(seller_id, goods_id, **fields)
    except GoodsError as exc:
        _fail(exc)


@router.delete("/goods/{goods_id}")
async def delete_goods(
    goods_id: int,
    confirm: bool = Query(default=False, description="删除不可逆，必须显式 confirm=true"),
    seller_id: str = Depends(require_seller),
) -> dict[str, Any]:
    """删除商品（连图片一起消失，不可撤销）。"""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="删除不可逆，需显式确认：请带 confirm=true（界面应先弹二次确认）",
        )
    try:
        return await goods_service.delete_goods(seller_id, goods_id)
    except GoodsError as exc:
        _fail(exc)


# ============================================================
# 图片
# ============================================================


@router.post("/goods/{goods_id}/media")
async def upload_media(
    goods_id: int, file: UploadFile = File(...), seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """上传商品图片（首张自动设为主图）。

    存储走 `ObjectStorage` 端口：开发期为本地目录，生产可切 COS，**本端点无需改动**。
    """
    from pathlib import Path

    raw_name = Path(file.filename or "").name      # 只取文件名，剥掉任何路径成分
    data = await file.read()
    try:
        return await goods_service.attach_media(
            seller_id, goods_id,
            filename=raw_name, data=data, content_type=file.content_type or "",
        )
    except GoodsError as exc:
        _fail(exc)


@router.delete("/goods/{goods_id}/media/{media_id}")
async def delete_media(
    goods_id: int, media_id: int, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """删除一张商品图片（行与文件一起删，主图被删则顺延到下一张）。"""
    try:
        return await goods_service.detach_media(seller_id, goods_id, media_id)
    except GoodsError as exc:
        _fail(exc)


# ============================================================
# AI 生成（⚠️ 两个端点都会产生**真实计费**调用）
# ============================================================


@router.post("/goods/{goods_id}/ai/copy")
async def ai_generate_copy(
    goods_id: int, payload: CopyPayload | None = None, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """生成一版**候选**商品文案。

    只生成、不写库 —— 卖家点「采纳」才由前端调 `PATCH` 写入（正常留痕）。
    ⚠️ 会触发真实模型调用。
    """
    # 延迟导入：`app.goods.ai` 会拉起 langchain 重依赖，不必拖慢启动
    from app.goods import ai as goods_ai

    try:
        return await goods_ai.generate_copy(
            seller_id, goods_id, (payload.style if payload else "口语")
        )
    except GoodsError as exc:
        _fail(exc)


@router.post("/goods/{goods_id}/ai/price")
async def ai_suggest_price(goods_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """给出定价建议（模型给区间与理由，**代码校验边界**）。

    ⚠️ 会触发真实模型调用；模型输出不可用时回落到**规则估算**，并在 `source` 字段标明
    （`model` / `rule_fallback`），界面据此如实标注来源。
    """
    from app.goods import ai as goods_ai

    try:
        return await goods_ai.suggest_price(seller_id, goods_id)
    except GoodsError as exc:
        _fail(exc)


@router.post("/goods/{goods_id}/ai/cover")
async def ai_generate_cover(
    goods_id: int, payload: CoverPayload | None = None, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """为商品生成宣传图（**真实计费**：1 张图 + 1 次文本调用）。

    ⚠️ 需先在阿里云百炼**开通图像模型**并配置 `IMAGE_MODEL`（如 `wanx2.1-t2i-turbo`）；
    未开通/未配置时**明确报错**，不假装能生成。

    生成的图会**立刻转存**到自己的对象存储（官方临时 URL 约 5 分钟过期），
    并登记为商品图片 —— 首张自动成为主图。
    """
    from app.goods import image_ai

    try:
        return await image_ai.generate_cover(
            seller_id, goods_id, hint=(payload.hint if payload else "")
        )
    except GoodsError as exc:
        _fail(exc)


@router.get("/goods/{goods_id}/aftersale-stats")
async def goods_aftersale_stats(
    goods_id: int, seller_id: str = Depends(require_seller)
) -> dict[str, Any]:
    """该商品的售后统计（**商品 × 售后联动**）。

    返回工单数 / 退货率 / 判定结果分布 / 转人工原因分布 / 最近工单。
    **无模型参与**，纯确定性聚合；没有关联工单时会带 `note` 说明原因，
    而不是返回一堆 0 让人误以为"这商品很健康"。
    """
    from app.goods import aftersale_stats

    try:
        return await aftersale_stats.stats_for_goods(seller_id, goods_id)
    except GoodsError as exc:
        _fail(exc)


@router.get("/uploads/{name:path}")
async def serve_upload(name: str) -> Response:
    """读取上传的图片。

    ⚠️ **一律经 `ObjectStorage` 端口读取** —— 路径穿越校验在适配器内完成；
    路由层不得自己拼文件路径，否则会绕过校验。
    """
    data = await get_storage().get(name)
    if data is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


# ============================================================
# 操作日志
# ============================================================


@router.get("/logs")
async def list_logs(
    actor: str = Query(default="", description="human / ai / system；留空为全部"),
    target_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    seller_id: str = Depends(require_seller),
) -> dict[str, Any]:
    """操作日志（人工与 AI 同等留痕）。"""
    items = await repo.list_operations(seller_id, actor=actor, target_id=target_id, limit=limit)
    return {"items": items, "total": len(items)}


@router.post("/logs/{log_id}/undo")
async def undo_log(log_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    """撤销某次可撤销的操作（按 before 快照回滚）。"""
    try:
        return await goods_service.undo_operation(seller_id, log_id)
    except GoodsError as exc:
        _fail(exc)
