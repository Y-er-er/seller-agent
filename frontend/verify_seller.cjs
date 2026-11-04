/* ============================================================
   卖家控制台前端验证（**纯函数，零后端、零计费**）
   ============================================================
   用最小 DOM mock 加载 seller.js，然后直接断言渲染函数的输出。
   覆盖：转义 / 金额格式 / URL 解析 / AI 结果卡 / 待确认卡 / 各类标签映射。

   运行：node frontend/verify_seller.cjs
   ============================================================ */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failures = [];
function check(name, ok, detail) {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (detail && !ok ? `  ${String(detail).slice(0, 160)}` : ""));
  if (!ok) { failures.push(name); }
}

/* ---------- 最小 DOM mock ---------- */
function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", hidden: false,
    style: {}, scrollTop: 0, scrollHeight: 0, offsetWidth: 40, offsetHeight: 40,
    // 记录 class 状态：这样才能断言"收起是否真的移除了 open"
    classList: {
      _set: {},
      add(c) { this._set[c] = true; },
      remove(c) { delete this._set[c]; },
      toggle(c, on) {
        if (on === undefined) { on = !this._set[c]; }
        if (on) { this._set[c] = true; } else { delete this._set[c]; }
      },
      contains(c) { return !!this._set[c]; }
    },
    listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, appendChild() {}, click() {}, focus() {},
    getBoundingClientRect() { return { left: 1200, top: 800, right: 1252, bottom: 852 }; },
    setPointerCapture() {}, releasePointerCapture() {}
  };
}

const els = {};
const documentMock = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  addEventListener() {}, querySelectorAll() { return []; },
  body: makeEl("body"), documentElement: makeEl("html")
};

/** 记录所有请求，供"采纳是否真的落库"这类回归断言使用。
 *  `headers` 也要记：登录态是靠 `X-Seller-Token` 头带的，不记就测不出"有没有带" */
const requests = [];

/** 响应脚本：不匹配任何一项就是默认 `200 {}`。
 *  用途：制造 401（会话失效 / 密码错）这类**只有真实后端才会给**的响应。
 *
 *  ⚠️ 命中规则是**按 URL 匹配**，不是按顺序 shift。
 *  因为 `refreshAll()` 是并行发请求的（改这项是为了去掉串行带来的秒级延迟），
 *  谁先回来由事件循环决定 —— 按顺序消费会把 401 派给另一个请求，
 *  结果就是测试时绿时红，而这种"偶发失败"最容易被当成"环境抖动"忽略。
 *  带 `match` 的优先；没写 `match` 的老条目按位置兜底（保持向后兼容）。 */
const fetchPlan = [];

function takePlan(url) {
  const u = String(url);
  for (let i = 0; i < fetchPlan.length; i++) {
    if (fetchPlan[i].match && u.indexOf(fetchPlan[i].match) !== -1) {
      return fetchPlan.splice(i, 1)[0];
    }
  }
  for (let i = 0; i < fetchPlan.length; i++) {
    if (!fetchPlan[i].match) { return fetchPlan.splice(i, 1)[0]; }
  }
  return null;
}

/** localStorage：登录 Token 的落点。真实浏览器里刷新后仍在，这里要能跨请求保留。 */
const storage = {};
const localStorageMock = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
  setItem(k, v) { storage[k] = String(v); },
  removeItem(k) { delete storage[k]; }
};

const sandbox = {
  document: documentMock,
  console,
  location: { search: "", protocol: "http:", origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/ui/seller.html" },
  // window 上真会用到 addEventListener（resize / message），漏了会在 init 里抛错 ——
  // 那个错误是**异步**抛出的，只在 unhandledRejection 时才可见，很容易被"测试全绿"掩盖
  window: {
    innerWidth: 1600, innerHeight: 900, prompt: () => null,
    addEventListener() {}, removeEventListener() {}
  },
  URLSearchParams,
  setTimeout, clearTimeout,
  localStorage: localStorageMock,
  fetch: async (url, init) => {
    requests.push({
      url: String(url),
      method: (init && init.method) || "GET",
      body: init && init.body,
      headers: (init && init.headers) || {}
    });
    const plan = takePlan(url);
    let status = plan ? plan.status : 200;
    let body = plan ? (plan.body || {}) : {};
    // 未登录时 `/auth/me` 真实后端就是 401 —— 默认也这么回，init 才会停在登录窗，
    // 不会在测试过程中偷偷把 state 刷掉（否则断言会随机失败）
    if (!plan && String(url).indexOf("/auth/me") !== -1 && !localStorageMock.getItem("seller.token")) {
      status = 401;
      body = { detail: "登录已失效，请重新登录" };
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify(body)
    };
  },
  FormData: function FormData() {},
  Number, String, Object, Array, Math, JSON, Date, isFinite, encodeURIComponent
};
sandbox.window.document = documentMock;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "seller.js"), "utf8"),
  sandbox,
  { filename: "seller.js" }
);

const S = sandbox;
check("seller.js 已加载且暴露 state", !!S.state && typeof S.renderSide === "function");

// 这些用例验证的是**已登录**后的界面行为，所以先把登录态摆上。
// 登录/退出相关的用例在文末，会自己改这个字段。
S.AUTH.seller = { username: "admin", display_name: "默认卖家", seller_id: "default" };

/* ---------- 中栏滚动容器 mock ----------
 * 真正滚动的是 .mid .body（不是 window），而 renderMid() 是整体替换 innerHTML。
 * 这里让「设置 innerHTML」同时把 scrollTop 归零，才忠实模拟真实 DOM 的行为，
 * 从而能断言「重渲染有没有把人弹回顶部」（见同名回归用例）。 */
const midBody = makeEl("mid.body");
els.mid = makeEl("mid");
els.mid.querySelector = (sel) => (sel === ".body" ? midBody : null);
(function () {
  let html = "";
  Object.defineProperty(els.mid, "innerHTML", {
    get() { return html; },
    set(v) { html = v; midBody.scrollTop = 0; }
  });
})();

/* ---------- 基础工具 ---------- */
check("escapeHtml 转义尖括号与引号",
  S.escapeHtml('<img src=x onerror="a">').indexOf("<") === -1
  && S.escapeHtml('"q"').indexOf('"') === -1);
check("money 固定两位小数", S.money(99) === "99.00" && S.money(9.5) === "9.50" && S.money(undefined) === "0.00");
check("resolveUrl 相对路径拼 API 基址",
  S.resolveUrl("/api/seller/uploads/a.png") === "http://127.0.0.1:8000/api/seller/uploads/a.png",
  S.resolveUrl("/api/seller/uploads/a.png"));
check("resolveUrl 绝对 URL 原样返回",
  S.resolveUrl("https://bucket.cos/x.png") === "https://bucket.cos/x.png");
check("resolveUrl 空值返回空串", S.resolveUrl("") === "");

/* ---------- 右栏重绘也要忠实于真实 DOM ----------
 * 真实浏览器里 `innerHTML = ...` 会**销毁旧节点**，重建出来的 `<select>` 回到第一项。
 * mock 若不模拟这一点，「重绘把语气改回口语」这类 bug 就测不出来
 * （曾真实踩到：aiCopy 在重绘之后读下拉框，读到的已是新节点）。 */
(function () {
  let html = els.right.innerHTML || "";
  Object.defineProperty(els.right, "innerHTML", {
    get() { return html; },
    set(v) { html = v; delete els.copyStyle; }
  });
})();

/* ---------- 标签映射 ---------- */
check("statusBadge 三种状态",
  S.statusBadge("on_shelf").indexOf("已上架") !== -1
  && S.statusBadge("off_shelf").indexOf("已下架") !== -1
  && S.statusBadge("draft").indexOf("草稿") !== -1);
check("actorChip 区分人工与 AI",
  S.actorChip("human").indexOf("卖家") !== -1 && S.actorChip("ai").indexOf("AI") !== -1);
check("actionLabel 映射动作码",
  S.actionLabel("update_price") === "修改价格"
  && S.actionLabel("ai_generate_copy") === "AI 生成文案"
  && S.actionLabel("unknown_x") === "unknown_x");
check("fieldLabel 映射字段名", S.fieldLabel("price") === "价格" && S.fieldLabel("stock_alert") === "预警阈值");
check("shortTime 截断为可读短时间", S.shortTime("2026-10-16T18:44:00").length <= 16);

/* ---------- AI 结果卡（阶段三） ---------- */
console.log("\n--- AI 结果卡 ---");
S.state._aiCopy = { style: "口语", copy: "一杯鲜果汁，出门也能喝。", model: "qwen3.7-max" };
const copyCard = S.aiCopyCard();
check("文案卡含三个动作（采纳/换一版/放弃）",
  copyCard.indexOf("采纳") !== -1 && copyCard.indexOf("换一版") !== -1 && copyCard.indexOf("放弃") !== -1,
  copyCard);
check("文案卡 data-act 齐备",
  copyCard.indexOf('data-act="copy-apply"') !== -1 && copyCard.indexOf('data-act="ai-copy"') !== -1
  && copyCard.indexOf('data-act="copy-drop"') !== -1);
check("文案卡显示风格与模型名",
  copyCard.indexOf("口语") !== -1 && copyCard.indexOf("qwen3.7-max") !== -1);

S.state._aiCopy = null;
check("无结果时不渲染卡片", S.aiCopyCard() === "");

