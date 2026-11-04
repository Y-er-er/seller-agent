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
    # ⚠️ 文本模型须为「纯文本模型」：langchain 的 ChatTongyi 仅按「名字是否含 vl」推断端点，
    #    多模态模型（qwen3.7-plus / qwen3.8-max 等）名字不含 vl，会被误判到纯文本端点，
    #    导致每次调用稳定返回 400 InvalidParameter: url error。
    #    视觉模型另有独立通路（app/llm/client.py 直接用 MultiModalConversation），不受此影响。
    llm_text_model: str = "qwen3.7-max"
    llm_vision_model: str = "qwen3-vl-plus"

    # ---------- 功能开关 ----------
    # 模型调用是**正常运行能力**；成本是配置项（见下方「成本闸门」），**不是设计前提**。
    # ⚠️ 开启后，任何走对话 / 问答润色 / 视觉的请求都会产生**真实计费**调用。
    # 注意：pydantic-settings 会隐式继承系统环境变量，OS 上的 DASHSCOPE_API_KEY 可被读到。
    llm_enabled: bool = True
    shadow_mode: bool = True

    # ---------- 视觉理解（举证图片，**计费**）----------
    # 闸门：只有 llm_enabled=true 且 vision_max_images>0 时才发起调用。
    # 单笔申请的图片处理张数上限（默认 5 张），且只处理可直接送模型的 http(s) 图片。
    vision_max_images: int = 5
    vision_timeout: float = 8.0      # 单张图片调用超时（秒），超时即降级，不阻塞判定
    # 合规控制（**非成本项**）：买家留言涉个人信息出境，默认不送第三方模型。
    vision_include_buyer_note: bool = False

    # ---------- 口径问答（客服释疑，**不参与资格判定**）----------
    # 基线为确定性解释（规则命中 + 审计事实）；qa_llm_polish=true 时用模型润色措辞（事实仍来自规则）。
    qa_llm_polish: bool = True
    qa_max_sources: int = 3          # 单次回答最多引用的标准口径条目数

    # ---------- 对话入口（P1：买家侧对话智能体）----------
    # 默认开启：对话入口即本智能体的主界面。
    chat_enabled: bool = True

    # ---------- 对话护栏（产品护栏，按业务需要调整）----------
    max_rounds: int = 20             # 单会话轮数上限（一个"往返"= 一轮：买家消息 + 智能体回复）
    max_msg_chars: int = 200         # 单条消息字数上限；首次超长只提示（不计轮次），再犯转人工
    max_tools_per_round: int = 3     # 单轮内工具调用次数上限（防模型空转）
    max_slot_ask: int = 2            # 同一槽位的追问次数上限，仍拿不到即转人工
    session_timeout_hours: int = 24  # 会话静默超时（小时）；到期自动收尾或转人工

    # ---------- 成本闸门（**配置项**，不是设计前提；用于兜底，不用于限制正常使用）----------
    # 三级动作：免费额度优先 → 预警线汇报 → 月度硬上限停止。
    # **逐次调用前置预检**，不是事后统计（否则"已经花了才发现"）。
    cost_warning_cny: float = 20.0       # 预警线（元）：默认取月度上限的 10%
    cost_monthly_cap_cny: float = 200.0  # 月度硬上限
    # **单个模型**的免费额度（token）；耗尽后进入现金计费。
    # ⚠️ 口径是"每个模型各一份"（百炼的发放方式），所以判定时只比**当前文本模型**的用量。
    # 拿全模型合计去比会把新模型的额度"让"给旧模型 —— 换模型后对话会被误判为耗尽。
    free_quota_tokens: int = 1_000_000
    # **额度到期日**（以官方控制台为准，形如 "2026-10-31"）。
    # 官方免费额度有有效期：**到期后额度失效，后续调用转为按量付费**。
    # 因此"到期"必须与"耗尽"一样触发暂停——否则会在不知情的情况下开始真扣费。
    free_quota_expire_date: str = ""
    # 官方控制台显示的**外部已用量**（非本项目消耗），**按当前模型**计。
    # 本地累计之外再加上它，才能得到该模型的真实剩余额度。
    free_quota_used_baseline: int = 0
    # 单价（元 / 1K token）—— **可选项**，属使用者提供的外部事实，本项目不臆造价格。
    # 留空（=0）时成本闸门只按 token 计量：**这是正常状态，不是待补的配置缺陷**，不做启动告警。
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

    # ---------- 知识库（卖家自建资料的检索 · RAG）----------
    # 卖家自己上传规则/政策/商品资料，助手按需检索引用 —— 数据自产，不依赖外部平台。
    # ⚠️ 向量化**按 token 计费**（与对话模型同属正常模型调用，成本是配置项）。
    knowledge_enabled: bool = True
    # Chroma 持久化目录（相对 backend/）；删库=删这个目录，别手工删单个文件
    knowledge_dir: str = "./data/knowledge"
    knowledge_collection: str = "seller_knowledge"
    # 向量化模型与维度：text-embedding-v3 支持 1024 / 768 / 512，改维度须重建库
    embedding_model: str = "text-embedding-v3"
    embedding_dimensions: int = 1024
    # DashScope 的 OpenAI 兼容端点（向量化与兼容调用共用）
    dashscope_compat_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_batch: int = 10        # v3 单次最多 10 条，超了服务端会拒
    embedding_timeout: float = 15.0
    # 分块参数：700 字 / 100 字重叠（按句子边界切，重叠保住跨块上下文）
    knowledge_chunk_size: int = 700
    knowledge_chunk_overlap: int = 100
    knowledge_top_k: int = 5         # 默认检索条数
    knowledge_max_upload_bytes: int = 10 * 1024 * 1024   # 单份资料上限 10 MB

    # ---------- 数据库 ----------
    mysql_uri: str = ""
    checkpoint_sqlite_path: str = "./data/checkpoints.sqlite"    # 审计是否落 MySQL。关闭时 audit 节点只产出内存事件，不触碰数据库
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

    # ---------- 可插拔适配层（开发/生产切换）----------
    # 完整设计见 docs/设计_可插拔适配层_v1.md；接口契约见 docs/契约_外部数据源_v1.md。
    #
    # 数据源模式：
    #   ''        自动推断（有 *_API_BASE 即 remote，否则 local）——开发与测试的首选
    #   'local'   进程内夹具（演示数据，零外部依赖）
    #   'remote'  真实只读 API
    # ⚠️ 生产**务必显式设 'remote'**：此时若 *_API_BASE 为空，取数会明确失败并记 error，
    #    **绝不回落到本地夹具** —— 杜绝"以为在用真实数据、其实在读演示数据"。
    data_source: str = ""
    # 对象存储后端：'local' 本地目录 / 'cos' 腾讯云 COS
    storage_backend: str = "local"
    # 本地存储根目录（相对 backend/），仅 storage_backend=local 时使用
    upload_dir: str = "./data/uploads"
    # 上传限制：单张图片字节上限（超限拒绝，不做截断）
    upload_max_bytes: int = 5 * 1024 * 1024

    # ---------- 腾讯云 COS ----------
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_region: str = "ap-nanjing"
    cos_bucket: str = ""

    #: 回收站保留天数：删除的商品在此期限内可恢复，到期自动彻底删除（0 = 不自动清）
    goods_trash_days: int = 7

    # ---------- 图像生成（AI 宣传图 · 通义万相）----------
    # ⚠️ 留空 = 未开通 / 未配置 → 生图端点会**明确报错**，绝不假装能生成。
    #    常用型号：wanx2.1-t2i-turbo（快）· wanx2.1-t2i-plus（精细）· wan2.5-t2i-preview
    #    需先在阿里云百炼**开通图像模型**，再把型号填到这里。
    image_model: str = ""
    # size 必须用**星号**（如 1024*1024）；写成小写 x 会被服务端判为 InvalidParameter
    image_size: str = "1024*1024"
    # 异步任务：总等待上限（秒）与轮询间隔
    image_gen_timeout: float = 90.0
    image_poll_interval: float = 3.0

    # ---------- 外部只读 API ----------
    order_api_base: str = ""
    logistics_api_base: str = ""
    aftersale_api_base: str = ""

    # ---------- 演示辅助（**非业务能力**，供测试与演示）----------
    # true：允许**任意订单号**受理——未预置的订单号按订单号**确定性合成**订单/物流/画像事实，
    #       使售后链路（含规则判定）能完整跑通，用于把智能体测透。
    # 两条硬边界：① 合成数据一律标注 `synthetic=true`，**不伪装成真实数据**；
    #            ② 已配置真实业务 API（order_api_base / logistics_api_base）时**整体失效**，不污染真实链路。
    demo_any_order: bool = True

    # ---------- 卖家登录（唯一身份来源）----------
    # 会话有效期（小时）。到期后必须重新登录；每次登录都会分配**新的助手会话线程**。
    seller_session_hours: float = 12.0
    # 连续登录失败达到该次数后锁定一段时间（同一账号 + 同一进程内计数）。
    seller_login_max_fails: int = 5
    seller_login_lock_minutes: float = 10.0
    # 首次启动自动创建的演示账号（仅在账号表为空时创建；**不会覆盖已存在的密码**）。
    # 改密：重新登录前把 SELLER_BOOTSTRAP_* 与库里的账号对齐，或直接用 scripts/create_seller_account.py 重建。
    seller_bootstrap_username: str = "admin"
    seller_bootstrap_password: str = "admin123"
    seller_bootstrap_name: str = "默认卖家"
    # 该演示账号绑定的业务数据归属 —— 与历史数据（DDL 默认值）保持一致，登录后直接看到现有店铺数据。
    seller_bootstrap_seller_id: str = "default"

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
    def resolved_data_source(self) -> str:
        """解析**实际生效**的数据源模式。

        显式配置优先；未配置时从 `*_API_BASE` 推断 —— **三个 base_url 任一非空即 remote**。

        为什么要统一判断、而不是各接口看自己的 base_url：后者会产生"一半数据走真实、
        一半走夹具"的混合态 —— 那比全假更危险（看起来是真的，其实掺了演示数据）。
        统一模式下，remote 时缺配置的接口会**明确返回 None**，问题立刻可见。

        推断只为兼容开发与测试的便利，**生产应由 `DATA_SOURCE=remote` 显式声明**。
        """
        mode = (self.data_source or "").strip().lower()
        if mode in ("local", "remote"):
            return mode
        has_any_base = bool(
            self.order_api_base or self.logistics_api_base or self.aftersale_api_base
        )
        return "remote" if has_any_base else "local"

    @property
    def storage_configured(self) -> bool:
        """腾讯云 COS 是否已配置齐全（决定 `STORAGE_BACKEND=cos` 能否真正启用）。"""
        return bool(self.cos_secret_id and self.cos_secret_key and self.cos_bucket)

    @property
    def price_configured(self) -> bool:
        """是否已配置模型单价。

        单价属**可选**的外部事实：未配置时成本闸门只按 token 计量，属**正常状态**。
        因此在健康端点如实报告该状态，但**不做启动告警**（成本是配置项，不是设计前提）。
        """
        return self.price_input_per_1k > 0 or self.price_output_per_1k > 0


# 单例：全进程共享一份配置
settings = Settings()
