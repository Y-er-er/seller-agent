"""AI 助手的**工具注册表** —— 阶段二的核心机制。

三条设计要点：

1. **加工具 = 加一个 `ToolSpec`**，不改循环、不改 API。
   这是"**一套内核挂多套工具**"的落点：商品的工具、陈列的工具、售后的工具
   都注册到同一张表，助手自然就会用（见 `docs/设计_智能体能力地图_v1.md` §五）。

2. **破坏性由注册表判定，不由模型自觉。**
   每个工具自带 `destructive` 标记；模型说"这个操作很安全"不算数。
   标记为破坏性的工具**不会被执行**，而是落成待确认动作（见 `app/assistant/actions.py`）。

3. **没有描述的破坏性工具注册不进来**：破坏性动作必须有 `summary_template`，
   否则界面上那张确认卡就无话可说 —— 在注册期直接拒绝，而不是等到运行时才发现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# 工具所属域（用于能力面板分组，也用于按界面上下文裁剪工具集）
DOMAIN_GOODS = "goods"
DOMAIN_DISPLAY = "display"
DOMAIN_AFTERSALE = "aftersale"
#: 运维域：任务 / 操作日志 / 撤销 —— 跨域的**可观测与补救**手段
DOMAIN_OPS = "ops"
#: 知识域：卖家自建资料的检索 —— 不是"业务动作"，是**作答依据的来源**
DOMAIN_KNOWLEDGE = "knowledge"
DOMAIN_ORDERS = "orders"
DOMAIN_SERVICE = "service"

DOMAIN_LABELS = {
    DOMAIN_GOODS: "商品",
    DOMAIN_DISPLAY: "陈列",
    DOMAIN_AFTERSALE: "售后",
    DOMAIN_OPS: "运维",
    DOMAIN_KNOWLEDGE: "知识库",
    DOMAIN_ORDERS: "订单",
    DOMAIN_SERVICE: "客服",
}


class AssistantError(Exception):
    """助手业务错误。API 层据 `status_code` 转 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class AssistantContext:
    """执行上下文：谁在操作、在哪个线程、界面上正看着什么。"""

    seller_id: str
    thread_id: str
    view: str = ""                 # 界面当前模块（overview/gallery/admin/audit/aftersale）
    selected_goods_no: str = ""    # 界面当前选中的商品编号（供"这件商品"指代）


@dataclass
class ToolResult:
    """工具执行结果。`content` 回灌给模型，`data` 供审计与界面使用。"""

    content: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass(frozen=True)
