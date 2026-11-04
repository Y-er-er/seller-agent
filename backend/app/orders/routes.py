"""卖家订单工作台 API。此处没有平台订单写操作。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.order_repo import OrderRepoError
from app.orders import service
from app.orders.service import ImportOrder, OrderError
from app.seller.deps import require_seller

router = APIRouter(prefix="/api/seller/orders", tags=["商铺经营Agent · 订单"])


def _fail(exc: OrderError | OrderRepoError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post("/import")
async def import_order(payload: ImportOrder, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await service.import_order(seller_id, payload)
    except (OrderError, OrderRepoError) as exc:
        _fail(exc)


@router.post("/demo")
async def create_demo(seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await service.create_demo(seller_id)
    except (OrderError, OrderRepoError) as exc:
        _fail(exc)


@router.get("")
async def list_orders(
    query: str = Query(default="", max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    seller_id: str = Depends(require_seller),
) -> dict[str, Any]:
    try:
        return await service.list_orders(seller_id, query=query.strip(), limit=limit, offset=offset)
    except OrderRepoError as exc:
        _fail(exc)


@router.get("/{order_id}")
async def get_order(order_id: int, seller_id: str = Depends(require_seller)) -> dict[str, Any]:
    try:
        return await service.get_order(seller_id, order_id)
    except (OrderError, OrderRepoError) as exc:
        _fail(exc)
