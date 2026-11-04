"""AI 助手的对话循环。

形态沿用 `app/chat/agent.py`（guard → model_step → tool_step → guard），但更精简：
助手没有成本闸门、没有转人工，只有两道边界 —— **步数上限**与**工具预算**。

**谁在决策**：模型自主决定调哪个工具、传什么参数、调几次、何时收尾。
代码只做三件事：

1. **破坏性工具转成待确认**（交给 `actions.create_pending`），**不执行**；
2. **未知工具直接拒绝**（即使模型幻觉出一个工具名，也执行不了）；
3. 步数与预算收口（防止无限循环）。

⚠️ 每轮都会产生**计费**调用；助手一轮可能触发多次工具调用 → 多次计费。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from app.assistant import actions, registry, tools_aftersale, tools_commerce, tools_goods, tools_knowledge, tools_ops
from app.assistant.registry import AssistantContext
from app.chat import cost_gate
from app.chat.llm_adapter import get_chat_model
from app.config import PROJECT_ROOT, settings
from app.db import assistant_repo as repo

logger = logging.getLogger(__name__)

#: 单轮最多几次模型往返（模型⇄工具）。
#: 从 6 提到 12：「查商品 → 改价 → 上架 → 生图 → 写文案 → 报告」这类链，6 步放不下。
MAX_STEPS = 12
#: 单轮最多执行几个工具。
#: 从 8 提到 24（2026-10-21 实测）：「增加 4 个商品 + 文案 + 宣传图 + 上架」一句要求
#: ≈ 2 次查询 + 4 次建品 + 9 次待确认动作 ≈ 15+ 个工具，8 次在「建完品」那一步就打穿，
#: 后续所有确认卡请求都被拦截 → 一张卡都没建、回复还承诺「马上接着处理」。
#: 预算的作用是防失控循环，不是限制正常批量 —— 真实计费由调用次数决定，不由上限决定。
DEFAULT_TOOL_BUDGET = 24
#: 送进上下文的历史消息条数（只取卖家/助手文本，不含工具过程）
HISTORY_LIMIT = 40

# ---- 「嘴说-事实」一致性守卫 ----
# 实测**两次**复发（2026-10-20 11:09 又一次）：模型回复"系统已经弹出了确认提示 /
# 已发起生成请求"，但一轮下来一次工具都没调，pending_action 表里什么都没有 ——
# 卖家界面上永远等不来那张卡。system prompt 第 5 条（含"实测踩过"警示）只能威慑、
# 拦不住，所以必须在**循环外做事后校验**：回复里出现下面这些"声称已发起"的说法、
# 而系统里没有任何待确认项 → 判定幻觉，追加一轮有界纠正，逼它真正调工具。
#: 只收**声称式**短语；故意不收「确认卡」「确认提示」这类中性词 ——
#: 卖家问"确认卡是什么"时模型讲解功能也算命中，会造成无谓的纠正轮。
_CLAIM_MARKERS: tuple[str, ...] = ("已弹出", "弹出了确认", "已生成待确认", "已发起", "已提交")
#: 纠正轮的步数上限（小而够：通常 1 次工具调用就能翻正）
_FIX_STEPS = 3


def _has_claim(reply: str) -> bool:
    """回复里是否出现「声称已发起动作」的说法（详见 _CLAIM_MARKERS 注释）。"""
    return any(marker in reply for marker in _CLAIM_MARKERS)


# ---- 「后台正在做」也必须由事实支撑（2026-10-20 实测）----
# 实测：卖家问「给电竞椅生成文案、以及宣传图」，模型回了「宣传图也在后台生成中」，
# 但**这一轮它一次工具都没调用**，ops_task 里也没有任何电竞椅的任务 ——
# 卖家去任务中心翻，什么都没有，于是问「显示在后台生成，为什么任务中没有显示」。
# 来源是它自己上一轮说过的话（"还在等你确认"），被这一轮复述成了"在生成中"。
# 与「已发起」同源：**状态类陈述不能由模型记忆决定**。两道防线：
#   ① 系统提示里注入**实时任务事实**（无任务就明说"没有"，从源头掐掉记忆复述）；
#   ② 收尾核对：说了"在后台生成"而系统里确实没有任务 → 纠正轮逼它真去调工具。
#: 只收**正在进行**的说法；「确认后会生成」这类未来时不算（后缀是"会生成"）。
_BACKGROUND_MARKERS: tuple[str, ...] = (
    "后台生成中", "后台处理中", "正在后台", "正在生成", "生成中", "处理中", "排队中",
)


#: 预算打穿后由**代码**补在回复末尾的事实（等价"剩余项由代码说"那条纪律）。
#: 2026-10-21 实测：预算 8 被「查 + 建 4 品」用光后，模型只能回一句
#: 「我马上接着为您处理」—— 轮次结束后什么都不会自动发生，卖家就干等那张
#: 永远不弹的卡。预算打穿后纠正轮也调不了工具（共用同一份额度），所以不再
#: 走纠正轮，直接由代码说出事实并给出可操作的出路。
_BUDGET_FACT_NOTE = (
    "\n\n（系统核对）本轮工具调用已达上限，上面说「接着处理」的那几步**并没有开始**"
    "——发「继续」我就接着把剩下的做完。"
)

#: 「未来承诺」的说法（2026-10-21 实测第三种「嘴说-事实」脱节）：
#: 「已发起 / 已弹出」是**过去时**（守卫 A），「正在生成中」是**进行时**（守卫 B），
#: 「我马上接着为您处理」是**将来时** —— 轮次一结束它就注定落空，却哪道守卫都不命中。
#: 只收**承诺接着做**的短语；「稍后需要您确认」（卡已建时）不算 —— 那是事实。
_PROMISE_MARKERS: tuple[str, ...] = (
    "马上接着", "接着为您处理", "马上为您处理", "接下来我会", "接下来我将",
    "随后我会", "稍后我会", "我接着处理",
)


def _has_promise(reply: str) -> bool:
    """回复里是否承诺「这一轮稍后还会接着做」（而轮次结束什么都不会自动发生）。"""
    return any(marker in reply for marker in _PROMISE_MARKERS)


#: 未来承诺的纠正指令：要么现在就调工具做完，要么如实交代并请卖家发「继续」
_PROMISE_CORRECTION_NOTICE = (
    "（系统校验，不是卖家发言）你上一条回复承诺「马上接着处理 / 稍后会处理」某些步骤，"
    "但对话在你说完这句话后就**结束了** —— 没有任何后台机制会替你继续，"
    "卖家会一直等那张永远不会弹出的卡片。这是「嘴上执行」的变体，被明令禁止。\n"
    "现在二选一：\n"
    "1. 本轮还能调工具 → **立刻调用**对应工具把剩余步骤做完"
    "（生成文案 → generate_copy；生成宣传图 → generate_cover；上架 → 对应上架工具），"
    "需要确认的会被系统转成确认卡；\n"
    "2. 做不完 → 如实告诉卖家哪几步已完成、哪几步还没开始，请他**回复「继续」**，"
    "下一轮你会接着做。\n"
    "**绝不要承诺你这一轮不会执行的动作。**"
)
#: 纠正轮也没救回来（还在承诺、也没建卡）→ 由代码说出事实
_PROMISE_FACT_NOTE = (
    "\n\n（系统核对）上面说「接着处理」的步骤其实**还没有开始** —— 发「继续」我就接着做完。"
)


def _has_background_claim(reply: str) -> bool:
    """回复里是否声称「有任务正在后台跑」。"""
    return any(marker in reply for marker in _BACKGROUND_MARKERS)


#: 模型调用失败的现场落盘文件（只在出错时追加，正常路径不碰磁盘）
_MODEL_ERROR_LOG = PROJECT_ROOT / "data" / "assistant_model_error.log"


def _skeleton(messages: list[dict[str, Any]]) -> str:
    """把请求消息压成一行骨架：角色、是否带 tool_calls、tool_call_id、内容长度。

    为什么只记骨架不记原文：判断 400 的依据是**结构**（角色是否交替、带 tool_calls 的
    assistant 后面有没有配对的 tool 消息、id 是否为空），原文动辄上万 token，写进去反而看不清。
    """
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "?")
        seg = role
        calls = msg.get("tool_calls") or []
        if calls:
            ids = [str(c.get("id") or "") or "EMPTY" for c in calls]
            seg += f"[calls={len(ids)} ids={','.join(ids)}]"
        if role == "tool":
            seg += f"[id={msg.get('tool_call_id') or 'EMPTY'}]"
        seg += f"(len={len(str(msg.get('content') or ''))})"
        parts.append(seg)
    return " → ".join(parts)


def _dump_model_failure(
    ctx: AssistantContext, exc: BaseException, messages: list[dict[str, Any]]
) -> None:
    """模型调用失败时把现场追加到落盘文件。

    2026-10-21 实测痛点：卖家连问两次只拿到「调用模型时出错了」，而原来只有
    `logger.warning` 走 stdout —— 终端一滚屏，原因就永远消失，事后只能靠 `llm_usage`
    里"这两次一条记录都没有"反推出「请求在计费前就被拒了」，仍不知道是 400 还是 429。
    """
    try:
        _MODEL_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _MODEL_ERROR_LOG.open("a", encoding="utf-8") as fp:
            fp.write(
                f"[{stamp}] thread={ctx.thread_id} seller={ctx.seller_id} "
                f"model={settings.llm_text_model} {type(exc).__name__}: {str(exc)[:1500]}\n"
                f"  messages: {_skeleton(messages)}\n"
            )
    except Exception as log_exc:  # noqa: BLE001 —— 写日志失败绝不能影响这轮回复
        logger.debug("写模型失败现场失败（忽略）：%s", log_exc)


def _ensure_call_ids(turn: Any) -> None:
    """确保每个 tool_call 都有**非空 id**。

    端点的 function calling 要求 tool 消息的 `tool_call_id` 能配对回 assistant 的
    `tool_calls`；模型偶尔不给 id（返回空串），空 id 的 ToolMessage 会被判成 400 ——
    而这种失败**不产生 token**，在 `llm_usage` 里查不到任何痕迹，事后只剩一句
    「调用模型时出错了」。就地补一个，把这类失败在源头掐掉。
    """
    for index, call in enumerate(getattr(turn, "tool_calls", None) or []):
        if not str(getattr(call, "id", "") or "").strip():
            call.id = f"call_{uuid.uuid4().hex[:12]}_{index}"


async def _unfinished_count(seller_id: str) -> int:
    """当前"排队中/执行中"的后台任务数 —— 状态类陈述的**唯一事实来源**。

    抽成模块级函数是为了**测试可替换**（离线跑单测时不连库）。
    查不到按 0 处理：宁可多纠一次"其实没有任务"，也不要放行一句无中生有的"在生成中"。
    """
    from app.db import task_repo

    try:
        return await task_repo.count_unfinished(seller_id)
    except Exception as exc:  # noqa: BLE001 —— 读任务数失败不该影响这轮对话
        logger.debug("读取进行中任务数失败（按 0 处理）：%s", exc)
        return 0

#: 纠正轮喂给模型的指令（伪装成卖家消息会污染历史，所以开头就声明是系统校验）
_CORRECTION_NOTICE = (
    "（系统校验，不是卖家发言）你上一条回复声称已经发起了某个操作 / 弹出了确认提示，"
    "但本轮你**一次对应的工具都没有调用**，系统里也**不存在任何待确认项** —— "
    "卖家界面上什么都不会出现。这是「嘴上执行」，是被明令禁止的行为。\n"
    "请**现在就真正调用对应工具**完成卖家刚才的要求"
    "（生成文案 → generate_copy；生成宣传图 → generate_cover；定价建议 → suggest_price；"
    "删除/改数据 → 对应的删除/更新工具）。工具会把动作转成待确认项并返回编号，"
    "然后你再如实转达「已请卖家确认」。\n"
    "如果卖家刚才的要求其实**不需要任何工具**（纯咨询），请直接回答，"
    "并且不要再提「已发起 / 已弹出」这类说法。"
)

VIEW_LABELS = {
    "overview": "概览",
    "gallery": "商品陈列",
    "admin": "商品管理",
    "audit": "操作日志",
    "aftersale": "售后",
}


#: 「后台任务」类的纠正指令：与上面那条不同 —— 这里要的是**先把事实查清**
_BACKGROUND_CORRECTION_NOTICE = (
    "（系统校验，不是卖家发言）你上一条回复说「在后台生成 / 正在处理」，"
    "但系统里**没有任何正在执行的后台任务**，本轮你也**没有调用对应的工具** —— "
    "卖家去「任务」页只会看到一片空，会以为你骗他。\n"
    "请按这个顺序处理：\n"
    "1. 先调用 `list_tasks` 看有没有**刚跑完 / 刚失败**的任务（有就如实说它的状态，不要重提）；\n"
    "2. 确实一条都没有 → 调用 `generate_cover` 把生图提交成待确认项；\n"
    "3. 然后如实说明：**图还没有开始生成**，需要他点一下确认。\n"
    "⚠️ 不要再用「在后台生成中 / 正在处理」这种说法描述**还没发生**的事。"
)
#: 纠正轮也没救回来时的兜底：由**代码**说出事实（等价于"剩余项由代码说"那条纪律）
_BACKGROUND_FACT_NOTE = (
    "\n\n（系统核对）现在**没有正在执行的后台任务** —— 上面说的「在后台生成」并没有发生，"
    "这张图还没开始生成。要我重新发起吗？"
)


def build_system_prompt(ctx: AssistantContext, *, unfinished_tasks: int | None = None) -> str:
    """助手系统提示。含**界面上下文注入** —— 这是"懂上下文"与"只会聊天"的分界。

    `unfinished_tasks`：当前"排队中/执行中"的后台任务数，由**调用方实时查库**传进来
    （见 `_unfinished_count`）。为什么要注入：模型对自己上一轮说过的话记得很清楚，
    对"那件事到底跑没跑起来"一无所知 —— 不给它事实，它就会把"还在等你确认"
    复述成"在后台生成中"（2026-10-20 实测）。
    """
    lines = [
        "你是商铺经营Agent里的 AI 助手，帮卖家打理「商品」「陈列」「售后」三块事务。",
        "",
        "工作方式：",
        "1. 先弄清意图再动手；信息不足时先问清楚，不要凭猜测改数据。",
        "2. ⚠️ **凡涉及商品或工单「当前状态」的问题（有哪些商品、什么价格、什么状态、多少库存、"
        "有哪些工单），都必须先调用工具实时查询** —— 卖家随时可能改价、上架、删商品，"
        "对话历史里的数据**随时可能已过期**，不得凭历史或记忆回答。"
        "即使同一个问题刚刚问过，也要重新查。",
        "3. 一次可以调用多个工具；工具结果要**如实转达**，不要编造。"
        "⚠️ **卖家一次要求多件事时（如「生成文案并上架」「配张图然后上架」），要把它们做完**，"
        "不要只做一半就停下来汇报。"
        "⚠️ 其中需要确认的步骤（不可逆 / 计费）会被系统**合并成一张确认卡**，"
        "卖家点一次就全部执行 —— 所以你**不要**逐步说「已发起第 1 步 / 第 2 步」，"
        "只说明「共几步需要确认」即可。"
        "⚠️ 若系统提示本轮工具调用额度已用完：**如实说明**哪些已完成、哪些还没开始，"
        "请卖家回复「继续」接着做 —— **绝不承诺「马上接着处理 / 稍后处理」**："
        "你的回复说完这轮就结束了，没有任何机制会替你继续，卖家只会干等。",
        "4. 工具返回失败时，把真实原因告诉卖家，不要含糊其辞或换个说法掩饰。",
        "5. ⚠️ **动作类请求必须先调用工具，不能只用嘴说**：删除、批量改动、工单归属变更，"
        "以及生成宣传图 / 文案 / 定价建议 —— **都要先调用对应工具**，系统会自动把它转成"
        "「待卖家确认」让卖家点。**不要自己说「已弹出确认提示」**："
        "**不调用工具就什么都不会发生**（实测踩过：模型说已弹出，实际没有任何确认项）。"
        "工具返回「已生成待确认项 #N」之后，你转达「已请他确认」即可，绝对不要说「已经完成」。"
        "⚠️ **收尾必须逐条对照卖家最初的要求**：说明哪几步真做了、哪几步还在等确认、哪几步没做成。"
        "**只要还有待确认项，就不能说「全部完成 / 都已办妥」** —— 说了会由系统在校验时纠正。"
        "⚠️ 系统会在你回复后**核对这一条**：回复里说「已发起 / 已弹出」"
        "但系统里没有任何待确认项，会被系统打回、要求你真正调用工具。",
        "6. 回复简洁口语化，面向卖家；不要暴露内部工具名、参数格式或技术细节。",
        "7. 商品编号形如 G-0001。卖家说「这件商品」「它」时，指的是当前界面上选中的那件。",
        "8. **写文案用 `generate_copy` 一步完成**：卖家要「更新 / 写入文案」时用 "
        "`generate_copy(apply=true)`，它会生成后**直接写入商品详情**；"
        "只有卖家只是「先看看候选」时才用 `apply=false`。"
        "**不要**生成后停在候选、或再单独调 `update_goods` 写回 —— 那是把一步拆成两步，"
        "批量时还容易被额度拦断。",        "9. **新建商品时卖家没说类目** → **先调用 `list_categories` 看现有类目**："
        "能归入就**沿用完全相同的名字**（已有「厨房小电」就别写「厨房电器」，近义词会让类目碎片化）；"
        "确实都归不进去，才新建一个贴切的类目名。",
        "10. **工具返回「内部错误」时只说人话**：告诉卖家「这件事没做成、可以怎么补救」，"
        "**绝对不要念技术报错原文**（`TypeError` / 函数名 / 堆栈）—— 那是给开发看的。"
        "卖家关心的是「我的商品怎么样了」，不是「你的代码怎么了」。",
        "11. **店里的规矩以知识库为准**：卖家问售后政策、退换条件、发货时效、平台规则、"
        "操作流程这类「我们是怎么规定的」问题时，**先调用 `search_knowledge` 检索他自己上传的资料**，"
        "按检索到的原文回答，并说明出自哪份资料。资料分多个知识库时可用 `kb` 限定范围"
        "（不确定有哪些库就先调 `list_knowledge_docs`）。"
        "⚠️ 检索为空就如实说「资料里没找到」，并提示他去「知识库」页面上传；"
        "**绝不要用常识编一套规则** —— 编出来的规则会被当真执行。"
        "删除资料只能通过 `delete_knowledge_doc`（会生成确认卡），"
        "**不要自己说「已删除」**：不调用工具就什么都没删。",
        "12. ⚠️ **「在后台生成 / 正在处理」这类说法必须以系统给的任务事实为准**（见下方"
        "「当前后台任务」，那是实时读库的）。没有任务在跑时，只能说「还没有开始生成，"
        "需要你点一下确认」——**不许**把上一轮自己的话改写成「正在生成中」；"
        "系统会在回复后核对，说了却没有任务会被打回。",
        "13. ⚠️ **批量请求用 `plan_batch` 一次性提交**：卖家一次要求多件商品的多步操作"
        "（如「给这 5 件商品都生成文案、宣传图并上架」）时，**调用 `plan_batch`**，"
        "把每件商品的每个动作列进 `items`（需要确认的会被合成一张卡、其余直接执行），"
        "而不是逐件逐个调工具 —— 逐件调会被单轮工具额度拦断，导致后半段动作根本没提交。"
        "提交后只说「共几步需要确认」，不要逐步说「已发起」。",
    ]
    if ctx.view or ctx.selected_goods_no:
        lines += ["", "当前界面上下文（供理解「这件 / 它」的指代）："]
        if ctx.view:
            lines.append(f"- 卖家正在浏览：{VIEW_LABELS.get(ctx.view, ctx.view)}")
        if ctx.selected_goods_no:
            lines.append(f"- 当前选中商品：{ctx.selected_goods_no}")
    if unfinished_tasks is not None:
        lines += ["", "当前后台任务（系统实时读库，以此为准）："]
        if unfinished_tasks > 0:
            lines.append(f"- 有 {unfinished_tasks} 个任务正在后台跑；要看是哪几件就调用 `list_tasks`。")
        else:
            lines.append("- **没有**正在执行的后台任务。此时说「在后台生成中 / 正在处理」一定是错的。")
    return "\n".join(lines)


def _to_model_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把对话历史转成模型消息。

    **只取卖家与助手的文本**，跳过 `role=tool` 的行 —— 工具过程不进上下文：既省 token，
    也避免"tool 消息缺 `tool_call_id`"这类格式问题。

    ⚠️ **代价必须说清**（实测踩到）：跳过 tool 行之后，上下文里就只剩「助手说过的话」。
    当数据在两次提问之间被改动（卖家刚上架 / 改价），模型很容易**把自己上一轮的旧结论当成事实**，
    于是答出过期信息。防线有两条，缺一不可：

    1. system prompt 的硬规则：**涉及当前状态必须实时查工具**；
    2. 这里给"当时由工具结果写回的助手消息"**打上过期标记**，让模型看到明确信号。

    这是"省 token"与"答得对"之间的刻意权衡，不是遗漏。
    """
    out: list[dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if not content:
            continue
        if role == "seller":
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            # 带 `tool_name` 的历史消息 = 当时由工具查询结果写回的，明确标注"可能已过期"
            if msg.get("tool_name"):
                content = f"{content}\n（以上是当时查询的结果，可能已过期；需要时应重新查询）"
            out.append({"role": "assistant", "content": content})
    return out


def ensure_tools_registered() -> None:
    """注册全部工具（幂等）。新增域时在此处加一行即可。"""
    tools_goods.register_all()
    tools_aftersale.register_all()
    tools_ops.register_all()
    tools_knowledge.register_all()
    tools_commerce.register_all()


#: 同一会话线程的**互斥锁**（线程 id → Lock）
_LOCKS: dict[str, asyncio.Lock] = {}


def _thread_lock(thread_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[thread_id] = lock
    return lock


async def run_turn(
    ctx: AssistantContext, message: str, *, internal: bool = False
) -> dict[str, Any]:
    """跑一轮对话 —— **同一线程串行**。

    ⚠️ 为什么必须串行：确认后的"续跑"与卖家新发的消息是**两次独立的 run_turn**，
    它们共享同一份对话历史。并发执行时，两轮会读到彼此写了一半的历史 → 上下文互相污染。

    实测踩到：卖家说「把 G-0005 商品删除」，模型却回了「已提交生成文案的请求」，
    还**多建了一张 `generate_copy` 的卡**（重复计费）—— 因为续跑那轮又调了一遍同一工具。

    前一轮还没跑完时，直接告知"上一条还在处理中"，而不是让卖家干等半分钟。
    """
    lock = _thread_lock(ctx.thread_id)
    if lock.locked():
        return {
            "reply": "上一条还在处理中，请等它结束后再发。",
            "pending": [], "trace": [], "stop_reason": "busy",
        }
    async with lock:
        return await _run_turn_locked(ctx, message, internal=internal)


async def _run_turn_locked(
    ctx: AssistantContext, message: str, *, internal: bool = False
) -> dict[str, Any]:
    """跑一轮助手对话：落卖家消息 → 循环 → 落助手回复。返回本轮结果。

    `internal=True` 表示**系统内部驱动**（例如"卖家确认后继续"）：
    不落卖家消息，把 `message` 直接作为一条 user 消息喂给模型。
    """
    ensure_tools_registered()

    text = (message or "").strip()
    if not text:
        return {"reply": "请告诉我要做什么。", "pending": [], "trace": [], "stop_reason": "empty"}

    if not internal:
        await repo.append_message(ctx.thread_id, "seller", text, seller_id=ctx.seller_id)

    model = get_chat_model()
    if model is None:
        # 模型不可用：**明确告知**，不假装在思考（也不静默失败）
        reply = ("AI 助手当前不可用：模型未启用或未配置密钥。"
                 "请检查 .env 的 LLM_ENABLED 与 DASHSCOPE_API_KEY 后重启服务。")
        await repo.append_message(ctx.thread_id, "assistant", reply, seller_id=ctx.seller_id)
        return {"reply": reply, "pending": [], "trace": [], "stop_reason": "model_unavailable"}

    history = await repo.list_messages(ctx.thread_id, limit=HISTORY_LIMIT)
    # 后台任务的**实时事实**：模型对自己说过的话记得很牢，对"任务到底跑没跑"一无所知，
    # 所以这一项必须由代码查好塞给它（见 _unfinished_count 处注释）。
    unfinished_before = await _unfinished_count(ctx.seller_id)
    turns = _to_model_messages(history)

    # ---- 上下文窗口的两条硬性对齐（缺一条，模型就会"答上一句"）----
    # 实测代价（2026-10-20）：`list_messages` 曾按 seq 升序取前 40 条，会话一超过 40 条，
    # 窗口就停在中段 —— 卖家连续四轮问「售后政策中哪些不予受理？」，模型每次都接着
    # 窗口末尾那条助手消息往下写"电竞椅文案已更新"。所以：
    #   1. 窗口必须**从卖家说的话开始**（否则开头是一段没头没尾的助手自述）；
    #   2. 卖家**这一句**必须是最后一条，且不能重复（窗口万一没覆盖到就补上）。
    while turns and turns[0].get("role") != "user":
        turns.pop(0)
    if internal:
        # 内部驱动：不落卖家消息，但要把它作为本轮的用户输入
        turns.append({"role": "user", "content": text})
    elif not turns or turns[-1].get("content") != text:
        # 兜底不变量：**无论历史怎么截断/清理，本轮卖家的话一定在模型输入的最后**
        turns.append({"role": "user", "content": text})

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(ctx, unfinished_tasks=unfinished_before)}
    ] + turns
    specs = registry.model_specs()

    reply = ""
    stop_reason = "done"
    trace: list[dict[str, Any]] = []
    pending_cards: list[dict[str, Any]] = []
    tools_used = 0
    #: 本轮是否有工具调用因预算打穿而被拦截 —— 收尾时决定走哪条守卫路径
    budget_hit = False
    #: 本轮的**批次号** —— 同一句要求里拆出的多个待确认动作共享它。
    #: 界面据此把它们收成**一张卡**：一次确认执行整批，而不是让卖家逐张点，
    #: 更不会出现"点了其中一张、剩下的又冒出来"的错位。
    batch_id = f"b{uuid.uuid4().hex[:12]}"
    #: 本轮已执行过的「工具 + 参数」—— 用于拦截**重复的有副作用调用**
    seen_calls: set[str] = set()

    async def _execute_calls(turn: Any) -> None:
        """把模型给出的工具调用全部派发并把结果回灌（主循环与纠正轮共用）。"""
        nonlocal tools_used, budget_hit
        _ensure_call_ids(turn)
        messages.append({
            "role": "assistant",
            "content": turn.content or "",
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls
            ],
        })
        for call in turn.tool_calls:
            if tools_used >= DEFAULT_TOOL_BUDGET:
                budget_hit = True
                # ⚠️ 措辞即行为（2026-10-21 实测）：旧文案「请先用已有信息回复卖家」
                # 等于**教模型半途收尾**，它照做后就写成「我马上接着为您处理」——
                # 轮次一结束什么都不会发生。改为要求如实交代 + 给卖家可操作的出路。
                content = ("本轮工具调用次数已达上限，这个操作**没有执行**。"
                           "请如实告诉卖家：哪些已完成、哪些（包括这一个）还没开始，"
                           "请他回复「继续」，下一轮接着做。"
                           "**不要**说「马上处理 / 接着处理」——那只会让他空等。")
                trace.append({"name": call.name, "args": call.arguments, "ok": False,
                              "summary": "超出本轮工具预算"})
            else:
                content = await _dispatch(
                    ctx, call, trace, pending_cards, seen_calls, batch_id
                )
                tools_used += 1
            messages.append({
                "role": "tool", "tool_call_id": call.id, "name": call.name, "content": content,
            })

    for _step in range(MAX_STEPS):
        # 成本闸门：助手与售后对话**受同一个闸门约束**（都是真实计费调用）
        gate = await cost_gate.precheck(ongoing=True)
        if gate.paused:
            reply = f"成本闸门已暂停模型调用：{gate.message}"
            stop_reason = "gate_paused"
            break

        try:
            turn = await model.step(messages, specs)
        except Exception as exc:  # noqa: BLE001 —— 模型异常由本层降级，不让整个请求炸掉
            logger.warning("助手模型调用失败：%s: %s", type(exc).__name__, exc)
            _dump_model_failure(ctx, exc, messages)
            reply = "抱歉，我这边调用模型时出错了，请稍后再试。"
            stop_reason = "model_error"
            break

        # 记账：助手调用同样计入用量（case_id 为空 —— 它不属于任何售后工单）
        await cost_gate.record_usage(
            turn.input_tokens, turn.output_tokens, case_id=None, seq=None, persist=True
        )

        if turn.content:
            reply = turn.content

        if not turn.has_tool_calls:
            stop_reason = "done"
            break

        await _execute_calls(turn)
    else:
        stop_reason = "max_steps"
        if not reply:
            reply = "这件事我还没处理完，需要你再确认一下具体要求。"

    async def _run_correction(notice: str, current_reply: str) -> str:
        """追加一轮**有界**纠正（主循环与两处守卫共用）。

        为什么纠正要走模型而不是直接改文案：正确的补救是**让它真的去调工具**
        （建出确认卡），而不是把句子改通顺 —— 光改文字，卖家那件事还是没做。
        """
        messages.append({"role": "user", "content": notice})
        for _fix in range(_FIX_STEPS):
            gate = await cost_gate.precheck(ongoing=True)
            if gate.paused:
                break
            try:
                fix_turn = await model.step(messages, specs)
            except Exception as exc:  # noqa: BLE001 —— 纠正轮失败就保留原回复，不再降级
                logger.warning("纠正轮模型调用失败：%s: %s", type(exc).__name__, exc)
                _dump_model_failure(ctx, exc, messages)
                break
            await cost_gate.record_usage(
                fix_turn.input_tokens, fix_turn.output_tokens,
                case_id=None, seq=None, persist=True,
            )
            if fix_turn.content:
                current_reply = fix_turn.content
            if not fix_turn.has_tool_calls:
                break
            await _execute_calls(fix_turn)
        return current_reply

    # ---- 守卫 0：预算打穿时的收尾兜底（2026-10-21 实测）----
    # 预算打穿后，纠正轮里的工具调用也会被同一份额度拦住 —— 再纠正纯属浪费计费。
    # 所以 A/B/C 三道守卫全部跳过，改由**代码**直接把事实补在回复末尾。
    if budget_hit and (_has_promise(reply) or _has_claim(reply) or _has_background_claim(reply)):
        reply += _BUDGET_FACT_NOTE

    # ---- 「嘴说-事实」一致性守卫（详见 _CLAIM_MARKERS 处注释）----
    # 只有"声称了 + 本轮没建卡 + 系统里也确实没有待确认项"三者同时成立才触发：
    #   - 本轮建了卡（pending_cards 非空）→ 声称属实，放行；
    #   - 系统里还有早前的待确认项 → 模型可能是在提醒卖家去点，放行。
    if not budget_hit and _has_claim(reply) and not pending_cards:
        try:
            still_pending = await actions.list_pending(ctx)
        except Exception:  # noqa: BLE001 —— 查不到按"没有"处理，宁可多纠一次不可漏放
            still_pending = []
        if not still_pending:
            logger.warning(
                "助手疑似「嘴上执行」（thread=%s）：回复声称已发起确认，但无任何待确认项，触发纠正轮",
                ctx.thread_id,
            )
            reply = await _run_correction(_CORRECTION_NOTICE, reply)

    # ---- 守卫 B：「在后台生成」必须真有后台任务（详见 _BACKGROUND_MARKERS 处注释）----
    # 与本轮是否建卡无关：后台任务不来自确认卡，来自 ops_task。所以判定条件是
    # "说了在进行 + 库里既没有进行中的任务、也没有待确认项"。
    if not budget_hit and _has_background_claim(reply):
        try:
            pending_now = await actions.list_pending(ctx)
        except Exception:  # noqa: BLE001
            pending_now = []
        if not pending_now and await _unfinished_count(ctx.seller_id) == 0:
            logger.warning(
                "助手声称「后台生成中」但无任何后台任务（thread=%s），触发纠正轮", ctx.thread_id,
            )
            reply = await _run_correction(_BACKGROUND_CORRECTION_NOTICE, reply)
            # 纠正轮也没建出任务 / 没建出卡，且它还在嘴硬 → 由**代码**说出事实
            try:
                pending_now = await actions.list_pending(ctx)
            except Exception:  # noqa: BLE001
                pending_now = []
            if (
                not pending_now and not pending_cards
                and await _unfinished_count(ctx.seller_id) == 0
                and _has_background_claim(reply)
            ):
                reply += _BACKGROUND_FACT_NOTE

    # ---- 守卫 C：「马上接着处理」式的未来承诺（2026-10-21 实测）----
    # 「已发起」（守卫 A，过去时）、「正在生成中」（守卫 B，进行时）之外的第一种：
    # **将来时** —— 「我马上接着为您处理……稍后需要您在弹出卡片上统一确认」。
    # 对话在回复落库那一刻就结束了，没有任何机制会替模型"接着处理"，卖家干等。
    # 触发条件与 A 同款：承诺了 + 本轮没建卡 + 系统里也没有待确认项。
    if not budget_hit and _has_promise(reply) and not pending_cards:
        try:
            promise_pending = await actions.list_pending(ctx)
        except Exception:  # noqa: BLE001 —— 查不到按"没有"处理，宁可多纠一次
            promise_pending = []
        if not promise_pending:
            logger.warning(
                "助手承诺「接着处理」但轮次即将结束、无任何待办事实（thread=%s），触发纠正轮",
                ctx.thread_id,
            )
            reply = await _run_correction(_PROMISE_CORRECTION_NOTICE, reply)
            # 纠正后仍在承诺、也没建出卡 → 由**代码**说出事实
            try:
                promise_pending = await actions.list_pending(ctx)
            except Exception:  # noqa: BLE001
                promise_pending = []
            if _has_promise(reply) and not pending_cards and not promise_pending:
                reply += _PROMISE_FACT_NOTE

    if not reply:
        reply = "我没能理解你的意思，能再说得具体一点吗？"

    # ---- 「留在等确认的步骤」必须由系统说出来，不能靠模型自觉 ----
    # 实测踩到（一次请求含「建商品 + 生图 + 文案」）：卖家点了文案那张卡，
    # 模型回复"文案已更新"，**下一句才轻描淡写地提到宣传图还在等确认** ——
    # 卖家读成"两件事都做完了"，接着去看商品发现没图，以为系统吞了操作。
    # 所以：只要系统里还有待确认项，就在回复末尾**固定追加一行**剩余清单。
    # 这不依赖模型措辞（它可能说"已完成"），也不额外调模型（不产生费用）。
    try:
        still_pending = await actions.list_pending(ctx)
    except Exception:  # noqa: BLE001 —— 查不到就不加，不影响正常回复
        still_pending = []
    # 有没有"本轮刚新建的卡"决定要不要补这一行：
    # 本轮建了卡 → 卡就渲染在回复下面、模型也刚说过"请确认"，再补一行是啰嗦；
    # 本轮没建卡却仍挂着待确认项 → 正是"点了其中一张"的情形，**必须补**。
    if still_pending and not pending_cards:
        names = "、".join(str(p.get("summary") or "") for p in still_pending[:3])
        more = "" if len(still_pending) <= 3 else f" 等 {len(still_pending)} 步"
        reply += (f"\n\n⏳ 还有 {len(still_pending)} 步在等你确认：{names}{more}。"
                  "点了卡片才会真正执行。")

    await repo.append_message(ctx.thread_id, "assistant", reply, seller_id=ctx.seller_id)
    return {
        "reply": reply, "pending": pending_cards, "trace": trace,
        "stop_reason": stop_reason, "tools_used": tools_used, "batch_id": batch_id,
    }