class ToolSpec:
    """一个可被助手调用的工具。"""

    name: str
    domain: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]
    destructive: bool = False
    #: **计费**动作（生成宣传图 / 文案 / 定价建议）：会花钱，但**不改变数据**。
    #: 与 `destructive` 同样要卖家点头，只是确认卡上的理由不同
    #: （"会产生费用"而不是"不可撤销"）—— 成本可见 + 需点头，与本项目对计费的一贯态度一致。
    billable: bool = False
    #: 破坏性动作的一句话说明模板（用入参格式化）。**需要确认的工具必须提供其一**。
    summary_template: str = ""
    #: 需要按入参做值映射（如 action 枚举→中文）时用这个，优先于 summary_template
    summary_builder: Callable[[dict[str, Any]], str] | None = None
    #: 影响明细（界面确认卡展开显示，如"将删除 3 件商品及 5 张图片"）；可为空
    impact_builder: Callable[[AssistantContext, dict[str, Any]], Awaitable[str]] | None = None
    #: **失败处理策略**：`retryable`（只读，可自动重试）/ `degrade`（有回落）/
    #: `fail_fast`（写操作，失败即报）。留空则按命名约定推断（见 `failure_policy`）。
    policy: str = ""
    #: **幂等身份键**：声明"哪些参数决定这是不是同一件事"。
    #: 留空 = 全部参数参与比对（严格）；指定字段 = 只比这些字段（宽松）。
    #: 例如 `generate_copy` 对同一商品只该有一张待确认卡 → `("goods_no",)`。
    idempotent_by: tuple[str, ...] = ()
    #: **确认卡上的补充说明**：告诉卖家"点确认之后会发生什么、去哪看结果"。
    #: 只写**卖家需要知道的期待值**（异步？多久？会不会改数据？），不写技术细节。
    #: 实测踩到：确认生图后卡片消失、任务中心还没刷新，卖家以为"点了没反应"。
    confirm_note: str = ""

    def identity_key(self, args: dict[str, Any]) -> str:
        """幂等身份键 —— 判断"是不是同一件事"，而不是"参数是否完全一样"。

        ⚠️ 为什么不能只比全参数：模型两次调用**可能传不同的参数组合**
        （一次带 `style`、一次不带），全参数比对会判成"两件事" → **重复建卡**。
        实测：同一商品出现两张「生成文案」确认卡，而它们本质是同一件事。
        """
        payload = args or {}
        keys = self.idempotent_by or tuple(sorted(payload.keys()))
        return json.dumps(
            {k: payload.get(k) for k in keys}, sort_keys=True, ensure_ascii=False
        )

    @property
    def failure_policy(self) -> str:
        """失败策略。显式声明优先；否则 **`list_` / `get_` 开头视为只读 → 可重试**。

        用命名约定而不是给每个工具手工标注：命名本身已经表达了"只读"，
        再标一遍是重复信息，而且容易漏标（漏标 = 悄悄失去重试保护）。
        """
        if self.policy:
            return self.policy
        return "retryable" if self.name.startswith(("list_", "get_")) else "fail_fast"

    @property
    def needs_confirm(self) -> bool:
        """执行前是否需要卖家点头：**不可逆**（destructive）或**会产生费用**（billable）。"""
        return self.destructive or self.billable

    @property
    def confirm_kind(self) -> str:
        """确认理由的类型，供界面选措辞。"""
        if self.destructive:
            return "destructive"
        return "billable" if self.billable else ""

    def to_model_spec(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 格式（喂给 `bind_tools`）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe_action(self, args: dict[str, Any]) -> str:
        """把入参渲染成一句人话，用于确认卡主文案。"""
        payload = args or {}
        if self.summary_builder is not None:
            try:
                return self.summary_builder(payload)
            except Exception:  # noqa: BLE001
                pass
        if self.summary_template:
            try:
                return self.summary_template.format(**payload)
            except (KeyError, IndexError, ValueError):
                pass
        # 渲染失败时退回工具描述 —— 绝不因"文案渲染失败"而放行动作
        return f"{self.description.split('。')[0]}（{self.name}）"


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """注册一个工具。命名冲突或**需要确认的工具缺说明文案**时，**注册期即报错**。

    为什么要在这里拦：需要确认的动作没有一句人话可展示，界面上的确认卡就无话可说 ——
    这种缺陷在注册期暴露最便宜，等到运行时才发现就要翻三层日志。
    说明文案有两条路径：静态 `summary_template` 或动态 `summary_builder`，**有其一即可**。
    """
    if spec.name in _REGISTRY:
        raise ValueError(f"工具名重复：{spec.name}")
    if spec.needs_confirm and not (spec.summary_template or spec.summary_builder):
        raise ValueError(
            f"需要确认的工具（destructive / billable）必须提供 summary_template 或 "
            f"summary_builder（确认卡需要它）：{spec.name}"
        )
    # 有副作用的工具若被标成 retryable，重试就等于重复执行（计费工具还会重复计费）
    if spec.failure_policy == "retryable" and spec.needs_confirm:
        raise ValueError(
            f"有副作用的工具不能标 retryable（重试会导致重复执行）：{spec.name}"
        )
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec | None:
    """按名取工具；不存在返回 None（调用方据此拒绝执行，而不是"试试看"）。"""
    return _REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def model_specs(domains: list[str] | None = None) -> list[dict[str, Any]]:
    """给模型看的工具清单（可按域裁剪）。"""
    specs = all_specs()
    if domains:
        specs = [s for s in specs if s.domain in domains]
    return [s.to_model_spec() for s in specs]


def capabilities() -> list[dict[str, Any]]:
    """能力面板数据：按域分组，标出哪些需要确认。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in all_specs():
        grouped.setdefault(spec.domain, []).append({
            "name": spec.name,
            "label": spec.description.split("。")[0],
            "destructive": spec.destructive,
        })
    return [
        {"domain": domain, "label": DOMAIN_LABELS.get(domain, domain), "tools": tools}
        for domain, tools in grouped.items()
    ]
