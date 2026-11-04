"""领域模型（Pydantic）。

用途：为规则引擎提供**类型化的事实输入**，并为审计留痕提供结构化载体。
注意：这些模型只描述"事实"，不承载任何判定逻辑——判定一律在 rules 包内。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LogisticsStatus(str, Enum):
    """物流状态。"""

    NOT_SHIPPED = "not_shipped"   # 未发货
    IN_TRANSIT = "in_transit"     # 运输中
    NOT_SIGNED = "not_signed"     # 已到达但未签收
    SIGNED = "signed"             # 已签收


class RefundReason(str, Enum):
    """仅退款原因码（仅枚举本方案需要区分的类别）。"""

    NOT_RECEIVED = "not_received"      # 未收到货
    QUALITY = "quality"                # 质量问题（触发业务红线：转人工）
    WRONG_ITEM = "wrong_item"          # 错发/漏发
    NO_REASON = "no_reason"            # 无理由
    OTHER = "other"


class ClaimType(str, Enum):
    """诉求分类（买家想要的处置方式）。

    第一步只落地 REFUND / RETURN_REFUND / RESHIP；EXCHANGE / PARTIAL_REFUND 预留枚举、P3 再开。
    """

    REFUND = "refund"                      # 仅退款（不退货）
    RETURN_REFUND = "return_refund"        # 退货退款（退货后退款）
    RESHIP = "reship"                      # 补发 / 重发
    EXCHANGE = "exchange"                  # 换货
    PARTIAL_REFUND = "partial_refund"      # 部分退款 / 差价补偿


class RefundRequest(BaseModel):
    """买家提交的一笔售后申请（输入字段，对齐方案 v4 §三）。

    `claim_type` 是「诉求分类」——它决定处置结果的**方向**（退款/补发/换货），
    与 `refund_reason_code`（为什么）正交：同一"错发/漏发"原因，诉求是补发就补发、是退款就退款。
    """

    order_id: str = Field(description="订单号")
    item_category: str = Field(default="", description="商品类目")
    pay_amount: float = Field(ge=0, description="实付金额（元）")
    claim_amount: float = Field(ge=0, description="申请退款金额（元）")
    claim_type: ClaimType = Field(default=ClaimType.REFUND, description="诉求分类（退款/退货退款/补发/换货/部分退款）")
    refund_reason_code: RefundReason = Field(description="退款原因码")
    claim_images: list[str] = Field(default_factory=list, description="买家举证图片（本地路径或 COS key）")
    seller_evidence: str | None = Field(default=None, description="卖家凭证")
    buyer_note: str = Field(default="", description="买家留言（仅供模型理解，不参与判定）")


class OrderItem(BaseModel):
    """订单行 —— **真实电商里，一个订单有多行**（买了 3 种商品就是 3 行）。

    金额用**整数分**：真实接口基本都以分为单位，且 float 在金额上迟早出精度问题。
    """

    line_id: str = Field(default="", description="平台订单行 id（售后按行关联时用）")
    goods_no: str = Field(default="", description="对应卖家控制台的 goods.goods_no")
    sku_id: str = Field(default="", description="平台 SKU（当前仅透传，我们不建 SKU 表）")
    title: str = ""
    quantity: int = Field(default=1, ge=1)
    unit_price_cent: int = Field(default=0, ge=0, description="单价（分）")
    paid_cent: int = Field(default=0, ge=0, description="该行实付（分，含优惠分摊）")


class OrderSnapshot(BaseModel):
    """订单快照（只读事实）。"""

    order_id: str
    item_category: str = ""
    pay_amount: float = 0.0
    signed_at: datetime | None = None
    logistics_status: LogisticsStatus = LogisticsStatus.IN_TRANSIT
    #: 该订单对应的**商品编号**（卖家控制台的 `goods.goods_no`）。
    #:
    #: 为什么需要它：商品是卖家自建的、订单来自外部业务系统，两者**本来毫无关联** ——
    #: 没有这个字段就算不出"这个商品退货率多少"。它是「商品 × 售后」之间**唯一的桥**。
    #: 真实业务里应由订单系统提供；本地夹具按约定注入。
    goods_no: str = ""
    #: ★ **订单行**（一单多商品）。
    #:
    #: 语义约定：**多行订单以 `items` 为准**；上面三个字段（`goods_no` / `item_category` /
    #: `pay_amount`）只在**单行订单**时有意义。保留它们是为了让现有判定链（R1—R11）
    #: 与 25 个测试**零改动** —— 这是向后兼容，不是"两个真相"。
    items: list[OrderItem] = Field(default_factory=list)


class LogisticsInfo(BaseModel):
    """物流信息（只读事实）。"""

    order_id: str
    logistics_status: LogisticsStatus = LogisticsStatus.IN_TRANSIT
    signed_at: datetime | None = None
    traces: list[str] = Field(default_factory=list, description="物流轨迹摘要")


class BuyerProfile(BaseModel):
    """买家画像（只读事实）。"""

    buyer_id: str = ""
    buyer_credit_score: int = Field(default=600, ge=0, le=1000)
    buyer_refund_rate_90d: float = Field(default=0.0, ge=0.0, le=1.0, description="90 天退款率")
    refund_count_90d: int = Field(default=0, ge=0, description="90 天仅退款笔数")


class GoodsSnapshot(BaseModel):
    """**外部商品快照**（只读，见 `GoodsSource` 端口）。

    为什么需要：现在商品是卖家**自建**的 —— 接真机后陈列页要显示**真实商品**，
    否则"经营控制台"看的是一套假数据。

    金额用**整数分**（与 `OrderItem` 一致）；图片**转存到自己的存储**，
    不直接引用平台 CDN（会过期 / 防盗链，且售后统计要长期可查）。
    """

    goods_no: str = Field(description="平台商品编号 —— **同步的幂等键**")
    title: str = ""
    category: str = Field(default="", description="平台类目（多级树扁平后的**最后一级**）")
    price_cent: int = Field(default=0, ge=0, description="售价（分）")
    list_price_cent: int = Field(default=0, ge=0, description="划线价（分）")
    stock: int = Field(default=0, ge=0)
    status: str = Field(default="", description="on_shelf / off_shelf / draft")
    description: str = ""
    images: list[str] = Field(default_factory=list, description="平台图片 URL（同步时转存）")
    updated_at: datetime | None = None


class EvidenceReport(BaseModel):
    """举证核验结果（确定性校验 + 可选的模型辅助信号）。"""

    image_count: int = Field(default=0, description="有效举证图片数量")
    decodable: bool = Field(default=True, description="图片是否全部可解码")
    invalid_reason: str | None = Field(default=None, description="不可用原因")
    has_quality_claim: bool = Field(default=False, description="模型抽出的'质量主张'信号（辅助，不参与阈值）")
    claim_type: str | None = Field(default=None, description="模型抽出的诉求类型（辅助）")
    source: str = Field(default="rule", description="信号来源：rule / llm / llm_disabled")


class RiskReport(BaseModel):
    """风险标记结果。"""

    level: str = Field(default="low", description="low / medium / high")
    flags: list[str] = Field(default_factory=list, description="命中的风险标签")


class ActionRecord(BaseModel):
    """处置动作记录。"""

    action: str = Field(description="实际执行（或影子模式下'本应执行'）的动作")
    executed: bool = Field(default=False, description="是否真正落地执行")
    shadow: bool = Field(default=False, description="本次是否处于影子模式")
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    """审计事件（写入 audit_events 列表，最终落业务审计表）。"""

    stage: str = Field(description="产生事件的节点名")
    at: datetime = Field(default_factory=datetime.now)
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
