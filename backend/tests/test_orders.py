"""订单工作台离线验证：校验、稳定幂等键、卖家归属传递。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orders import service  # noqa: E402
from app.assistant import tools_commerce  # noqa: E402
from app.assistant.registry import AssistantContext  # noqa: E402


def payload(**changes):
    data = {
        "external_order_id": " O-1 ", "status": "paid", "paid_cent": 300,
        "lines": [
            {"line_id": "2", "title": "乙", "quantity": 1, "unit_price_cent": 100, "paid_cent": 100},
            {"line_id": "1", "title": "甲", "quantity": 1, "unit_price_cent": 200, "paid_cent": 200},
        ],
    }
    data.update(changes)
    return data


def rejects(data):
    try:
        service.ImportOrder.model_validate(data)
    except ValidationError:
        return True
    return False


async def main():
    assert rejects(payload(paid_cent=301))
    assert rejects(payload(lines=[payload()["lines"][0]] * 2, paid_cent=200))
    assert rejects(payload(external_order_id="   "))
    assert rejects(payload(lines=[{**payload()["lines"][0], "line_id": " "}], paid_cent=100))
    first = service.ImportOrder.model_validate(payload())
    assert first.external_order_id == "O-1"
    assert first.order_amount_cent == 300
    old_shape = first.model_dump(mode="json")
    old_shape.pop("order_amount_cent")
    old_shape.pop("import_payload")
    old_shape["lines"].sort(key=lambda line: line["line_id"])
    old_digest = hashlib.sha256(json.dumps(
        old_shape, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    assert service._canonical(first)[1] == old_digest
    unpaid = service.ImportOrder.model_validate(payload(
        paid_cent=0, order_amount_cent=300,
        lines=[{**line, "paid_cent": 0} for line in payload()["lines"]],
        import_payload=[{"订单号": "O-1"}, {"订单号": "O-1"}],
    ))
    assert unpaid.paid_cent == 0 and unpaid.order_amount_cent == 300
    assert rejects(payload(import_payload=[{"订单号": "O-1"}]))
    reverse = service.ImportOrder.model_validate(payload(lines=list(reversed(payload()["lines"]))))
    assert service._canonical(first)[1] == service._canonical(reverse)[1]

    seen = []
    original_import = service.order_repo.import_order
    original_get = service.order_repo.get_order
    try:
        async def fake_import(seller_id, value, digest):
            seen.append((seller_id, value["external_order_id"], digest))
            return {"order_id": 1, "created": True, "source": "imported"}

        async def fake_get(seller_id, order_id):
            seen.append((seller_id, order_id))
            return None

        service.order_repo.import_order = fake_import
        service.order_repo.get_order = fake_get
        result = await service.import_order("shop-a", first)
        assert result["source"] == "imported" and seen[0][:2] == ("shop-a", "O-1")
        try:
            await service.get_order("shop-b", 1)
        except service.OrderError as exc:
            assert exc.status_code == 404 and seen[1] == ("shop-b", 1)
        else:
            raise AssertionError("不存在的订单未返回 404")
    finally:
        service.order_repo.import_order = original_import
        service.order_repo.get_order = original_get
    try:
        async def fake_private_order(seller_id, order_id):
            return {
                "external_order_id": "O-1", "status": "待付款", "source": "imported",
                "order_amount_cent": 300, "paid_cent": 0, "lines": [],
                "import_payload": [{"联系电话": "13800000000"}],
            }

        tools_commerce.order_repo.get_order = fake_private_order
        result = await tools_commerce._get_order(AssistantContext("shop-a", "test"), 1)
        assert "13800000000" not in result.content
        assert "import_payload" not in result.data["order"]
    finally:
        tools_commerce.order_repo.get_order = original_get
    print("orders: all offline checks passed")


if __name__ == "__main__":
    asyncio.run(main())