S.state._aiPrice = {
  suggested: 99, min: 95, max: 109, reason: "同类均价 ¥92，成本 ¥38。",
  source: "model", cost_price: 38, margin_percent: 61.6, peer_count: 2, peer_avg: 92
};
const priceCard = S.aiPriceCard();
check("定价卡显示建议价与区间",
  priceCard.indexOf("99.00") !== -1 && priceCard.indexOf("95.00") !== -1 && priceCard.indexOf("109.00") !== -1);
check("定价卡显示成本 / 毛利 / 同类目均价",
  priceCard.indexOf("成本 ¥38.00") !== -1 && priceCard.indexOf("61.6%") !== -1
  && priceCard.indexOf("同类目均价 ¥92.00") !== -1 && priceCard.indexOf("2 件") !== -1);
check("定价卡标注来源=模型建议", priceCard.indexOf("模型建议") !== -1);
check("定价卡含采纳与保持原价",
  priceCard.indexOf('data-act="price-apply"') !== -1 && priceCard.indexOf('data-act="price-drop"') !== -1);

S.state._aiPrice = {
  suggested: 95, min: 87.4, max: 106.4, reason: "按成本估算（未使用模型）",
  source: "rule_fallback", cost_price: 38, margin_percent: 60.0, peer_count: 0, peer_avg: null
};
const fallbackCard = S.aiPriceCard();
check("回落时如实标注「规则估算（模型未参与）」", fallbackCard.indexOf("规则估算") !== -1, fallbackCard);
check("无同类目时显示「无同类目参考」", fallbackCard.indexOf("无同类目参考") !== -1);

S.state._aiPrice = null;
check("无建议时不渲染定价卡", S.aiPriceCard() === "");

/* ---------- 待确认卡（阶段二） ---------- */
console.log("\n--- 待确认卡 ---");
// 旧卡（升级前建的，没有批次号）→ 保持单卡交互
const pend = S.pendingCard({ id: "", items: [{
  id: 7, summary: "删除商品 G-0002", kind: "destructive", tool_name: "delete_goods",
  detail: "将删除「快充数据线」及 1 张图片。删除后无法恢复。"
}] });
check("待确认卡含确认与取消按钮",
  pend.indexOf("确认执行") !== -1 && pend.indexOf("取消") !== -1
  && pend.indexOf('data-ai="confirm"') !== -1 && pend.indexOf('data-ai="cancel"') !== -1, pend);
check("待确认卡显示影响明细",
  pend.indexOf("无法恢复") !== -1, pend);
check("待确认卡不露英文工具名（卖家看的是「要不要执行」，不是函数名）",
  pend.indexOf("delete_goods") === -1 && pend.indexOf("由 AI 助手发起") !== -1, pend);
check("待确认卡对摘要做转义",
  S.pendingCard({ id: "", items: [{ id: 1, summary: "<img src=x onerror=alert(1)>", detail: "", tool_name: "t" }] })
    .indexOf("<img") === -1);

/* ---------- 多步确认卡（一句话里有多件事） ---------- */
// 实测踩到：一句话含「生成宣传图 + 文案」→ 冒出两张卡 → 只点了一张 → 另一张还挂着，
// 卖家以为两件都做完了（任务中心没任务、商品也没图）。
console.log("\n--- 多步确认卡（一次确认执行整批） ---");
const twoSteps = [
  { id: 11, summary: "为商品 G-0015 生成文案候选", kind: "billable",
    tool_name: "generate_copy", confirm_note: "确认后生成一版文案" },
  { id: 12, summary: "为商品 G-0015 生成宣传图", kind: "billable",
    tool_name: "generate_cover", confirm_note: "确认后会建后台任务，约 30—60 秒" }
];
const batchCard = S.pendingCard({ id: "b-abc", items: twoSteps });
check("多步合成**一张**卡（不是两张）",
  batchCard.indexOf("这次有 2 步要你确认") !== -1, batchCard.slice(0, 120));
check("卡内列出每一步的说明",
  batchCard.indexOf("生成文案候选") !== -1 && batchCard.indexOf("生成宣传图") !== -1, batchCard);
check("只有一个「确认执行这 2 步」按钮（点一次全做）",
  batchCard.indexOf('data-ai="batch-confirm"') !== -1
  && batchCard.indexOf("确认执行这 2 步") !== -1
  && (batchCard.match(/data-ai="batch-confirm"/g) || []).length === 1, batchCard);
check("每步都能单独划掉（不做的那步不必被迫执行）",
  (batchCard.match(/data-ai="skip"/g) || []).length === 2, batchCard);
check("批次按钮带批次号（服务端据此一次执行整批）",
  batchCard.indexOf('data-bid="b-abc"') !== -1, batchCard);
check("异步任务在卡上就说清去哪看结果",
  batchCard.indexOf("任务") !== -1 && batchCard.indexOf("30—60 秒") !== -1, batchCard);
check("多步卡标明其中几步会产生费用（总代价可见）",
  batchCard.indexOf("其中 2 步会产生费用") !== -1, batchCard.slice(0, 160));
check("多步卡不写单个工具名（会误导成只做那一步）",
  batchCard.indexOf("来自 generate_cover") === -1, batchCard);

S.CHAT.skipped = { 11: true };
const batchSkipped = S.pendingCard({ id: "b-abc", items: twoSteps });
check("划掉的步骤压暗并标「恢复」",
  batchSkipped.indexOf("pstep skip") !== -1 && batchSkipped.indexOf("恢复") !== -1, batchSkipped);
S.CHAT.skipped = {};

/* ---------- 商品 × 售后联动（阶段四） ---------- */
console.log("\n--- 商品 × 售后联动 ---");
const hp = S.healthPanel([
  { goods_no: "G-0001", title: "便携榨汁杯 350ml", total: 5, refund_rate: 40.0, costly_count: 2 },
  { goods_no: "G-0002", title: "快充数据线 2 米", total: 3, refund_rate: 0.0, costly_count: 0 }
]);
check("健康度卡显示商品名与退货率",
  hp.indexOf("便携榨汁杯 350ml") !== -1 && hp.indexOf("40%") !== -1, hp);
check("健康度卡显示样本量（防小样本误导）",
  hp.indexOf("共 5 单") !== -1 && hp.indexOf("赔付类 2 单") !== -1, hp);
check("零退货率的商品也在榜上", hp.indexOf("快充数据线 2 米") !== -1 && hp.indexOf("0%") !== -1);
check("退货率高的用告警色", hp.indexOf("#b91c1c") !== -1, hp);
check("无关联工单时给出可操作说明（而不是一堆 0）",
  S.healthPanel([]).indexOf("goods_no") !== -1, S.healthPanel([]));

check("判定结果码有中文映射",
  S.OUTCOME_LABELS.refund === "退款" && S.OUTCOME_LABELS.pending_evidence === "需补证"
  && S.OUTCOME_LABELS.processing === "处理中");
check("转人工原因码有中文映射",
  S.REASON_LABELS.tool_failed === "工具失败" && S.REASON_LABELS.buyer_request === "买家要求");
check("跨模块跳转函数已暴露",
  typeof S.openGoodsByNo === "function" && typeof S.bindHostMessages === "function"
  && typeof S.openCaseInAftersale === "function");

/* ---------- 上下文 ---------- */
console.log("\n--- 助手上下文 ---");
S.state.view = "admin";
S.state.sel = { goods_no: "G-0001" };
const ctx = S.aiContext();
check("上下文含视图与选中商品", ctx.view === "admin" && ctx.selected_goods_no === "G-0001", JSON.stringify(ctx));
S.state.sel = null;
check("未选中商品时上下文为空串", S.aiContext().selected_goods_no === "");

/* ---------- 浮窗开关与跟随（阶段：浮窗化） ---------- */
console.log("\n--- 浮窗开关与跟随 ---");
const drawerEl = documentMock.getElementById("drawer");
S.state.view = "overview";
S.toggleDrawer(true);
check("展开时 drawer 有 open 类", drawerEl.classList.contains("open"));
check("展开后按按钮坐标摆位（left/top 已写入）",
  typeof drawerEl.style.left === "string" && drawerEl.style.left.endsWith("px")
  && typeof drawerEl.style.top === "string" && drawerEl.style.top.endsWith("px"),
  JSON.stringify({ left: drawerEl.style.left, top: drawerEl.style.top }));
check("摆位后不再用 right/bottom 锚点（否则会与 left/top 打架）",
  drawerEl.style.right === "auto" && drawerEl.style.bottom === "auto",
  JSON.stringify({ right: drawerEl.style.right, bottom: drawerEl.style.bottom }));

S.toggleDrawer(false);
check("收起时 open 类被移除（display:none 才能真正藏起来）",
  !drawerEl.classList.contains("open"), String(drawerEl.classList.contains("open")));
check("收起后 positionDrawer 仍存在（拖动按钮不会报错）",
  typeof S.positionDrawer === "function");

// 点击 AI 按钮走的是**不带参数**的切换：开着→关、关着→开（与 × 按钮等价）
S.toggleDrawer();
check("不带参数调用 = 切换（关 → 开）", drawerEl.classList.contains("open"));
S.toggleDrawer();
check("再点一次 = 切换（开 → 关）", !drawerEl.classList.contains("open"));

