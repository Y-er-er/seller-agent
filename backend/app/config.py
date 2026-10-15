"""全局配置。

纪律 9：密钥 / API 地址 / 模型名称等**一律来自 .env**，代码中不出现任何硬编码取值。
所有配置项集中在此处，其他模块只允许通过 `settings` 读取。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend/app/config.py -> backend/app -> backend -> refund-agent）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """运行期配置。字段名与 .env 中的键一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / "backend" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 模型服务商 ----------
    dashscope_api_key: str = ""
    deepseek_api_key: str = ""

    # ---------- 模型名称 ----------
    # 默认值与用户既有环境（D:\Users\17153\Desktop\.env）的命名对齐
    llm_text_model: str = "qwen3.7-plus"
    llm_vision_model: str = "qwen3-vl-plus"

    # ---------- 功能开关 ----------
    # 默认**关闭**模型调用：开启即产生**计费** API 调用，须显式授权（纪律 10）。
    # 注意：pydantic-settings 会隐式继承系统环境变量；若 OS 环境已存在
    #      DASHSCOPE_API_KEY，本模块能读到密钥——但模型仍不会被调用，
    #      因为总开关 llm_enabled 默认 False，构成第二道闸门。
    llm_enabled: bool = False
    shadow_mode: bool = True

    # ---------- 视觉理解（举证图片，**计费**）----------
    # 与 llm_enabled 构成双重约束：只有 llm_enabled=true 且 vision_max_images>0 时才发起调用。
    # 成本可控：单笔申请的图片处理张数硬上限（默认 2 张），且只处理可直接送模型的 http(s) 图片。
    vision_max_images: int = 2
    vision_timeout: float = 8.0      # 单张图片调用超时（秒），超时即降级，不阻塞判定
    # 隐私：买家留言**默认不送**第三方模型（个人信息出境需单独评估，见方案 §11 边界声明）。
    vision_include_buyer_note: bool = False

    # ---------- 口径问答（客服释疑，**不参与资格判定**）----------
    # 确定性解释（规则命中 + 审计事实）**零成本**；仅当 qa_llm_polish=true 且总闸门开启时才用模型润色。
    qa_llm_polish: bool = False
    qa_max_sources: int = 3          # 单次回答最多引用的标准口径条目数

    # ---------- 对话入口（P1：买家侧对话智能体）----------
    # 默认**关闭**：新入口独立于既有判定链路，关闭即退回控制台形态（回退方案见规划书 §十四）。
    chat_enabled: bool = False

    # ---------- 对话护栏（**已定稿，不可放宽**；见规划书 §9.1）----------
    max_rounds: int = 20             # 单会话轮数上限（一个"往返"= 一轮：买家消息 + 智能体回复）
    max_msg_chars: int = 200         # 单条消息字数上限；首次超长只提示（不计轮次），再犯转人工
    max_tools_per_round: int = 3     # 单轮内工具调用次数上限（防模型空转）
    max_slot_ask: int = 2            # 同一槽位的追问次数上限，仍拿不到即转人工
    session_timeout_hours: int = 24  # 会话静默超时（小时）；到期自动收尾或转人工

    # ---------- 成本闸门（**计费红线**，见规划书 §9.1 / §13.1）----------
    # 三级动作：免费额度优先 → 预警线暂停 → 月度硬上限停止。
    # **逐次调用前置预检**，不是事后统计（否则"已经花了才发现"）。
    cost_warning_cny: float = 0.1        # 预警线：等效消耗达此值即**暂停新会话并向用户汇报**
    cost_monthly_cap_cny: float = 200.0  # 月度硬上限
    free_quota_tokens: int = 1_000_000   # 免费额度（token）；耗尽后进入现金计费
    # **额度到期日**（以官方控制台为准，形如 "2026-10-31"）。
    # 官方免费额度有有效期：**到期后额度失效，后续调用转为按量付费**。
    # 因此"到期"必须与"耗尽"一样触发暂停——否则会在不知情的情况下开始真扣费。
    free_quota_expire_date: str = ""
    # 官方控制台显示的**外部已用量**（非本项目消耗）。本地累计之外再加上它，才能得到真实剩余额度。
    free_quota_used_baseline: int = 0
    # 单价（元 / 1K token）**须由用户按官方价填写**——本项目不臆造价格。
    # 未填写（=0）时，闸门自动降级为**按 token 计量**兜底（累计 token ≥ 免费额度即暂停），
    # 并在健康端点标记 `price_configured=false`，避免"因单价缺失而闸门形同虚设"。
    price_input_per_1k: float = 0.0
    price_output_per_1k: float = 0.0

    # ---------- 可调业务阈值（规则表以 ${VAR} 引用，纪律 9）----------
    # 默认值与 **已定稿** 的规则表（2026.10-final）保持一致；调参只需改 .env。
    # 命名与规则表的对应关系见 config/refund_rules.yaml 头部注释。
    auto_amount_cap: float = 300.0   # R1：金额**达到**此上限即转人工（规则用 gte）
    small_amount: float = 50.0       # R2 与 R4 **共用**的小额线（元）
    min_credit_score: int = 650      # R4：自动通过所需的最低信用分
    # R4 与 R5 的退款率门槛**取同一值**，使两段衔接、不留无人区（见规则表头部注释）
    max_refund_rate: float = 0.20    # R4：自动通过允许的 90 天退款率上限
    high_refund_rate: float = 0.20   # R5：判定为高风险的 90 天退款率门槛
    high_refund_count: int = 6       # R5：判定为高频的 90 天仅退款笔数门槛
    reship_amount_cap: float = 200.0 # R9/R10：补发自动处理上限（元）——达到即转人工
    refund_window_days: int = 15     # R11：售后时效（天），签收后超过此天数且非质量 → 拒绝

    # ---------- 数据库 ----------
    mysql_uri: str = ""
    checkpoint_sqlite_path: str = "./data/checkpoints.sqlite"

    # 审计是否落 MySQL。关闭时 audit 节点只产出内存事件，不触碰数据库
    # （单元测试与离线环境据此保持无外部依赖）。
    audit_db_enabled: bool = True

    # ---------- 处置执行（M2：真实资金动作）----------
    # **双闸门**（对齐 llm_enabled 的计费闸门思路）：
    #   1) shadow_mode = false   —— 关闭影子模式
    #   2) disposal_enabled = true —— 显式授权执行真实资金动作
    # 两者同时满足才有资格执行；**即便如此**，未配置 disposal_api_base 时仍会拒绝执行。
    # 任何一项不满足都不会"静默降级为 mock 成功"——资金动作不允许假成功。
    disposal_enabled: bool = False
    disposal_api_base: str = ""
    disposal_timeout: float = 5.0    # 资金动作超时（秒），超时视为失败并升级人工

    # ---------- 腾讯云 COS ----------
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_region: str = "ap-nanjing"
    cos_bucket: str = ""

    # ---------- 外部只读 API ----------
    order_api_base: str = ""
    logistics_api_base: str = ""
    aftersale_api_base: str = ""

    # ---------- 跨域（复核工作台）----------
    # 免构建的静态工作台通过 file:// 或本地静态端口打开，与后端**不同源**，
    # 浏览器会拦截 fetch —— 必须由后端显式放行 CORS。
    # 默认 "*" 仅适用于本地开发；生产须收紧为具体域名（逗号分隔）。
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的来源配置解析为列表；空值回落为 "*"（仅本地开发）。"""
        items = [item.strip() for item in (self.cors_origins or "").split(",") if item.strip()]
        return items or ["*"]

    @property
    def price_configured(self) -> bool:
        """是否已配置模型单价。

        未配置时成本闸门**降级为按 token 计量**（而不是"金额恒为 0、闸门永不触发"）——
        这种静默失效正是最危险的，故在健康端点显式暴露该状态。
        """
        return self.price_input_per_1k > 0 or self.price_output_per_1k > 0


# 单例：全进程共享一份配置
settings = Settings()
