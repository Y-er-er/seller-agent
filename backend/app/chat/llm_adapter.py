"""模型适配层 —— 对话内核与具体模型之间的**唯一接口**。

为什么单独一层（三个理由，缺一不可）：

1. **离线可测**：agentic 循环只依赖 `ChatModel` 协议。注入"脚本化桩"即可跑通
   「模型自主选工具 → 多轮往复 → 提交判定 → 生成回复」**全链路，且不产生任何计费调用**——
   自动化回归因此可以零成本长跑。⚠️ 这是**测试策略**，不是"少用模型"的理由：
   真实运行按正常业务调用模型，成本是配置项（见「成本闸门」）。

2. **模型自主的落点**：模型通过返回 `tool_calls` 自主表达"要调哪个工具、传什么参数"；
   循环据此执行并把结果回灌。**调用顺序、调用次数、是否追问、何时提交判定，全由模型决定**；
   代码只做安全边界（轮数 / 单轮工具数 / 成本闸门），**不替模型做决策**。

3. 模型名与密钥来自 `.env`，本文件不硬编码；`LLM_ENABLED=false` 时
   `get_chat_model()` 返回 `None`，调用方必须降级。默认 `true` —— 模型**正常使用**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# 消息角色常量（内部统一格式，对齐 OpenAI/LangChain，便于真机与桩共用）
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class ModelTurn:
    """模型一轮的输出：要么 `tool_calls`（要调工具），要么 `content`（直接回复）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)  # {input_tokens, output_tokens}

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("input_tokens") or 0)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("output_tokens") or 0)


class ChatModel(Protocol):
    """对话模型协议：真机与桩共用。**agentic 循环只依赖这个协议。**"""

    async def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        ...


class ModelUnavailable(RuntimeError):
    """模型不可用（闸门关闭 / 未配置密钥）——调用方应降级而非重试。"""


# ============================================================
# 真机实现（千问 · function calling）
# ============================================================


def _to_langchain_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """把内部消息格式转成 LangChain 消息对象。

    容错：`tool_calls` 的 `arguments` 统一转成 dict（模型偶尔返回 JSON 字符串）。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    out: list[Any] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == ROLE_SYSTEM:
            out.append(SystemMessage(content=content))
        elif role == ROLE_USER:
            out.append(HumanMessage(content=content))
        elif role == ROLE_ASSISTANT:
            raw_calls = msg.get("tool_calls") or []
            calls = []
            for call in raw_calls:
                args = call.get("arguments", {})
                if isinstance(args, str):
                    import json

                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"name": call.get("name", ""), "args": args, "id": call.get("id", "")})
            out.append(AIMessage(content=content, tool_calls=calls))
        elif role == ROLE_TOOL:
            out.append(
                ToolMessage(
                    content=str(content),
                    tool_call_id=msg.get("tool_call_id", ""),
                    name=msg.get("name", ""),
                )
            )
    return out


class QwenChatModel:
    """千问 function-calling 模型。

    ⚠️ 每次 `step()` 都会产生**真实计费**调用；`LLM_ENABLED=false` 时
    `get_chat_model()` 不会返回本类实例，故本类不会在开关关闭时被触发。

    端点由 `ChatTongyi` 按模型名推断（`"vl" in name` 即走多模态端点），本层无法覆盖。
    因此 `.env` 的 `LLM_TEXT_MODEL` **必须是纯文本模型**——配成多模态模型会稳定 400
    （InvalidParameter: url error）。该约束已由 `config_validation` 在启动期点名。
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.llm_text_model
        self._bound: Any = None

    def _ensure_bound(self, tools: list[dict[str, Any]]) -> Any:
        """惰性构建 `bind_tools` 后的可调用对象（未启用时不加载重依赖）。"""
        if self._bound is None:
            from langchain_community.chat_models import ChatTongyi

            base = ChatTongyi(
                model=self.model_name,
                dashscope_api_key=settings.dashscope_api_key,
                temperature=0,
            )
            self._bound = base.bind_tools(tools) if tools else base
        return self._bound

    async def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        try:
            llm = self._ensure_bound(tools)
            result = await llm.ainvoke(_to_langchain_messages(messages))
        except Exception as exc:  # noqa: BLE001 —— 模型层异常统一上抛，由循环决定降级/重试
            # 只记 debug：调用方（agent._model_step）会以业务语义记一条 warning，
            # 这里若是 warning 就会与它重复打印同一条异常，徒增日志噪音。
            logger.debug("模型调用失败：%s: %s", type(exc).__name__, exc)
            raise

        content = getattr(result, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)

        raw_calls = getattr(result, "tool_calls", None) or []
        calls: list[ToolCall] = []
        for call in raw_calls:
            # LangChain 的 tool_call 是 dict；兼容对象属性访问
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
            if isinstance(args, str):
                import json

                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(name=name or "", arguments=args or {}, id=call_id or ""))

        return ModelTurn(content=content, tool_calls=calls, usage=_extract_usage(result))


def _extract_usage(result: Any) -> dict[str, Any]:
    """从 LangChain 返回中提取 token 用量（不同版本字段不同，做兼容取用）。"""
    usage = getattr(result, "usage_metadata", None)
    if isinstance(usage, dict) and (usage.get("input_tokens") or usage.get("output_tokens")):
        return {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}

    meta = getattr(result, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict):
        return {
            "input_tokens": token_usage.get("input_tokens") or token_usage.get("prompt_tokens") or 0,
            "output_tokens": token_usage.get("output_tokens") or token_usage.get("completion_tokens") or 0,
        }
    return {}


# ============================================================
# 工厂
# ============================================================


def chat_model_available() -> bool:
    """对话模型是否可用：**对话入口开关** + **模型总开关** + 已配置密钥。

    两个开关都要开——对话层有自己的独立开关（`CHAT_ENABLED`），
    与判定链路的 `LLM_ENABLED` 分开，便于单独灰度与下线。
    """
    return bool(settings.chat_enabled and settings.llm_enabled and settings.dashscope_api_key)


def get_chat_model() -> ChatModel | None:
    """获取对话模型。**返回 None 表示不可用**——调用方必须降级，不得假设非空。"""
    if not chat_model_available():
        return None
    return QwenChatModel()