/* ---------- 任务重试 ---------- */
console.log("\n--- 任务重试 ---");
S.state.view = "tasks";
S.state.tasks = [
  { id: 1, kind: "ai_cover", title: "生图失败", status: "failed", retryable: true,
    created_at: "2026-10-19 20:00:00" },
  { id: 2, kind: "bulk_op", title: "批量调价（失败）", status: "failed", retryable: false,
    created_at: "2026-10-19 20:00:00" },
  { id: 3, kind: "ai_cover", title: "生图完成", status: "done", retryable: false,
    created_at: "2026-10-19 20:00:00" }
];
S.renderMid();
const taskHtml = els.mid.innerHTML;
const retryBtns = (taskHtml.match(/data-act="retry-task"/g) || []).length;
check("只有后端标记 retryable 的任务才出现「重试」", retryBtns === 1, String(retryBtns));
check("重试按钮旁提示会再计费", taskHtml.indexOf("计费") !== -1);
check("失败任务显示失败原因", taskHtml.indexOf("批量调价（失败）") !== -1);

/* ---------- 巡检摘要（助手先开口） ---------- */
console.log("\n--- 巡检摘要 ---");
S.CHAT.messages = [];
S.CHAT.pending = [];
S.CHAT.issues = [
  { kind: "low_stock", title: "便携榨汁杯", detail: "库存 3，低于预警 10" },
  { kind: "task_failed", title: "批量调价", detail: "批量部分失败" }
];
S.renderChat();
const chatHtml = els.chat.innerHTML;
check("空对话时显示巡检摘要（有数据才显示）",
  chatHtml.indexOf("巡了一遍") !== -1 && chatHtml.indexOf("2") !== -1, chatHtml.slice(0, 200));
check("巡检项带类型标签与详情",
  chatHtml.indexOf("低库存") !== -1 && chatHtml.indexOf("库存 3") !== -1);
check("巡检摘要说明「不会自动执行」", chatHtml.indexOf("仍会让你确认") !== -1);
S.CHAT.issues = [];
S.renderChat();
check("没有巡检结果时不显示该段", els.chat.innerHTML.indexOf("巡了一遍") === -1);

/* ---------- 回收站 ---------- */
console.log("\n--- 回收站 ---");
S.state.view = "admin";
S.state.goods = [{ id: 1, goods_no: "G-1", title: "正常品", price: 10, stock: 5, stock_alert: 0, status: "on_shelf", media: [] }];
S.state.trash = [
  { id: 9, goods_no: "G-9", title: "已删品", price: 20, days_left: 5, media: [] },
  { id: 10, goods_no: "G-10", title: "快到期品", price: 30, days_left: 0, media: [] }
];
S.state.trashCount = 2;
S.state._status = "";
S.state._adminTab = "trash";
S.renderMid();
const trashHtml = els.mid.innerHTML;
check("商品管理出现「回收站」tab",
  trashHtml.indexOf('data-tab="trash"') !== -1 && trashHtml.includes("回收站"));
check("回收站列出商品与剩余可恢复天数",
  trashHtml.includes("已删品") && trashHtml.includes("5 天"), trashHtml.slice(0, 200));
check("回收站提供「恢复」与「彻底删除」",
  trashHtml.indexOf('data-act="restore-goods"') !== -1
  && trashHtml.indexOf('data-act="purge-goods"') !== -1);
check("说明保留期限（到期自动彻底删除）", trashHtml.includes("到期自动彻底删除"));
check("回收站里不混入正常商品", !trashHtml.includes("正常品"));

S.state._adminTab = "all";
S.renderMid();
check("正常视图不显示已删商品",
  !els.mid.innerHTML.includes("已删品"), els.mid.innerHTML.slice(0, 160));

/* ---------- 待确认角标（数字，而非无信息的红点） ---------- */
console.log("\n--- 待确认角标 ---");
S.CHAT.pending = [{ id: 1 }, { id: 2 }, { id: 3 }];
S.updatePendingDot();
const dotEl = documentMock.getElementById("fabDot");
check("角标显示**数量**而不是纯红点", dotEl.textContent === "3", String(dotEl.textContent));
check("角标有文字说明（知道是什么事）",
  String(dotEl.title).indexOf("3 项") !== -1, String(dotEl.title));
check("有待确认时角标可见", dotEl.hidden === false);

S.CHAT.pending = [];
S.updatePendingDot();
check("没有待确认时角标隐藏", dotEl.hidden === true);

/* ---------- 批量操作的前后对比（人工侧也要能核对） ---------- */
console.log("\n--- 批量操作 diff ---");
const bulkRows = [
  { id: 1, title: "品A", price: 99, stock: 3, stock_alert: 10, status: "on_shelf", media: [] },
  { id: 2, title: "品B", price: 30, stock: 5, stock_alert: 0, status: "draft", media: [{ id: 1 }] }
];

const dPrice = S.bulkDiffHtml(bulkRows, "price_delta", -10);
check("改价 diff 显示「现价 → 改后价」",
  dPrice.html.includes("¥99.00") && dPrice.html.includes("¥89.00"), dPrice.html);
check("改价 diff 有表头", dPrice.html.includes("改后") && dPrice.html.includes("现在"));

const dBad = S.bulkDiffHtml(bulkRows, "price_delta", -100);
check("改后 ≤0 的行标红并计入 bad", dBad.bad === 2 && dBad.html.includes("bad"), String(dBad.bad));
check("非法项给出提示（后端会拒绝，但先让人看见）", dBad.html.includes("≤ 0"), dBad.html);

const dStatus = S.bulkDiffHtml(bulkRows, "on_shelf", 0);
check("上下架 diff 显示状态变化",
  dStatus.html.includes("已上架") && dStatus.html.includes("草稿"), dStatus.html);
check("已是目标状态标「不变」（不制造假改动）", dStatus.html.includes("（不变）"), dStatus.html);

const dDel = S.bulkDiffHtml(bulkRows, "delete", 0);
check("删除 diff 显示将删除的图片数",
  dDel.html.includes("1 图") && dDel.html.includes("删除"), dDel.html);

/* ---------- 商品管理子功能栏（低库存并入） ---------- */
console.log("\n--- 商品管理子功能栏 ---");
check("isLowStock 与后端同口径（断货一律算；阈值 0 不按阈值预警）",
  S.isLowStock({ stock: 3, stock_alert: 10 }) === true        // 低于阈值
  && S.isLowStock({ stock: 50, stock_alert: 10 }) === false   // 正常
  && S.isLowStock({ stock: 0, stock_alert: 0 }) === true      // **断货：即使没设阈值也算**
  && S.isLowStock({ stock: 0, stock_alert: 5 }) === true,     // 断货（也低于阈值）
  JSON.stringify([S.isLowStock({ stock: 3, stock_alert: 10 }), S.isLowStock({ stock: 0, stock_alert: 0 })]));

S.state.goods = [
  { id: 1, goods_no: "G-1", title: "低库存品", price: 10, stock: 3, stock_alert: 10, status: "on_shelf", media: [] },
  { id: 2, goods_no: "G-2", title: "充足品", price: 20, stock: 50, stock_alert: 10, status: "on_shelf", media: [] }
];
S.state.view = "admin";
S.state._status = "";
S.state.checked = {};

S.state._adminTab = "all";
S.renderMid();
const midAll = els.mid.innerHTML;
check("商品管理页含两个子功能栏", midAll.includes("全部商品") && midAll.includes("低库存"), midAll.slice(0, 120));
check("全部视图含两件商品", midAll.includes("G-1") && midAll.includes("G-2"));

S.state._adminTab = "low";
S.renderMid();
const midLow = els.mid.innerHTML;
check("低库存视图只渲染低于阈值的商品（点击 tab 真的生效）",
  midLow.includes("G-1") && !midLow.includes("G-2"), midLow.slice(0, 160));
check("低库存 tab 处于选中态",
  midLow.includes('data-tab="low"') && midLow.indexOf('class="tab on" data-act="admin-tab" data-tab="low"') !== -1,
  midLow.slice(0, 200));

/* ---------- 商品管理：筛选折叠 + 合并列 + 排序 ---------- */
console.log("\n--- 筛选 / 合并列 / 排序 ---");
S.state.view = "admin";
S.state.goods = [
  { id: 1, goods_no: "G-1", title: "便宜品", category: "数码配件", price: 20, stock: 50, stock_alert: 0, status: "on_shelf", media: [] },
  { id: 2, goods_no: "G-2", title: "贵品", category: "厨房小电", price: 200, stock: 2, stock_alert: 10, status: "on_shelf", media: [] },
  { id: 3, goods_no: "G-3", title: "中价品", category: "数码配件", price: 80, stock: 30, stock_alert: 0, status: "draft", media: [] }
];
S.state._status = ""; S.state._adminTab = "all";
S.state._filter = {}; S.state._filterOpen = false; S.state._sort = { key: "", dir: 1 };

S.renderMid();
let h = els.mid.innerHTML;
check("商品信息已合并为一列（不再有独立「编号」列）",
  h.includes("cell-good") && h.indexOf("<th>编号</th>") === -1, h.slice(0, 200));
check("价格 / 库存列头可排序",
  h.indexOf('data-act="sort" data-key="price"') !== -1
  && h.indexOf('data-act="sort" data-key="stock"') !== -1);
