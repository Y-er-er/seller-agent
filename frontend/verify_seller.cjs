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
    addEventListener() {}, setAttribute() {}, getAttribute() { return null; },
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

/** 记录所有请求，供"采纳是否真的落库"这类回归断言使用。 */
const requests = [];

const sandbox = {
  document: documentMock,
  console,
  location: { search: "", protocol: "http:", origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/ui/seller.html" },
  window: { innerWidth: 1600, innerHeight: 900, prompt: () => null },
  URLSearchParams,
  setTimeout, clearTimeout,
  fetch: async (url, init) => {
    requests.push({
      url: String(url),
      method: (init && init.method) || "GET",
      body: init && init.body
    });
    return { ok: true, status: 200, text: async () => "{}" };
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
const pend = S.pendingCard({ id: 7, summary: "删除商品 G-0002", detail: "将删除「快充数据线」及 1 张图片。删除后无法恢复。", tool_name: "delete_goods" });
check("待确认卡含确认与取消按钮",
  pend.indexOf("确认执行") !== -1 && pend.indexOf("取消") !== -1
  && pend.indexOf('data-ai="confirm"') !== -1 && pend.indexOf('data-ai="cancel"') !== -1);
check("待确认卡显示影响明细与工具名",
  pend.indexOf("无法恢复") !== -1 && pend.indexOf("delete_goods") !== -1);
check("待确认卡对摘要做转义",
  S.pendingCard({ id: 1, summary: "<img src=x onerror=alert(1)>", detail: "", tool_name: "t" })
    .indexOf("<img") === -1);

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

/* ---------- 商品管理子功能栏（低库存并入） ---------- */
console.log("\n--- 商品管理子功能栏 ---");
check("isLowStock 与后端同口径（阈值 0 不预警）",
  S.isLowStock({ stock: 3, stock_alert: 10 }) === true
  && S.isLowStock({ stock: 50, stock_alert: 10 }) === false
  && S.isLowStock({ stock: 0, stock_alert: 0 }) === false,
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

  console.log(`\n结果：${failures.length ? failures.length + " 项失败" : "全部通过"}`);
  failures.forEach((f) => console.log("  - " + f));
  process.exit(failures.length ? 1 : 0);
})();