async def continue_after_actions(
    ctx: AssistantContext, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """卖家确认并执行动作后，**恢复对话**让模型继续。

    为什么必须有这一步（实测踩到）：确认是一次独立的 HTTP 请求
    （`POST /actions/{id}/confirm`），执行完就结束了 ——
    **模型并不知道"它请求的操作已经完成"**。于是它永远停在"生成了文案候选"那一步，
    不会继续调用 `update_goods` 把文案写进商品（现象：商品文案一直是空字符串）。

    为什么收**一组**而不是一条：一次请求里的多个步骤现在由卖家**一次确认**执行完
    （`actions.confirm_batch`），若这里逐条续跑，就会对同一句话重复调 N 次模型 ——
    既重复计费，也会让后一次续跑把前一步再提交一遍（实测踩到：多出多余的确认卡）。
    """
    # 把"已经在等确认的事"告诉模型 —— 否则它会以为自己还没提交，再提一次
    try:
        pending_items = await actions.list_pending(ctx)
    except Exception as exc:  # noqa: BLE001 —— 取不到就不带这段，不影响续跑
        logger.warning("续跑时读取待确认列表失败（忽略）：%s", exc)
        pending_items = []

    if pending_items:
        lines = "\n".join(
            f"- {p.get('summary')}（待确认项 #{p.get('id')}）" for p in pending_items
        )
        pending_note = (
            "⚠️ 下面这些操作**已经在等卖家确认**，系统已经记录，"
            "**不要再重复提交**（重复提交会造出多余的确认卡）：\n" + lines + "\n\n"
        )
    else:
        pending_note = ""

    done = "\n".join(
        f"{index}. {'✅' if one.get('ok') else '⚠️'} {one.get('summary')}：{one.get('content')}"
        for index, one in enumerate(results, start=1)
    )
    notice = (
        f"（系统通知）卖家已经在确认卡上一次性确认了 {len(results)} 步操作，"
        f"下面是**逐项真实执行结果**：\n{done}\n\n"
        + pending_note
        + "请据此**继续完成卖家最初的要求**：如果还需要**下一步**（例如把刚才生成的文案写入商品、"
        "或补完他要求的其余步骤），**现在就执行**，不要停下来等他再说第二遍。"
        "⚠️ **绝对不要重复调用已经执行过的工具** —— 那些事已经做完，再调一次会重复执行、重复计费。"
        "⚠️ 收尾时**逐条对照卖家最初的要求**，明确说清哪几步已经完成、哪几步还没做"
        "（尤其是失败的、还在等确认的）—— **没做的绝不能说成做完**。"
    )
    return await run_turn(ctx, notice, internal=True)


async def continue_after_action(
    ctx: AssistantContext, tool_name: str, result_text: str
) -> dict[str, Any]:
    """单条版本的续跑（保留给单卡确认与既有调用方）。"""
    return await continue_after_actions(ctx, [{
        "tool_name": tool_name, "summary": tool_name, "ok": True, "content": result_text,
    }])


async def _exec(ctx: AssistantContext, spec: registry.ToolSpec, args: dict[str, Any]) -> Any:
    """执行工具；**只读**工具（策略 `retryable`）遇失败自动重试一次。

    只读重试安全：超时/连接重置这类瞬时故障很常见，再查一次即可。
    ⚠️ 写操作与计费工具**绝不重试**（会重复执行、重复计费）—— 由 `failure_policy` 保证。
    """
    try:
        return await spec.handler(ctx, **args)
    except Exception as exc:  # noqa: BLE001
        if spec.failure_policy != "retryable":
            raise
        logger.warning("只读工具 %s 失败，自动重试一次：%s: %s", spec.name, type(exc).__name__, exc)
        return await spec.handler(ctx, **args)


async def _dispatch(
    ctx: AssistantContext,
    call: Any,
    trace: list[dict[str, Any]],
    pending_cards: list[dict[str, Any]],
    seen: set[str] | None = None,
    batch_id: str = "",
) -> str:
    """执行一个工具调用，返回要回灌给模型的文本。

    四条分支：
    **未知工具 → 拒绝**；**本轮重复的有副作用调用 → 拦截**；
    **需要确认的（不可逆 / 计费）→ 转待确认、不执行**；其余 → 执行（只读失败自动重试一次）。

    `batch_id`：本轮所有待确认动作共享的批次号 —— 界面据此把它们合成一张卡。
    """
    spec = registry.get(call.name)
    args = call.arguments if isinstance(call.arguments, dict) else {}

    if spec is None:
        trace.append({"name": call.name, "args": args, "ok": False, "summary": "未知工具，已拒绝"})
        return f"不存在名为 {call.name} 的工具。请只使用已提供的工具。"

    # ---- 重复调用拦截：同轮内「同工具 + 同参数」且**有副作用**时不再执行 ----
    # 只拦有副作用的（不可逆/计费）；只读工具重复调无害，甚至可能是模型在核对。
    key = f"{spec.name}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
    if seen is not None and spec.needs_confirm and key in seen:
        trace.append({"name": spec.name, "args": args, "ok": False, "summary": "重复调用，已拦截"})
        return (f"这个操作（{spec.name}）在本轮**已经执行过一次**，结果见上文。"
                f"**不要重复调用** —— 重复执行会重复生效、重复计费。")
    if seen is not None:
        seen.add(key)

    if spec.needs_confirm:
        card = await actions.create_pending(ctx, spec, args, batch_id=batch_id)
        # 这个动作最近刚做完（模型在续跑那轮重复提交）→ 不建卡，直接告诉它结果
        if card.get("already_done"):
            trace.append({"name": spec.name, "args": args, "ok": True,
                          "summary": "近期已完成，未重复提交"})
            return (f"这个操作（{card.get('summary')}）**刚刚已经完成过了**，结果是：\n"
                    f"{str(card.get('result') or '')[:200]}\n"
                    f"**不要重复提交**；如果这件事已经全部做完，直接告诉卖家即可。")

        pending_cards.append(card)
        trace.append({"name": spec.name, "args": args, "ok": True,
                      "summary": f"待卖家确认：{card['summary']}"})
        # 理由分两类：不可逆 vs 会产生费用 —— 让模型转达的措辞准确
        reason = "会改变数据且不可撤销" if spec.destructive else "会产生 API 费用"
        return (f"该操作（{card['summary']}）{reason}，"
                f"系统已生成待确认项 #{card['id']}，**在卖家确认之前不会执行**。"
                f"⚠️ 同一句要求里的多个待确认动作会**合并成一张卡**，卖家一次确认就全部执行；"
                f"所以你现在**只做一件事**：用一句话告诉卖家「共 N 步需要他在卡片上确认」，"
                f"不要逐步说「已发起」，也不要说任何一步「已完成」。")

    try:
        result = await _exec(ctx, spec, args)
        trace.append({"name": spec.name, "args": args, "ok": result.ok, "summary": result.content[:120]})
        return result.content
    except Exception as exc:  # noqa: BLE001 —— 工具异常要如实回灌，让模型能解释给卖家
        logger.warning("工具执行异常（%s）：%s: %s", spec.name, type(exc).__name__, exc)
        trace.append({"name": spec.name, "args": args, "ok": False, "summary": str(exc)[:120]})
        # 给**模型**完整原因（它要判断能否自愈），但明确要求它面向卖家**说人话** ——
        # 实测踩到：模型把 `TypeError: create_task() got an unexpected keyword...` 原样念给卖家。
        return (
            f"工具执行出错（**内部错误**，卖家看不懂也不需要看懂）："
            f"{type(exc).__name__}: {exc}\n"
            "请**用卖家能懂的话**告诉他：这件事没做成、现在能怎么办（重试 / 换个方式 / 稍后再试）。"
            "**绝对不要把技术报错原文、函数名、堆栈念给他** —— 那是给开发看的。"
        )