check("默认不展开筛选面板（只占一行）", h.indexOf('class="filters open"') === -1);

S.state._filter = { cat: "数码配件" };
S.renderMid();
h = els.mid.innerHTML;
check("类目筛选生效（只剩 2 件）",
  h.includes("G-1") && h.includes("G-3") && !h.includes("G-2"), h.slice(0, 160));
check("生效条件显示为 chip", h.indexOf('class="chip"') !== -1 && h.includes("数码配件"));

S.state._filter = { pmin: "50", pmax: "100" };
S.renderMid();
h = els.mid.innerHTML;
check("价格区间筛选生效（只剩中价品）",
  h.includes("G-3") && !h.includes("G-1") && !h.includes("G-2"), h.slice(0, 160));
check("价格上下限算一个筛选维度", S.activeFilterCount() === 1, String(S.activeFilterCount()));

S.state._filter = { smax: "10" };
S.renderMid();
check("库存上限筛选生效（只剩库存 2 的）",
  els.mid.innerHTML.includes("G-2") && !els.mid.innerHTML.includes("G-1"));

S.state._filter = {};
S.state._sort = { key: "price", dir: 1 };
S.renderMid();
const ascOrder = (els.mid.innerHTML.match(/G-\d/g) || []).join(",");
check("价格升序：G-1 → G-3 → G-2",
  ascOrder.indexOf("G-1") < ascOrder.indexOf("G-3") && ascOrder.indexOf("G-3") < ascOrder.indexOf("G-2"),
  ascOrder);

S.state._sort = { key: "price", dir: -1 };
S.renderMid();
const descOrder = (els.mid.innerHTML.match(/G-\d/g) || []).join(",");
check("再点一下变降序", descOrder.indexOf("G-2") < descOrder.indexOf("G-1"), descOrder);

S.state._filterOpen = true;
S.renderMid();
check("展开后筛选面板可见", els.mid.innerHTML.indexOf('class="filters open"') !== -1);
check("面板含类目 / 价格 / 库存控件",
  els.mid.innerHTML.includes("fltCat") && els.mid.innerHTML.includes("fltPmin")
  && els.mid.innerHTML.includes("fltSmax"));

// 创建时间筛选
S.state._filterOpen = true;
S.state.goods = [
  { id: 1, goods_no: "G-1", title: "旧品", category: "x", price: 10, stock: 5, stock_alert: 0,
    status: "on_shelf", media: [], created_at: "2026-10-16T10:00:00" },
  { id: 2, goods_no: "G-2", title: "新品", category: "x", price: 20, stock: 5, stock_alert: 0,
    status: "on_shelf", media: [], created_at: "2026-10-19T10:00:00" }
];
S.state._filter = { dmin: "2026-10-19", dmax: "2026-10-19" };
S.renderMid();
check("创建时间筛选生效（只剩当天的）",
  els.mid.innerHTML.includes("G-2") && !els.mid.innerHTML.includes("G-1"),
  els.mid.innerHTML.slice(0, 160));
check("时间也算一个筛选维度", S.activeFilterCount() === 1, String(S.activeFilterCount()));
check("面板含时间输入框",
  els.mid.innerHTML.includes("fltDmin") && els.mid.innerHTML.includes("fltDmax"));

S.state._filterOpen = false;
S.state._filter = {};
S.state._sort = { key: "", dir: 1 };

/* ---------- 重渲染不应把人弹回顶部（回归：点一行 → 列表跳回第一行） ---------- */
console.log("\n--- 重渲染保持滚动位置 ---");
S.state.view = "admin";
S.state._adminTab = "all";
S.state._filter = {};
S.renderMid();                 // 首次渲染：没有旧位置
midBody.scrollTop = 320;       // 卖家往下翻了一段
S.render();                    // ← 点一行的真实路径（selectGoods → render）
check("同一视图内重绘保留滚动位置（点一行不会被弹回顶部）",
  midBody.scrollTop === 320, String(midBody.scrollTop));

midBody.scrollTop = 320;
S.renderMid();                 // 勾选 / 撤销等只重绘中栏的路径
check("只重绘中栏也保留滚动位置", midBody.scrollTop === 320, String(midBody.scrollTop));

S.state.view = "gallery";
S.renderMid();                 // 换视图 = 换了数据 → 从顶部开始
check("切换视图后回到顶部", midBody.scrollTop === 0, String(midBody.scrollTop));

S.state.view = "admin";
S.renderMid();
midBody.scrollTop = 200;
S.renderMid({ reset: true });   // 筛选 / 排序：数据集变了，原位置已无意义
check("数据集变化（筛选/排序/搜索）时回到顶部", midBody.scrollTop === 0, String(midBody.scrollTop));

S.state._filter = {};
S.state._sort = { key: "", dir: 1 };

