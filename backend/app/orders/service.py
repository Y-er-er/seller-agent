"""订单导入的业务校验与幂等语义。金额均为整数分。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db import order_repo


class OrderError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class ImportLine(BaseModel):
    line_id: str = Field(min_length=1, max_length=64)
    goods_no: str = Field(default="", max_length=64)
    sku_id: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1)
    unit_price_cent: int = Field(ge=0)
    paid_cent: int = Field(ge=0)

    @field_validator("line_id", "goods_no", "sku_id", "title")
    @classmethod
    def strip_text(cls, value: str, info: Any) -> str:
        cleaned = value.strip()
        if not cleaned and info.field_name in ("line_id", "title"):
            raise ValueError("订单行编号和商品名称不能为空")
        return cleaned


class ImportOrder(BaseModel):
    external_order_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    paid_cent: int = Field(ge=0)
    order_amount_cent: int | None = Field(default=None, ge=0)
    import_payload: list[dict[str, str]] | None = None
    placed_at: datetime | None = None
    lines: list[ImportLine] = Field(min_length=1, max_length=100)

    @field_validator("external_order_id", "status")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("订单号和状态不能为空")
        return cleaned

    @field_validator("placed_at")
    @classmethod
    def naive_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("下单时间请使用不带时区的本地时间")
        return value

    @model_validator(mode="after")
    def check_lines(self) -> "ImportOrder":
        ids = [line.line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("同一订单的行编号不能重复")
        if sum(line.paid_cent for line in self.lines) != self.paid_cent:
            raise ValueError("订单实付金额必须等于各订单行实付之和")
        if self.order_amount_cent is None:
            self.order_amount_cent = self.paid_cent
        if self.import_payload is not None and len(self.import_payload) != len(self.lines):
            raise ValueError("原始订单行数量与导入行数量不一致")
        return self


def _canonical(order: ImportOrder) -> tuple[dict[str, Any], str]:
    # 订单行的传入顺序不是业务事实；排序后同一订单重传可稳定命中幂等。
    value = order.model_dump(mode="json")
    value["lines"].sort(key=lambda line: line["line_id"])
    hash_value = value.copy()
    if hash_value["import_payload"] is None and hash_value["order_amount_cent"] == hash_value["paid_cent"]:
        # 旧英文模板的哈希在扩充模型前不含这两个字段；保持旧订单重传幂等。
        hash_value.pop("order_amount_cent")
        hash_value.pop("import_payload")
    raw = json.dumps(hash_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value, hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def import_order(seller_id: str, order: ImportOrder) -> dict[str, Any]:
    value, digest = _canonical(order)
    try:
        return await order_repo.import_order(seller_id, value, digest)
    except order_repo.OrderRepoError as exc:
        raise OrderError(exc.code, exc.message, exc.status_code) from exc


async def create_demo(seller_id: str) -> dict[str, Any]:
    """每个卖家一笔固定的演示订单；重复点击不会生成重复记录。"""
    order = ImportOrder(
        external_order_id="DEMO-ORDER-001", status="paid", paid_cent=9900,
        lines=[ImportLine(line_id="1", title="演示订单商品", quantity=1,
                          unit_price_cent=9900, paid_cent=9900)],
    )
    value, digest = _canonical(order)
    try:
        return await order_repo.create_demo(seller_id, value, digest)
    except order_repo.OrderRepoError as exc:
        raise OrderError(exc.code, exc.message, exc.status_code) from exc


async def list_orders(seller_id: str, *, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return await order_repo.list_orders(seller_id, query=query, limit=limit, offset=offset)


async def get_order(seller_id: str, order_id: int) -> dict[str, Any]:
    result = await order_repo.get_order(seller_id, order_id)
    if result is None:
        raise OrderError("order_not_found", "订单不存在", 404)
    return result
