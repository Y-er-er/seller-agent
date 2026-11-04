"""客服 API 离线契约：公开入口限制、买家令牌与卖家身份传递。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import service_repo  # noqa: E402
from app.service_chat import routes  # noqa: E402


async def main():
    try:
        routes.BuyerStart(seller_id="default", message="   ")
    except ValidationError:
        pass
    else:
        raise AssertionError("空咨询消息必须拒绝")
    try:
        routes.Reply(title="  ", content="正文")
    except ValidationError:
        pass
    else:
        raise AssertionError("空快捷回复标题必须拒绝")

    seen = []
    old_get, old_append = service_repo.get, service_repo.append
    try:
        async def fake_get(public_id, *, seller_id="", token=""):
            seen.append((public_id, seller_id, token))
            return {"public_id": public_id, "messages": []}

        async def fake_append(public_id, role, text, *, seller_id="", token="", author="", reply_id=None):
            seen.append((public_id, role, seller_id, token, author))
            return {"seq": 2, "role": role, "content": text}

        service_repo.get, service_repo.append = fake_get, fake_append
        await routes.buyer_get("c-1", "buyer-secret")
        assert seen[-1] == ("c-1", "", "buyer-secret")
        await routes.seller_get("c-1", "shop-a")
        assert seen[-1] == ("c-1", "shop-a", "")
        session = routes.SellerSession(token="t", seller_id="shop-a", username="alice", display_name="", thread_id="x")
        await routes.seller_message("c-1", routes.Message(text="您好"), session)
        assert seen[-1] == ("c-1", "human", "shop-a", "", "alice")
    finally:
        service_repo.get, service_repo.append = old_get, old_append
    print("service chat: all offline checks passed")


if __name__ == "__main__":
    asyncio.run(main())