/* ---------- 批量栏常驻占位（回归：勾选一行 → 批量栏突然出现 → 列表跳一格） ---------- */
console.log("\n--- 批量栏常驻占位 ---");
S.state.view = "admin";
S.state._adminTab = "all";
S.state.checked = {};
S.state.sel = null;
S.renderMid();
let bar0 = (els.mid.innerHTML.match(/<div class="bulkbar[^"]*"[\s\S]*?<\/div>\s*<table/) || [""])[0];
check("未勾选时批量栏也渲染（常驻占位，不再是 display:none）",
  bar0.includes('class="bulkbar"') && bar0.includes("已选"), bar0.slice(0, 120));
check("未勾选时批量按钮 disabled（防误点空跑）",
  bar0.includes("disabled") && (bar0.match(/disabled/g) || []).length === 4,
  String((bar0.match(/disabled/g) || []).length));

S.state.checked = { 1: true };
S.renderMid();
let bar1 = (els.mid.innerHTML.match(/<div class="bulkbar[^"]*"[\s\S]*?<\/div>\s*<table/) || [""])[0];
check("勾选后批量栏激活（按钮可用）",
  bar1.includes('class="bulkbar on"') && !bar1.includes("disabled"), bar1.slice(0, 120));
check("批量栏结构不变（只有状态类 / disabled / 计数在变 → 高度恒定不跳格）",
  bar0.replace(/ on| disabled|<b>\d+<\/b>/g, "") === bar1.replace(/ on| disabled|<b>\d+<\/b>/g, ""));

S.state.checked = {};

/* ---------- 右栏「收起」（空态不该出现；有内容才渲染） ---------- */
console.log("\n--- 右栏收起 ---");
S.state.view = "admin";
S.state.sel = null;
S.state.log = null;
S.renderRight();
check("空态不渲染「收起」按钮（没内容可收，摆着只会让人点了疑惑）",
  !els.right.innerHTML.includes("collapse-right"),
  (els.right.innerHTML || "").slice(0, 100));

S.state.sel = { id: 6, goods_no: "G-0001", title: "测试品", status: "on_shelf", price: 99, media: [] };
S.renderRight();
check("有选中内容时才渲染「收起」按钮",
  els.right.innerHTML.includes("collapse-right"),
  (els.right.innerHTML || "").slice(0, 120));

/* ---------- 采纳即落库（回归：曾出现"填了表单但从未写库"） ---------- */
(async function main() {
  /* AI 悬浮按钮：细小位移仍是点击，真正拖动不误触展开。 */
  S.CHAT.messages = [{ role: "assistant", content: "已有消息" }];
  S.bindDrawer();
  const fab = els.fab;
  const pointer = (type, x, y) => fab.listeners[type]({
    pointerId: 1, clientX: x, clientY: y
  });
  pointer("pointerdown", 1200, 800);
  pointer("pointermove", 1202, 802);
  pointer("pointerup", 1202, 802);
  check("AI 按钮轻微抖动仍能展开", S.CHAT.open && els.drawer.classList.contains("open"));
  pointer("pointerdown", 1200, 800);
  pointer("pointermove", 1202, 802);
  pointer("pointerup", 1202, 802);
  check("再次点击 AI 按钮能收起", !S.CHAT.open && !els.drawer.classList.contains("open"));
  pointer("pointerdown", 1200, 800);
  pointer("pointermove", 1220, 820);
  pointer("pointerup", 1220, 820);
  check("拖动 AI 按钮不会误展开", !S.CHAT.open);
  S.CHAT.messages = [];
  console.log("\n--- 采纳即落库（回归） ---");

  const before = requests.length;
  S.state.sel = { id: 6, goods_no: "G-0001", title: "便携榨汁杯 350ml" };
  S.state._aiCopy = { copy: "  一杯鲜果汁，出门也能喝。  ", style: "口语", model: "qwen3.7-max" };
  await S.applyCopy();
  const patches = requests.slice(before).filter((r) => r.method === "PATCH");
  check("采纳文案会发出 PATCH（不是只填表单）", patches.length === 1, JSON.stringify(patches));
  check("PATCH 的 description 就是该文案（且已 trim）",
    patches.length === 1 && JSON.parse(patches[0].body).description === "一杯鲜果汁，出门也能喝。",
    patches.length ? patches[0].body : "没有发出 PATCH");

  const before2 = requests.length;
  S.state.sel = { id: 6, goods_no: "G-0001" };
  S.state._aiPrice = { suggested: 99, min: 95, max: 109, reason: "x", source: "model" };
  await S.applyPrice();
  const patches2 = requests.slice(before2).filter((r) => r.method === "PATCH");
  check("采纳建议价会发出 PATCH", patches2.length === 1, JSON.stringify(patches2));
  check("PATCH 的 price 是建议价",
    patches2.length === 1 && JSON.parse(patches2[0].body).price === 99,
    patches2.length ? patches2[0].body : "没有发出 PATCH");

  // 空生成结果不应落库（避免把商品文案清空）
  const before3 = requests.length;
  S.state.sel = { id: 6, goods_no: "G-0001" };
  S.state._aiCopy = { copy: "   " };
  await S.applyCopy();
  check("空生成结果不落库",
    requests.slice(before3).filter((r) => r.method === "PATCH").length === 0,
    "空文案竟然发了 PATCH");

  // AI 生图（阶段五）：按钮必须打到 /ai/cover
  const before4 = requests.length;
  S.state.sel = { id: 6, goods_no: "G-0001" };
  await S.aiCover();
  const coverPosts = requests.slice(before4)
    .filter((r) => r.method === "POST" && /\/ai\/cover$/.test(r.url));
  check("AI 生图按钮会 POST /ai/cover", coverPosts.length === 1, JSON.stringify(coverPosts));

  /* ---------- 文案语气（回归：选了专业却生成口语） ----------
   * 曾经的 bug 两件事同一个原因：aiCopy() 先 `renderRight()` 再读下拉框，
   * 而重绘把 `<select>` 重建回第一项 → 永远读到"口语"，
   * 于是界面跳回口语、发出去的 style 也是口语。
   * 断言要卡住两点：① 重绘后选项仍停在所选值；② 请求体带的是所选值。 */
  console.log("\n--- 文案语气（回归） ---");
  S.state.view = "gallery";
  S.state.sel = {
    id: 6, goods_no: "G-0001", title: "便携榨汁杯", description: "",
    price: 99, cost_price: 40, stock: 10, stock_alert: 2, category: "厨房",
    status: "on_shelf", pinned: false, sort_no: 0, media: []
  };
  S.state._aiStyle = "专业";
  S.renderRight();
  const rightHtml = els.right.innerHTML || "";
  check("右栏重绘后语气仍停在所选值（不跳回口语）",
    rightHtml.includes('<option value="专业" selected>'), rightHtml.slice(0, 300));
  check("语气选项来自统一常量且与后端一致（口语/专业/促销）",
    S.COPY_STYLES.join(",") === "口语,专业,促销", String(S.COPY_STYLES));

  // 生成时必须把所选语气发出去
  els.copyStyle = makeEl("copyStyle");
  els.copyStyle.value = "专业";
  const beforeStyle = requests.length;
  await S.aiCopy();
  const styleReq = requests.slice(beforeStyle)
    .filter((r) => r.method === "POST" && /\/ai\/copy$/.test(r.url))[0];
  check("生成文案请求体带上所选语气（不是固定口语）",
    !!styleReq && JSON.parse(styleReq.body).style === "专业",
    styleReq ? styleReq.body : "没有发出 /ai/copy");

  // 读不到下拉框时（重绘中途）也不能回落成口语，而要沿用记住的值
  els.copyStyle = undefined;
  S.state._aiStyle = "促销";
  const beforeStyle2 = requests.length;
  await S.aiCopy();
  const styleReq2 = requests.slice(beforeStyle2)
    .filter((r) => r.method === "POST" && /\/ai\/copy$/.test(r.url))[0];
  check("下拉框读不到时沿用记住的语气（不静默变回口语）",
    !!styleReq2 && JSON.parse(styleReq2.body).style === "促销",
    styleReq2 ? styleReq2.body : "没有发出 /ai/copy");
  S.state._aiStyle = "口语";

  /* ---------- 知识库视图（RAG 检索） ---------- */
  S.state.view = "knowledge";
  S.state.knowledge = {
    loaded: true, docs: [], kbs: [], stats: { documents: 0, chunks: 0 },
    q: "", results: null, busy: false
  };
  S.renderMid();
  const kbEmpty = els.mid.innerHTML;
  check("知识库视图渲染出「上传资料」入口与检索框",
    kbEmpty.includes("上传资料") && kbEmpty.includes('id="kbQ"'), kbEmpty.slice(0, 80));
  check("知识库空态引导上传（不是空白页）",
    kbEmpty.includes("还没有资料") && kbEmpty.includes("上传资料"), kbEmpty.slice(-120));

  // 上传卡：目标知识库 + 标签必须在**选文件之前**就能填（否则只能全落默认库）
  check("上传卡有目标知识库与标签输入",
    kbEmpty.includes('id="kbTarget"') && kbEmpty.includes('id="kbTags"'),
    kbEmpty.slice(0, 240));
  check("空知识库时不上来就摆筛选/范围下拉（没得选）",
    !kbEmpty.includes('id="kbFilter"') && !kbEmpty.includes('id="kbScope"'), kbEmpty.slice(0, 240));

  // 有文档 + 有检索结果 + 多个知识库
  S.state.knowledge = {
    loaded: true,
    docs: [
      { doc_id: "d1", filename: "售后政策.md", chunks: 3, kb: "售后政策",
        tags: "退货,时效", created_at: "2026-10-20 10:00:00" },
      { doc_id: "d2", filename: "平台规则.pdf", chunks: 5, kb: "平台规则",
        tags: "", created_at: "2026-10-21 09:00:00" },
    ],
    kbs: [
      { kb: "售后政策", documents: 1, chunks: 3 },
      { kb: "平台规则", documents: 1, chunks: 5 },
    ],
    stats: { documents: 2, chunks: 8, kbs: 2, embedding_model: "text-embedding-v3" },
    q: "退货时限",
    searchKb: "",
    results: [{ filename: "售后政策.md", kb: "售后政策", section_title: "退货政策",
                page_number: 0, similarity: 0.83, text: "退货需在签收后 15 天内提出。" }],
    busy: false
  };
  S.renderMid();
  const kbReady = els.mid.innerHTML;
  check("知识库列出已入库资料与片段数",
    kbReady.includes("售后政策.md") && kbReady.includes("3 个片段"), kbReady.slice(0, 120));
  check("知识库渲染检索结果并标出出处与相关度",
    kbReady.includes("15 天内") && kbReady.includes("相关度") && kbReady.includes("退货政策"),
    kbReady.slice(-200));
  check("知识库检索词重绘后仍在输入框里（不会丢输入）",
    kbReady.includes('value="退货时限"'), kbReady.slice(0, 200));

  // 删除按钮必须带 doc_id（否则点了不知道删哪份）
  check("每份资料都有删除按钮且带文档号",
    kbReady.includes('data-kb="del"') && kbReady.includes('data-id="d1"'),
    kbReady.slice(-200));

  // 多知识库：文件名下面要有库名徽标 + 标签，检索出处要带库名
  check("文档行标出所属知识库徽标",
    kbReady.includes('class="kgtag">售后政策<') && kbReady.includes('class="kgtag">平台规则<'),
    kbReady.slice(-400));
  check("文档行标出标签（逗号拆成独立标签）",
    kbReady.includes('class="kbtag">退货<') && kbReady.includes('class="kbtag">时效<'),
    kbReady.slice(-400));
  check("检索出处带知识库名（同名文件才分得清）",
    kbReady.includes("售后政策 · 售后政策.md"), kbReady.slice(-300));
  check("多库时才出现清单筛选与检索范围下拉",
    kbReady.includes('id="kbFilter"') && kbReady.includes('id="kbScope"'), kbReady.slice(0, 300));

  // 改归属：按钮带 doc_id；点了之后该行切成输入框，其余行不动
  check("每份资料都有「改归属」按钮且带文档号",
    kbReady.includes('data-kb="move"') && kbReady.includes('data-id="d1"'), kbReady.slice(-300));
  S.state.knowledge.moving = "d1";
  S.renderMid();
  const kbMoving = els.mid.innerHTML;
  check("改归属时该行变成输入框 + 保存/取消",
    kbMoving.includes('id="kbMoveTo"') && kbMoving.includes('data-kb="moveSave"')
      && kbMoving.includes('data-kb="moveCancel"'), kbMoving.slice(-400));
  check("改归属只影响那一行（另一行仍有删除按钮）",
    kbMoving.includes('data-kb="del" data-id="d2"'), kbMoving.slice(-400));
  S.state.knowledge.moving = "";

  // 按库筛选后为空：提示必须与"真的没有资料"分开
  S.state.knowledge.docs = [];
  S.state.knowledge.kbFilter = "平台规则";
  S.renderMid();
  const kbFiltered = els.mid.innerHTML;
  check("筛选后为空时说明是筛选造成（不让人以为资料丢了）",
    kbFiltered.includes("「平台规则」里还没有资料") && kbFiltered.includes("全部知识库"),
    kbFiltered.slice(-260));
  S.state.knowledge.kbFilter = "";

  // 侧栏入口存在（视图不加进侧栏 = 用户点不到）
  S.state.overview = { goods: { on_shelf: 1, total: 1 }, recent_operations: [] };
  S.renderSide();
  check("侧栏有「知识库」入口且带文档数角标",
    els.side.innerHTML.includes('data-view="knowledge"') && els.side.innerHTML.includes("知识库"),
    els.side.innerHTML.slice(0, 200));

  // 对话浮窗的回形针：卖家不切视图也能把资料递进知识库
  const htmlSrc = fs.readFileSync(path.join(__dirname, "seller.html"), "utf8");
  check("对话浮窗有上传资料的入口（回形针 + 文件框）",
    htmlSrc.includes('id="btnKbUpload"') && htmlSrc.includes('id="kbUploadInput"'),
    htmlSrc.slice(0, 200));
  const jsSrc = fs.readFileSync(path.join(__dirname, "seller.js"), "utf8");
  check("回形针点击会打开文件框、上传走同一个入库接口",
    jsSrc.includes("attachInput.click()") && jsSrc.includes("/api/seller/knowledge/upload"),
    "缺 attachInput.click() 或入库调用");

  /* ---------- 一次确认 = 整批执行（回归：只点一张 → 只做一半） ---------- */
  console.log("\n--- 批次确认（回归） ---");
  S.CHAT.pending = [
    { id: 21, batch_id: "b-run", summary: "为商品 G-0015 生成文案候选", kind: "billable", tool_name: "generate_copy" },
    { id: 22, batch_id: "b-run", summary: "为商品 G-0015 生成宣传图", kind: "billable", tool_name: "generate_cover" }
  ];
  S.CHAT.skipped = { 22: true };          // 卖家把"生图"那步划掉
  const beforeBatch = requests.length;
  try {
    await S.confirmBatch("b-run");
  } catch (err) {
    check("批次确认能跑通（不抛错）", false, String(err && err.message));
  }
  const batchReqs = requests.slice(beforeBatch)
    .filter((r) => r.method === "POST" && r.url.indexOf("/actions/batch/b-run/confirm") !== -1);
  check("点一次只发一个批次确认请求（不是每步各发一个）",
    batchReqs.length === 1, JSON.stringify(batchReqs.map((r) => r.url)));
  check("被划掉的步骤不会被执行（skip_ids 带上去）",
    batchReqs.length === 1 && JSON.parse(batchReqs[0].body).skip_ids.join(",") === "22",
    batchReqs.length ? batchReqs[0].body : "没有请求");
  check("确认后会重新拉取待确认清单（不留下点不动的卡）",
    requests.slice(beforeBatch).some((r) => r.url.indexOf("/assistant/actions") !== -1
      && r.method === "GET"), "没有重拉 actions");
  check("确认后刷新任务中心（生图进度要能立刻看到）",
    requests.slice(beforeBatch).some((r) => r.url.indexOf("/api/seller/tasks") !== -1),
    "没有刷新 tasks");
  S.CHAT.skipped = {};

  // 全部被划掉时不能"点了什么也没发生"
  S.CHAT.pending = [
    { id: 31, batch_id: "b-all", summary: "A", kind: "billable", tool_name: "generate_copy" }
  ];
  S.CHAT.skipped = { 31: true };
  const beforeAll = requests.length;
  await S.confirmBatch("b-all");
  check("全部步骤被划掉时不发执行请求（并给出提示）",
    !requests.slice(beforeAll).some((r) => r.method === "POST" && r.url.indexOf("/actions/batch/") !== -1),
    JSON.stringify(requests.slice(beforeAll).map((r) => r.url)));
  S.CHAT.skipped = {};
  S.CHAT.pending = [];

  /* ---------- 确认卡：点过之后不再给可点的按钮 ---------- */
  console.log("\n--- 确认卡点击后不再可点 ---");
  // 为什么必须有这组：请求往返几百毫秒，按钮如果还在，卖家会以为没点上而再点一次；
  // 也可能被"是不是要重复执行"吓到。所以**点下去就撤掉按钮**，卡片只留一句状态。
  S.CHAT.pending = [
    { id: 61, batch_id: "b-lock", summary: "把 G-0001 价格改为 89", kind: "destructive" },
    { id: 62, batch_id: "b-lock", summary: "把 G-0001 标题改为 x", kind: "normal" }
  ];
  S.CHAT.skipped = {};
  S.renderChat();
  check("初始状态确认卡有可点按钮",
    els.chat.innerHTML.indexOf('data-ai="batch-confirm"') !== -1);

  const pLock = S.confirmBatch("b-lock");          // 故意不 await：先看"进行中"的界面
  const midHtml = els.chat.innerHTML;
  check("点下确认后按钮立刻消失（不等请求回来）",
    midHtml.indexOf('data-ai="batch-confirm"') === -1
    && midHtml.indexOf('data-ai="batch-cancel"') === -1, midHtml.slice(0, 200));
  check("进行中时步骤上的「跳过」也一并撤掉（卡已提交，不该还能改）",
    midHtml.indexOf('data-ai="skip"') === -1);
  check("按钮的位置换成一句状态文字",
    midHtml.indexOf("正在执行") !== -1 && midHtml.indexOf("submitted") !== -1,
    midHtml.slice(0, 200));
  await pLock;
  check("执行完成后整张卡消失（不残留一张点不动的卡）",
    els.chat.innerHTML.indexOf('data-ai="batch-confirm"') === -1);

  // 失败时不弹回"可点"就等于把这张卡废了：服务端没说办成，卡还在清单里，必须能重试
  S.CHAT.pending = [{ id: 63, batch_id: "b-fail", summary: "删除 G-0009", kind: "destructive" }];
  S.CHAT.skipped = {};
  S.renderChat();
  fetchPlan.push({ match: "/actions/batch/b-fail/confirm", status: 409, body: { detail: "这一步已被处理过" } });
  await S.confirmBatch("b-fail");
  check("执行失败后清掉「已提交」标记",
    !S.CHAT.pendingBusy["b-fail"], JSON.stringify(S.CHAT.pendingBusy));
  S.CHAT.pending = [{ id: 63, batch_id: "b-fail", summary: "删除 G-0009", kind: "destructive" }];
  S.renderChat();
  check("执行失败后按钮恢复（否则这张卡再也点不动）",
    els.chat.innerHTML.indexOf('data-ai="batch-confirm"') !== -1);

  // 升级前建的卡没有批次号，走另一条渲染分支 —— 同样不能留可点按钮
  S.CHAT.pending = [{ id: 71, summary: "删除商品 G-0007", kind: "destructive" }];
  S.CHAT.skipped = {};
  S.renderChat();
  check("旧式单卡初始有按钮", els.chat.innerHTML.indexOf('data-ai="confirm"') !== -1);
  const pSolo = S.confirmAction(71);
  check("旧式单卡点下确认后按钮也没了",
    els.chat.innerHTML.indexOf('data-ai="confirm"') === -1
    && els.chat.innerHTML.indexOf('data-ai="cancel"') === -1,
    els.chat.innerHTML.slice(0, 160));
  await pSolo;
  check("单卡执行完清掉「已提交」标记", !S.CHAT.pendingBusy["solo-71"]);
  S.CHAT.pending = [];
  S.CHAT.pendingBusy = {};
  S.renderChat();

  /* ---------- 批量执行失败后可重试失败项（P1） ---------- */
  console.log("\n--- 失败项可重试 ---");
  // 失败项标 failed 后不再出现在待确认卡里，若不给入口，卖家只能重新说一遍（会把成功项也重做）
  S.CHAT.pending = [
    { id: 81, batch_id: "b-fail2", summary: "生成宣传图 A", kind: "billable", tool_name: "generate_cover" },
    { id: 82, batch_id: "b-fail2", summary: "生成宣传图 B", kind: "billable", tool_name: "generate_cover" }
  ];
  S.CHAT.skipped = {};
  S.renderChat();
  const beforeRetry = requests.length;
  fetchPlan.push({ match: "/actions/batch/b-fail2/confirm", status: 200,
    body: { ok_count: 1, fail_count: 1, skipped_count: 0 } });
  await S.confirmBatch("b-fail2");
  check("失败后记录失败批次", S.CHAT.failedBatch && S.CHAT.failedBatch.count === 1,
    JSON.stringify(S.CHAT.failedBatch));
  check("聊天里出现「重试失败项」入口",
    els.chat.innerHTML.indexOf('data-ai="batch-retry"') !== -1, els.chat.innerHTML.slice(0, 260));

  fetchPlan.push({ match: "/actions/batch/b-fail2/retry", status: 200, body: { reset: 1, ok: true } });
  await S.retryFailedBatch("b-fail2");
  check("重试失败项发对了请求（POST /retry）",
    requests.slice(beforeRetry).some((r) => r.method === "POST"
      && r.url.indexOf("/actions/batch/b-fail2/retry") !== -1),
    JSON.stringify(requests.slice(beforeRetry).map((r) => r.url)));
  check("重试后清掉失败批次标记", S.CHAT.failedBatch === null, JSON.stringify(S.CHAT.failedBatch));
  S.CHAT.pending = [];
  S.CHAT.pendingBusy = {};

  /* ---------- 顶栏（按设计稿删除全局搜索） ---------- */
  console.log("\n--- 顶栏 / 陈列头部 ---");
  const pageSrc = fs.readFileSync(path.join(__dirname, "seller.html"), "utf8");
  check("顶栏不再有全局搜索框（id=topSearch 已移除）",
    pageSrc.indexOf('id="topSearch"') === -1 && pageSrc.indexOf('class="search"') === -1);
  check("顶栏保留连接状态 / 数据源 / 版本号 / 刷新",
    ["id=\"conn\"", "id=\"modeChip\"", "id=\"verChip\"", "id=\"btnRefresh\""].every((k) => pageSrc.indexOf(k) !== -1));
  // 搜索能力没有丢：下沉到陈列页自己的搜索框（否则删掉顶栏搜索就真的没搜索了）
  check("陈列页仍有自己的搜索框", typeof S.applyGallerySearch === "function");

  S.state.view = "gallery";
  S.state._galleryCat = "";
  S.state._galleryQ = "";
  S.state.goods = [
    { id: 1, goods_no: "G-1", title: "迷你电饭煲", category: "厨房小电", price: 199, stock: 8, stock_alert: 0, cover_url: "" },
    { id: 2, goods_no: "G-2", title: "快充数据线", category: "数码配件", price: 29, stock: 100, stock_alert: 0, cover_url: "" },
    { id: 3, goods_no: "G-3", title: "氮化镓充电器", category: "数码配件", price: 129, stock: 3, stock_alert: 10, cover_url: "" }
  ];
  S.renderMid();
  const gh = els.mid.innerHTML;
  check("陈列头是「标题 + 副标题」竖排（设计稿口径）",
    gh.indexOf('class="ghead"') !== -1 && gh.indexOf("像买家一样看到你的橱窗") !== -1, gh.slice(0, 240));
  check("副标题带真实在架件数（不是写死的数字）", gh.indexOf("<b>3</b> 件") !== -1, gh.slice(0, 240));
  check("分类胶囊含「全部」且默认选中",
    gh.indexOf('class="pill on" data-act="gallery-cat" data-cat=""') !== -1);
  check("分类胶囊来自真实类目（厨房小电 / 数码配件）",
    gh.indexOf('data-cat="厨房小电"') !== -1 && gh.indexOf('data-cat="数码配件"') !== -1);
  check("保留了编排模式与新增商品（改版没把功能删掉）",
    gh.indexOf('id="arrangeToggle"') !== -1 && gh.indexOf('data-act="new-goods"') !== -1);

  S.state._galleryCat = "数码配件";
  check("选分类后只留该类目（2 件）",
    S.galleryVisible().length === 2, String(S.galleryVisible().length));
  S.renderMid();
  check("选分类后渲染结果不含其它类目",
    els.mid.innerHTML.indexOf("迷你电饭煲") === -1 && els.mid.innerHTML.indexOf("快充数据线") !== -1);
  check("选中的分类胶囊带 on", els.mid.innerHTML.indexOf('class="pill on" data-act="gallery-cat" data-cat="数码配件"') !== -1);

  S.state._galleryQ = "充电";
  check("搜索与分类**叠加**生效（只剩氮化镓充电器）",
    S.galleryVisible().length === 1 && S.galleryVisible()[0].goods_no === "G-3",
    JSON.stringify(S.galleryVisible().map((g) => g.goods_no)));
  check("搜索词写回输入框（重绘后不丢）",
    (function () { S.renderMid(); return els.mid.innerHTML.indexOf('value="充电"') !== -1; })());
  S.state._galleryCat = "";
  S.state._galleryQ = "";

  /* ---------- 断货：不写「库存 0」，直接标「断货」且要显眼 ---------- */
  // 「库存 0」混在灰色小字里太容易被扫过去，而断货意味着这件商品此刻卖不了。
  S.state._galleryCat = "";
  S.state._galleryQ = "";
  S.state.goods = [
    { id: 11, goods_no: "G-11", title: "显卡", category: "数码配件", price: 8500, stock: 0, stock_alert: 0, cover_url: "" },
    { id: 12, goods_no: "G-12", title: "风扇", category: "生活小电", price: 39, stock: 46, stock_alert: 0, cover_url: "" },
    { id: 13, goods_no: "G-13", title: "充电器", category: "数码配件", price: 129, stock: 3, stock_alert: 10, cover_url: "" }
  ];
  S.renderMid();
  const stkHtml = els.mid.innerHTML;
  check("库存 0 → 显示「断货」而不是「库存 0」",
    stkHtml.indexOf("断货") !== -1 && stkHtml.indexOf("库存 0") === -1, stkHtml.slice(0, 200));
  check("断货带专属样式类（实心红底，不是灰色小字）",
    stkHtml.indexOf('class="stk out">断货') !== -1);
  check("有货仍显示「库存 N」", stkHtml.indexOf(">库存 46<") !== -1);
  check("低库存（3 ≤ 阈值 10）仍走 low，不算断货",
    stkHtml.indexOf('class="stk low">库存 3<') !== -1);
  const cssSrc = fs.readFileSync(path.join(__dirname, "seller.css"), "utf8");
  check("CSS 里 .stk.out 是实心红底白字（与 .stk.low 的红字区分开）",
    /\.stk\.out\{[^}]*background:var\(--danger\)/.test(cssSrc)
    && /\.stk\.out\{[^}]*color:#fff/.test(cssSrc),
    (cssSrc.match(/\.stk\.out\{[^}]*\}/) || [""])[0]);
  check("断货与低库存是两个不同状态（不能把断货也标成 low）",
    S.stockChip({ stock: 0, stock_alert: 5 }).indexOf("out") !== -1
    && S.stockChip({ stock: 5, stock_alert: 5 }).indexOf("low") !== -1);

  /* ---------- 搜索：中文能输入 + 管理页不再吞字 ---------- */
  console.log("\n--- 搜索输入 ---");
  // 陈列页：中文输入法组字期间**不能重绘**，否则输入框被销毁重建、拼音上不了屏。
  // 事件绑定在 mock 里是空实现，所以这里断言源码里的守卫，再单独测函数行为。
  const srcNow = fs.readFileSync(path.join(__dirname, "seller.js"), "utf8");
  check("组字期间直接返回（不在 composition 中重绘）",
    /if \(e\.isComposing \|\| state\._composing\) \{ return; \}/.test(srcNow));
  check("监听了 compositionstart / compositionend",
    srcNow.indexOf('addEventListener("compositionstart"') !== -1
    && srcNow.indexOf('addEventListener("compositionend"') !== -1);
  check("compositionend 后用最终文本过滤（两条路径共用同一个分发函数）",
    /compositionend", function \(e\) \{\s*state\._composing = false;\s*applySearchLike\(e\.target\);/.test(srcNow));

  // 商品管理：搜索改成**本地过滤** —— 原来走后端 q= 并把结果写回 state.goods，
  // 会同时把陈列页/左栏角标换成搜索结果，还会重绘出空输入框（"吞字直接搜"）。
  S.state.view = "admin";
  S.state._adminTab = "all";
  S.state._adminQ = "";
  S.state._galleryQ = "";
  S.state.goods = [
    { id: 21, goods_no: "G-21", title: "氮化镓充电器", category: "数码配件", price: 129, stock: 9, status: "on_shelf" },
    { id: 22, goods_no: "G-22", title: "迷你电饭煲", category: "厨房小电", price: 199, stock: 8, status: "on_shelf" },
    { id: 23, goods_no: "G-23", title: "快充数据线", category: "数码配件", price: 30, stock: 46, status: "on_shelf" }
  ];
  const beforeAdminSearch = requests.length;
  S.applyAdminSearch("充电");
  check("管理页搜索不发请求（改为本地过滤，不再打后端 q=）",
    requests.length === beforeAdminSearch,
    JSON.stringify(requests.slice(beforeAdminSearch).map((r) => r.url)));
  check("管理页搜索不移除 state.goods（三个视图共用同一份数据）",
    S.state.goods.length === 3, String(S.state.goods.length));
  check("管理页搜索词写进 state（重绘后读得回来）", S.state._adminQ === "充电", S.state._adminQ);
  check("管理页搜索框重绘后仍显示关键词（不吞字）",
    els.mid.innerHTML.indexOf('id="adminSearch"') !== -1
    && els.mid.innerHTML.indexOf('value="充电"') !== -1,
    els.mid.innerHTML.slice(0, 200));
  check("管理页只留下命中的商品",
    els.mid.innerHTML.indexOf("氮化镓充电器") !== -1
    && els.mid.innerHTML.indexOf("迷你电饭煲") === -1, els.mid.innerHTML.slice(0, 300));
  S.applyAdminSearch("");
  check("清空搜索后恢复完整列表",
    els.mid.innerHTML.indexOf("迷你电饭煲") !== -1 && els.mid.innerHTML.indexOf("快充数据线") !== -1);
  check("旧的 localSearch（走后端 q= 并覆盖 state.goods）已移除",
    srcNow.indexOf("function localSearch") === -1);

  // 分段匹配（2026-10-21 修复「英伟达5090显卡搜不到英伟达RTX5090显卡」）：
  // 前端本地过滤必须与后端 `goods_repo.keyword_terms` 同口径 —— 整串 indexOf 会把
  // 「这个词没命中」表现成「店里没有」。
  console.log("\n--- 关键词分段匹配 ---");
  check("kwTerms / kwHit 已定义",
    typeof S.kwTerms === "function" && typeof S.kwHit === "function");
  const regGoods = [
    { id: 31, goods_no: "G-0012", title: "英伟达RTX5090显卡", category: "数码配件", price: 8500, stock: 5, status: "on_shelf" },
    { id: 32, goods_no: "G-0011", title: "联想笔记本电脑", category: "数码配件", price: 7900, stock: 20, status: "on_shelf" }
  ];
  S.state.goods = regGoods;
  S.state._galleryCat = "";
  S.state._galleryQ = "英伟达5090显卡";   // 卖家少说 RTX，也必须命中
  const hit1 = S.galleryVisible();
  check("「英伟达5090显卡」能命中「英伟达RTX5090显卡」",
    hit1.length === 1 && hit1[0].goods_no === "G-0012", JSON.stringify(hit1.map((g) => g.goods_no)));
  S.state._galleryQ = "显卡";
  check("单片段「显卡」命中显卡商品", S.galleryVisible().length === 1, String(S.galleryVisible().length));
  S.state._galleryQ = "电饭煲";
  check("不相干词不命中", S.galleryVisible().length === 0, String(S.galleryVisible().length));
  S.state._galleryQ = "";
  // 整串 indexOf 的老写法必须已从两个过滤函数里移除（否则会退化回旧行为）
  check("陈列/管理页过滤不再用整串 indexOf",
    !/\(String\(g\.title \|\| ""\) \+ " " \+ String\(g\.goods_no \|\| ""\)\)\.toLowerCase\(\)\.indexOf/.test(srcNow));

  /* ---------- 左栏账号区 ---------- */
  console.log("\n--- 登录态与账号区 ---");
  S.AUTH.seller = { username: "admin", display_name: "默认卖家", seller_id: "default" };
  S.renderSide();
  check("左栏显示登录账号名与账号",
    els.side.innerHTML.indexOf("默认卖家") !== -1 && els.side.innerHTML.indexOf("admin") !== -1);
  check("左栏有退出登录入口", els.side.innerHTML.indexOf('id="btnLogout"') !== -1);
  check("不再出现「登录接口已预留 · 本轮不做」",
    els.side.innerHTML.indexOf("登录接口已预留") === -1);
  S.AUTH.seller = null;
  S.renderSide();
  check("未登录时不显示任何账号名（不编一个假名字）",
    els.side.innerHTML.indexOf("默认卖家") === -1 && els.side.innerHTML.indexOf("未登录") !== -1);
  S.render();
  check("未登录时中栏是「请先登录」占位，而不是永远转的「加载中」",
    els.mid.innerHTML.indexOf("请先登录") !== -1 && els.mid.innerHTML.indexOf("加载中") === -1,
    els.mid.innerHTML.slice(0, 120));
  check("未登录时左栏不渲染数字角标（0 不是事实）",
    els.side.innerHTML.indexOf('class="n ') === -1, els.side.innerHTML.slice(0, 200));
  S.AUTH.seller = { username: "admin", display_name: "默认卖家", seller_id: "default" };

  /* ---------- Token 随请求走 ---------- */
  S.setAuthToken("tok-1");
  await S.api("/api/seller/goods");
  check("登录后每个请求都带 X-Seller-Token",
    requests[requests.length - 1].headers["X-Seller-Token"] === "tok-1",
    JSON.stringify(requests[requests.length - 1].headers));
  S.setAuthToken("");
  await S.api("/api/seller/goods");
  check("未登录时不带该请求头",
    !("X-Seller-Token" in requests[requests.length - 1].headers));
  check("清 Token 会真的从 localStorage 删掉", S.authToken() === "");

  /* ---------- 刷新链路必须并行（回归：去掉"点一下卡一秒"） ---------- */
  // 串行版实测：一次点击 1.62s / 39 条 MySQL 连接，其中逐商品 N+1 占 22 条。
  // 前端这一侧只负责**别把互不依赖的请求排成一队**；
  // 后端 N+1 已改成单条聚合（见 backend/tests/test_aftersale_stats.py 的 B10）。
  const beforeRefresh = requests.length;
  const refreshP = S.refreshAll();
  const firedSync = requests.length - beforeRefresh;   // await 之前就发出去的请求数
  await refreshP;
  check("refreshAll 并行发出请求（串行实现只会先发 1 个）",
    firedSync >= 3, `同步发出 ${firedSync} 个`);
  const refreshUrls = requests.slice(beforeRefresh).map((r) => r.url);
  check("刷新覆盖左栏角标的三个来源（概览 / 售后 / 任务）",
    refreshUrls.some((u) => u.indexOf("/api/seller/overview") !== -1)
    && refreshUrls.some((u) => u.indexOf("/api/seller/tasks") !== -1)
    && refreshUrls.some((u) => u.indexOf("/api/console/cases") !== -1),
    JSON.stringify(refreshUrls));
  // 只看 refreshAll 的**函数体**：init() 里那句 `await loadHealth()` 是单次探活，正常。
  const rStart = jsSrc.indexOf("async function refreshAll()");
  const refreshBody = rStart === -1 ? "" : jsSrc.slice(rStart, jsSrc.indexOf("\n}", rStart));
  check("refreshAll 源码里不再逐个 await（无串行 load 链）",
    refreshBody.indexOf("await load") === -1 && refreshBody.indexOf("Promise.all(jobs)") !== -1,
    refreshBody.slice(0, 160));
  check("点导航先刷新左栏高亮与面包屑，再取数（避免「点了没反应」）",
    /renderSide\(\);\s*updateCrumb\(\);\s*refreshAll\(\)/.test(jsSrc));

  /* ---------- 401 → 就地弹登录窗（不跳转页面） ---------- */
  // 首次进来（从没登录过）：弹窗但不甩红字 —— 什么都没发生，不该像报错
  fetchPlan.push({ match: "/api/seller/goods", status: 401, body: { detail: "登录已失效，请重新登录" } });
  const rAnon = await S.api("/api/seller/goods");
  check("没登录过时收到 401：弹窗但**不**显示错误红字",
    rAnon.status === 401 && els.auth.hidden === false
    && !String(S.$("authErr").textContent), String(S.$("authErr").textContent));

  // 有 Token 却 401 = 会话过期：这时候必须说清楚，否则像"点了没反应"
  S.setAuthToken("tok-stale");
  fetchPlan.push({ match: "/api/seller/goods", status: 401, body: { detail: "登录已失效，请重新登录" } });
  const r401 = await S.api("/api/seller/goods");
  check("收到 401 就把登录浮窗打开", r401.status === 401 && els.auth.hidden === false);
  check("会话过期时显示「登录已失效」",
    String(S.$("authErr").textContent).indexOf("登录已失效") !== -1,
    String(S.$("authErr").textContent));
  S.setAuthToken("");
  check("浮窗是覆盖层（不跳转新界面）",
    pageSrc.indexOf('class="auth" id="auth"') !== -1 && pageSrc.indexOf('id="authForm"') !== -1);

  /* ---------- 登录：失败与成功 ---------- */
  S.showAuth("请先登录");
  fetchPlan.push({ match: "/auth/login", status: 401, body: { detail: "账号或密码不正确" } });
  S.$("authUser").value = "admin";
  S.$("authPass").value = "wrong";
  await S.doLogin();
  check("密码错时**不**把浮窗当成会话失效关掉",
    els.auth.hidden === false && String(els.authErr.textContent).indexOf("账号或密码不正确") !== -1,
    String(els.authErr.textContent));
  check("密码错时不写 Token", S.authToken() === "");

  // 登录前先放一段"上一段对话"，用于验证登录后真的清空（= 新会话）
  S.CHAT.messages = [{ role: "seller", content: "上一段对话" }];
  S.CHAT.pending = [{ id: 9, batch_id: "b-old", summary: "旧卡" }];
  const beforeLogin = requests.length;
  fetchPlan.push({
    match: "/auth/login",
    status: 200,
    body: {
      token: "tok-new",
      thread_id: "t-fresh",
      seller: { username: "admin", display_name: "默认卖家", seller_id: "default" }
    }
  });
  S.$("authUser").value = "admin";
  S.$("authPass").value = "admin123";
  await S.doLogin();
  check("登录成功：Token 落盘", S.authToken() === "tok-new", S.authToken());
  check("登录成功：浮窗关闭", els.auth.hidden === true);
  check("登录成功：记住会话线程号（后端分配）", S.AUTH.threadId === "t-fresh", S.AUTH.threadId);
  check("登录成功：左栏显示当前账号", els.side.innerHTML.indexOf("默认卖家") !== -1);
  check("登录成功：对话被清空 —— **每次登录都是新对话**",
    S.CHAT.messages.length === 0 && S.CHAT.pending.length === 0,
    JSON.stringify(S.CHAT.messages));
  check("登录成功：重新拉取了自己的数据（不沿用登录前）",
    requests.slice(beforeLogin).some((r) => r.url.indexOf("/api/seller/goods") !== -1));
  check("登录成功：把密码从输入框清掉", S.$("authPass").value === "");

  /* ---------- 退出登录 ---------- */
  fetchPlan.push({ match: "/auth/logout", status: 200, body: { ok: true } });
  await S.doLogout();
  check("退出登录：清掉 Token", S.authToken() === "");
  check("退出登录：重新弹出登录框", els.auth.hidden === false);
  check("退出登录：界面上不残留上一个账号",
    els.side.innerHTML.indexOf("默认卖家") === -1);
  check("退出登录：请求走 /auth/logout（后端会真失效）",
    requests.some((r) => r.url.indexOf("/api/seller/auth/logout") !== -1));

  console.log(`\n结果：${failures.length ? failures.length + " 项失败" : "全部通过"}`);
  failures.forEach((f) => console.log("  - " + f));
  process.exit(failures.length ? 1 : 0);
})();
