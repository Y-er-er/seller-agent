/* ============================================================
   商铺经营Agent —— 前端逻辑（免构建）
   ============================================================
   结构：state → api → 视图渲染 → 事件委托 → 数据加载 → init

   两条约定：
   1. **右栏是上下文面板**，内容随「当前模块 + 选中对象」变化（不是固定的编辑商品）；
      概览与售后不显示右栏。
   2. **删除一律经弹窗确认**，且请求带 `confirm=true` —— 后端也强制，
      所以"确认"不是装饰，漏传会被后端 400。
   ============================================================ */

var DEFAULT_API_BASE = "http://127.0.0.1:8000";

/** 前端版本号（免构建静态页没有文件哈希，浏览器可能一直用旧缓存 —— 顶栏直接显示，
 *  让"我改了但页面没生效"一眼可见。**每次改完 seller.js 必须同步更新这里**
 *  以及 seller.html 里 script 标签的 `?v=`（两者保持一致）。 */
var FRONTEND_VERSION = "1104vue1";

/* ---------- 登录态 ----------
 * Token 存 `localStorage`，随每个请求走 `X-Seller-Token` 头。
 * 为什么不用 Cookie：控制台可以用 file:// 直接打开，跨源 Cookie 需要后端
 * `allow_credentials` + 精确 origin，而当前 CORS 是 `*`（本地开发）—— 两者不能共存。
 * 取舍与风险写在 `backend/app/seller/deps.py` 的文件头。
 *
 * ⚠️ `seller` / `threadId` 只是**给界面用的缓存**；"还能不能用"一律以请求返回 401 为准，
 *    不要靠这两个字段自己判断登录是否有效（过期时它们看起来仍然正常）。 */
var AUTH = { seller: null, threadId: "", open: false };

function authToken() {
  try { return localStorage.getItem("seller.token") || ""; } catch (e) { return ""; }
}

function setAuthToken(token) {
  try {
    if (token) { localStorage.setItem("seller.token", token); }
    else { localStorage.removeItem("seller.token"); }
  } catch (e) { /* 隐私模式禁写：本次会话内存里仍可用，只是刷新后要重新登录 */ }
}

//: 文案语气候选。**必须与后端 `app/goods/ai.py::COPY_STYLES` 一致** ——
//: 前端传了后端不认的值会直接 400（报"不支持的风格"），且只在真点生成时才暴露。
//: 顺序≠集合：这里的顺序只是界面上的排列（把最常用的"口语"放第一）。
var COPY_STYLES = ["口语", "专业", "促销"];

var API_BASE = (function () {
  var q = new URLSearchParams(location.search);
  if (q.get("api")) { return q.get("api"); }        // ?api= 显式覆盖优先
  // 由后端托管时（/ui/seller.html）用**同源地址** —— 这样后端换端口也不必改前端代码；
  // 硬编码 8000 会在后端跑在其它端口时全部 Failed to fetch（实测踩到）。
  if (location.protocol === "http:" || location.protocol === "https:") { return location.origin; }
  return DEFAULT_API_BASE;                           // file:// 打开时回落默认端口
})();

var state = {
  view: "overview",     // overview / gallery / admin / aftersale / audit
  goods: [],            // 当前列表（陈列或管理）
  sel: null,            // 选中的商品详情（含 media）
  log: null,            // 选中的操作日志
  overview: null,
  logs: [],
  checked: {},          // 表格勾选的 goods id
  aftersaleCount: 0,    // 已转人工的售后工单数（左栏角标，查不到则为 0）
  _aiBusy: false,       // AI 生成中（用于禁用按钮，避免重复点击产生多次计费）
  rightOpen: (function () {
    // 记住上次的展开/收起状态：收起过就一直收起，不用每次刷新都再收一遍
    try { return localStorage.getItem("seller.rightOpen") !== "0"; } catch (e) { return true; }
  })(),
  _adminTab: "all",     // 商品管理内的子功能栏：all / low（低库存是它的一个视图，不是并列项）
  _filterOpen: false,   // 商品管理：筛选面板是否展开（默认折叠，只占一行）
  _filter: {},          // 生效中的筛选：{cat, pmin, pmax, smin, smax}
  _sort: { key: "", dir: 1 },  // 商品管理排序：key=price/stock，dir=1 升序 / -1 降序
  trash: [],            // 回收站商品列表
  trashCount: 0,        // 回收站数量（tab 角标）
  tasks: [],            // 任务列表（长任务 + 批量操作记录）
  taskRunning: 0,       // 进行中的任务数（左栏角标）
  knowledge: {          // 知识库：文档清单 + 统计 + 试检索结果
    loaded: false,      // 是否已拉过（首次切到该视图时才拉，避免每次刷新都请求）
    docs: [],
    stats: null,
    kbs: [],            // 知识库清单（按文档聚合：名称 / 份数 / 片段数）
    kbFilter: "",       // 文档清单的筛选：空 = 全部
    searchKb: "",       // 试检索的范围：空 = 全部知识库
    uploadKb: "",       // 上传时选择的目标知识库：空 = 默认知识库
    tags: "",           // 上传时填的标签（逗号分隔）
    moving: "",         // 正在改归属的文档号（该行切成输入框，其余行不动）
    q: "",              // 检索词（重绘后要回填，否则输入内容会丢）
    results: null,      // null = 还没搜过；[] = 搜过但没结果
    busy: false
  },
  _aiCopy: null,        // 文案生成结果（候选，未采纳）
  _aiPrice: null,       // 定价建议结果（候选，未采纳）
  // 文案语气（口语 / 专业 / 促销）。**必须存进 state**：
  // 生成期间会重绘右栏，`<select>` 被重建后没有任何回填依据 → 每次都跳回第一项"口语"，
  // 于是"选了专业却生成口语"（用户实测踩到）。界面上看到的选择与真正发出去的值必须同一个来源。
  _aiStyle: "口语",
  arrange: false,       // 陈列编排模式
  // 陈列页的分类筛选与关键词：**存进 state 而不是读输入框** ——
  // 重绘会重建 DOM，读输入框等于读默认值（"点了分类，搜索词自己清空"就是这样来的）
  _galleryCat: "",
  _galleryQ: "",
  _adminQ: "",          // 商品管理页搜索词（与陈列同一套：本地过滤 + 存 state，不覆盖 state.goods）
  _composing: false,    // 输入法正在组字（中文/日文）：此时**绝不重绘**，否则组合被打断
  busy: false,
  lastError: "",
  onOk: null            // 弹窗确认回调
};

/* ---------- 工具 ---------- */

function $(id) { return document.getElementById(id); }

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function money(value) {
  var n = Number(value || 0);
  return n.toFixed(2);
}

function toast(msg, isErr) {
  var el = $("toast");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.classList.add("on");
  clearTimeout(el._tm);
  el._tm = setTimeout(function () { el.classList.remove("on"); }, isErr ? 3200 : 1800);
}

function setConn(ok, text) {
  var el = $("conn");
  el.classList.remove("online", "offline");
  el.classList.add(ok ? "online" : "offline");
  $("connText").textContent = text;
}

function askConfirm(opts, onOk) {
  $("dlgTitle").textContent = opts.title || "确认操作";
  $("dlgText").textContent = opts.text || "";
  var box = $("dlgBox");
  if (opts.box) { box.innerHTML = opts.box; box.classList.add("on"); } else { box.classList.remove("on"); }
  $("dlgIco").textContent = opts.ico || "!";
  state.onOk = onOk;
  $("mask").classList.add("open");
}

function closeConfirm() {
  $("mask").classList.remove("open");
  state.onOk = null;
}

/* ---------- API ---------- */

async function api(path, opts) {
  var init = { method: (opts && opts.method) || "GET", headers: {} };
  var token = authToken();
  if (token) { init.headers["X-Seller-Token"] = token; }
  if (opts && opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (opts && opts.form) { init.body = opts.form; }   // FormData：浏览器自设 Content-Type
  try {
    var res = await fetch(API_BASE + path, init);
    var text = await res.text();
    var data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      var errText = typeof detail === "object"
        ? String(detail.message || detail.code || ("HTTP " + res.status))
        : String(detail || ("HTTP " + res.status));
      // 401 = 没登录 / 会话过期 → **就地弹登录窗**，不再逐个 toast 报错。
      // `noAuthGate` 给登录请求自己用：登录失败也是 401，不能把弹窗当成"会话失效"处理
      // （会把刚输入的账号密码清掉，看起来像"点了没反应"）。
      if (res.status === 401 && !(opts && opts.noAuthGate)) {
        // 有 Token 却说 401 = 会话真的过期了，值得说一句；
        // 压根没登录过就不要先甩一条红字（第一次打开页面不该像"出错了"）。
        showAuth(authToken() ? "登录已失效，请重新登录" : "");
      }
      return { ok: false, status: res.status, error: errText };
    }
    return { ok: true, status: res.status, data: data };
  } catch (err) {
    return { ok: false, status: 0, error: "无法连接后端：" + (err && err.message ? err.message : err) };
  }
}

/* ---------- 登录浮窗 ---------- */

function showAuth(message) {
  if (!AUTH.open) {
    AUTH.open = true;
    var box = $("auth");
    if (box) { box.hidden = false; }
    if (message) { authError(message); }
    var u = $("authUser");
    if (u && !u.value) { try { u.focus(); } catch (e) { /* 忽略 */ } }
  } else if (message) {
    authError(message);
  }
}

function hideAuth() {
  AUTH.open = false;
  var box = $("auth");
  if (box) { box.hidden = true; }
  authError("");
}

function authError(text) {
  var el = $("authErr");
  if (!el) { return; }
  el.textContent = text || "";
  el.hidden = !text;
}

/** 登录。成功后**原地接管**（不刷新页面）：清本地数据 → 拉新数据 → 渲染。 */
async function doLogin(event) {
  if (event && event.preventDefault) { event.preventDefault(); }
  var user = ($("authUser").value || "").trim();
  var pass = $("authPass").value || "";
  if (!user || !pass) { authError("请输入账号和密码"); return; }
  var btn = $("authSubmit");
  btn.disabled = true;
  btn.textContent = "登录中…";
  authError("");
  // `noAuthGate`：这里 401 是"密码不对"，不是"会话失效"，交给下方统一提示
  var r = await api("/api/seller/auth/login",
    { method: "POST", body: { username: user, password: pass }, noAuthGate: true });
  btn.disabled = false;
  btn.textContent = "登录";
  if (!r.ok) { authError(r.error || "登录失败"); return; }

  setAuthToken(r.data.token);
  AUTH.seller = r.data.seller;
  AUTH.threadId = r.data.thread_id;
  $("authPass").value = "";                 // 不把密码留在 DOM 里
  hideAuth();
  await afterLogin();
  toast("欢迎回来，" + ((r.data.seller && r.data.seller.display_name) || user));
}

async function doLogout() {
  await api("/api/seller/auth/logout", { method: "POST" });
  setAuthToken("");
  AUTH.seller = null;
  AUTH.threadId = "";
  // 清空界面数据：退出后如果不擦掉，下一屏会**短暂显示上一个账号的店铺**
  state.goods = []; state.logs = []; state.overview = null; state.sel = null;
  state.log = null; state.tasks = []; state.checked = {};
  CHAT.messages = []; CHAT.pending = []; CHAT.skipped = {};
  CHAT.pendingBusy = {};   // 退出后残留的"已提交"标记会污染下一次登录的卡
  CHAT.failedBatch = null;
  render();
  showAuth("已退出登录，请重新登录");
}

/** 登录成功后统一入口：新会话是**新线程**，所以对话从空白开始。 */
async function afterLogin() {
  await refreshAll();
  render();
  await loadChat();      // 新线程 → 空历史；打开抽屉即是"新对话"
}

/* ---------- 数据加载 ---------- */

async function loadHealth() {
  var r = await api("/api/health");
  if (!r.ok) { setConn(false, "后端不可达"); return; }
  setConn(true, "已连接");
  var ds = (r.data && r.data.data_source) || {};
  var st = (r.data && r.data.storage) || {};
  var chip = $("modeChip");
  var mode = ds.mode || "?";
  chip.textContent = "数据源：" + mode + "（" + (ds.declared || "?") + "）· 存储：" + (st.backend || "?");
  chip.className = "mode " + (mode === "local" ? "local" : "remote");
}

async function loadOverview() {
  var r = await api("/api/seller/overview");
  if (!r.ok) { state.lastError = r.error; state.overview = null; return; }
  state.overview = r.data;
}

async function loadGoods() {
  var scope = state.view === "gallery" ? "shelf" : "all";
  var r = await api("/api/seller/goods?scope=" + scope + "&limit=200");
  if (!r.ok) { state.lastError = r.error; state.goods = []; return; }
  state.goods = r.data.items || [];
}

async function loadLogs() {
  var r = await api("/api/seller/logs?limit=100");
  if (!r.ok) { state.lastError = r.error; state.logs = []; return; }
  state.logs = r.data.items || [];
}

/** 拉取"已转人工"的售后工单数，用于左栏角标。
 *  查不到就保持 0（不显示角标）—— 宁可没有角标，也不要显示一个**不真实的数字**。 */
async function loadAftersaleCount() {
  var r = await api("/api/console/cases?status=escalated");
  state.aftersaleCount = r.ok ? (r.data.items || []).length : 0;
}

async function selectGoods(goodsId) {
  var r = await api("/api/seller/goods/" + goodsId);
  if (!r.ok) { toast(r.error, true); return; }
  state.sel = r.data;
  state.log = null;
  state._aiCopy = null;      // 换商品时清掉上一件的 AI 结果，避免"张冠李戴"
  state._aiPrice = null;
  render();
}

function clearSel() { state.sel = null; state.log = null; render(); }

/* ---------- 渲染：左栏 ---------- */

function renderSide() {
  var ov = state.overview || {};
  var counts = (ov.goods || {});
  var ICONS = {
    overview: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    gallery: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/>',
    admin: '<path d="M4 5h16M4 12h16M4 19h16"/><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="7" cy="19" r="1.5"/>',
    orders: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    service: '<path d="M4 5h16v12H9l-5 4z"/><path d="M8 10h8M8 13h5"/>',
    aftersale: '<path d="M5 4h9l4 4v12H5z"/><path d="M14 4v4h4"/>',
    tasks: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
    audit: '<path d="M12 3v18M3 12h18"/><circle cx="12" cy="12" r="2"/>',
    // 知识库：书本（卖家自己的资料库，不是"商品"也不是"服务"）
    knowledge: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v14H6.5A2.5 2.5 0 0 0 4 19.5z"/><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20v4H6.5A2.5 2.5 0 0 1 4 19.5z"/>'
  };
  var kbDocs = ((state.knowledge || {}).stats || {}).documents || 0;
  // 未登录时**一个数字都不显示**：0 是"确实没有"，而这时是"还没查到"
  var loggedIn = !!AUTH.seller;
  var items = [
    { grp: "总览" },
    { view: "overview", name: "概览" },
    { grp: "经营" },
    { view: "gallery", name: "商品陈列", n: loggedIn ? (counts.on_shelf || 0) : "" },
    { view: "admin", name: "商品管理", n: loggedIn ? (counts.total || 0) : "" },
    { view: "orders", name: "订单" },
    // 知识库：卖家自建的规则/政策/资料，AI 答题时引用它（文档数为角标）
    { view: "knowledge", name: "知识库", n: kbDocs > 0 ? kbDocs : "" },
    { grp: "服务" },
    { view: "service", name: "客服" },
    { view: "aftersale", name: "售后", n: state.aftersaleCount > 0 ? state.aftersaleCount : "" },
    // 任务：长任务与批量操作的去处；有进行中任务时角标显示数量
    { view: "tasks", name: "任务", n: state.taskRunning > 0 ? state.taskRunning : "" },
    { view: "audit", name: "操作日志", n: loggedIn ? ((ov.recent_operations || []).length || "") : "" }
  ];
  // ⚠️ 「低库存」曾在这里作为**并列一级项**插入，且 view 也写成 "admin" →
  //    与「商品管理」同时高亮，点它也不改变筛选（只把 view 设成 admin）。
  //    现改为商品管理页内的**子功能栏**（见 bodyAdmin 的 .tabs），此处不再插入。

  var html = "";
  items.forEach(function (it) {
    if (it.grp) { html += '<div class="grp">' + escapeHtml(it.grp) + "</div>"; return; }
    var on = it.view === state.view ? " on" : "";
    var badge = it.n !== "" && it.n !== undefined
      ? '<span class="n ' + (it.cls || "") + '">' + escapeHtml(it.n) + "</span>" : "";
    var icon = ICONS[it.view] ? '<svg viewBox="0 0 24 24">' + ICONS[it.view] + "</svg>" : "";
    html += '<a class="' + on.trim() + '" data-view="' + it.view + '">' + icon + escapeHtml(it.name) + badge + "</a>";
  });
  // 底部账号区：名字来自**后端会话**（`/auth/me`），不是前端猜的。
  // 未登录时显示「未登录」，不显示任何账号名 —— 显示一个假名字比空着更容易误导。
  var who = AUTH.seller
    ? '<div class="who">当前卖家：<b>' + escapeHtml(AUTH.seller.display_name || AUTH.seller.username) + "</b>"
      + '<div class="tag">账号 ' + escapeHtml(AUTH.seller.username) + "</div>"
      + '<button class="out" id="btnLogout">退出登录</button></div>'
    : '<div class="who">未登录<div class="tag">请先登录</div></div>';
  html += who;
  $("side").innerHTML = html;
}

/* ---------- 渲染：中栏 ---------- */

/**
 * 中栏滚动位置的"记忆"。
 *
 *  ⚠️ 坑：`renderMid()` 是整体替换 innerHTML → 滚动容器被重建 → scrollTop 归零。
 *     后果：卖家在列表里往下翻到第 30 行，点一下只是想看详情，却被弹回列表最顶部
 *     （看起来像界面抽了一下、甚至像"没点上"）。
 *
 *  规则：**同一视图内**重绘（选中行 / 勾选 / 撤销 / 任务轮询…）→ 还原滚动位置；
 *        只有"换了数据集"的操作（切视图、切子功能栏、改筛选、排序、搜索）→ 回到顶部。
 */
var _midScroll = { view: "", top: 0 };

/**
 * @param {Object} [opts] opts.reset = true → 强制回到顶部（数据集变了，原位置已无意义）
 */
function renderMid(opts) {
  var reset = !!(opts && opts.reset);
  var box = $("mid").querySelector(".body");
  // 换视图 / 显式要求重置时，不保留旧位置
  var keep = (!reset && _midScroll.view === state.view && box) ? box.scrollTop : 0;

  var hd, body;
  if (state.view === "overview") { hd = hdOverview(); body = bodyOverview(); }
  else if (state.view === "gallery") { hd = hdGallery(); body = bodyGallery(); }
  else if (state.view === "admin") { hd = hdAdmin(); body = bodyAdmin(); }
  else if (state.view === "tasks") { hd = hdTasks(); body = bodyTasks(); }
  else if (state.view === "knowledge") { hd = hdKnowledge(); body = bodyKnowledge(); }
  else if (state.view === "orders" || state.view === "service") {
    hd = commerceHeading(state.view); body = commerceBody(state.view);
  }
  else if (state.view === "audit") { hd = hdAudit(); body = bodyAudit(); }
  else { hd = hdAftersale(); body = bodyAftersale(); }
  $("mid").innerHTML = '<div class="hd">' + hd + '</div><div class="body">' + body + "</div>";

  var nb = $("mid").querySelector(".body");
  if (nb && keep) { nb.scrollTop = keep; }
  _midScroll.view = state.view;
  _midScroll.top = nb ? nb.scrollTop : 0;
  // ⚠️ 拖拽监听到挂在 `#galleryGrid` 这个**节点**上，而这里每次都重建它 ——
  //    必须在重绘之后重新绑，否则"切一次分类，拖拽就失灵"（曾经只在 render() 里绑）。
  if (state.view === "gallery") { bindGalleryDrag(); }
}

function errBanner() {
  return state.lastError
    ? '<div class="panel" style="margin-bottom:14px"><div class="pb" style="padding:12px 14px;color:#b91c1c">'
      + escapeHtml(state.lastError) + "</div></div>"
    : "";
}

function hdOverview() {
  var ov = state.overview || {};
  return '<h2>概览</h2><span class="sub">今天需要你处理的，都在这里</span>'
    + '<div class="spacer"></div>'
    + '<span class="hint">数据源：' + escapeHtml((ov.goods ? "已加载" : "加载中")) + "</span>";
}

function bodyOverview() {
  var ov = state.overview;
  if (!ov) { return errBanner() || '<div class="hint">加载中…</div>'; }
  var g = ov.goods || {};
  var ops = ov.recent_operations || [];
  var low = ov.low_stock || [];

  var html = '<div class="metrics">'
    + metric("在售商品", g.on_shelf || 0, "件", "共 " + (g.total || 0) + " 件 · " + (g.draft || 0) + " 草稿 · " + (g.off_shelf || 0) + " 下架")
    + metric("低库存", ov.low_stock_count || 0, "件", "低于预警阈值", "warn")
    + metric("下架 / 草稿", (g.off_shelf || 0) + (g.draft || 0), "件", "可重新上架")
    + metric("最近操作", ops.length, "条", "人工与 AI 都记", "ai")
    + "</div>";

  html += '<div class="cols2"><div class="panel"><div class="ph"><b>需要你处理的</b>'
    + '<span class="spacer"></span><span class="hint">' + low.length + " 项</span></div><div class=\"pb\">";
  if (!low.length) {
    html += '<div class="todo"><div class="ic b">好</div><div class="tx">暂无低库存商品<em>库存与价格都正常</em></div></div>';
  } else {
    low.slice(0, 6).forEach(function (item) {
      html += '<div class="todo" data-goods="' + item.id + '"><div class="ic w">库</div>'
        + '<div class="tx"><b>' + escapeHtml(item.title) + "</b> 库存仅剩 <b>" + item.stock + "</b><br />"
        + '<em>预警阈值 ' + item.stock_alert + " 件 · " + escapeHtml(item.goods_no) + "</em></div>"
        + '<span class="go">去补货</span></div>';
    });
  }
  html += "</div></div>";

  html += '<div class="panel"><div class="ph"><b>最近操作</b><span class="spacer"></span>'
    + '<button class="btn sm" data-act="goto-audit">查看全部</button></div><div class="pb" style="padding:6px 14px">';
  if (!ops.length) {
    html += '<div class="hint" style="padding:12px 0">暂无操作记录</div>';
  } else {
    ops.slice(0, 6).forEach(function (op) {
      html += '<div class="trace-item"><span class="d"></span><div>'
        + escapeHtml(op.actor === "ai" ? "AI " : op.actor === "system" ? "系统 " : "")
        + escapeHtml(op.summary) + "</div></div>";
    });
  }
  html += "</div></div></div>";
  html += healthPanel(ov.health_ranking || []);
  return html;
}

/** 商品健康度（商品 × 售后联动）。数据来自 `/overview` 的 `health_ranking`（后端纯聚合，无模型参与）。 */
function healthPanel(ranking) {
  var html = '<div class="panel" style="margin-top:14px">'
    + '<div class="ph"><b>商品健康度</b><span class="spacer"></span>'
    + '<span class="hint">按退货率 · 来自该商品的售后工单</span></div>'
    + '<div class="pb" style="padding:10px 14px">';
  if (!ranking.length) {
    html += '<div class="hint">还没有关联到商品的售后工单。'
      + "订单带上 <code>goods_no</code> 后即会出现在这里。</div>";
    return html + "</div></div>";
  }
  ranking.forEach(function (item) {
    var rate = item.refund_rate == null ? "—" : item.refund_rate + "%";
    var value = item.refund_rate || 0;
    var color = value >= 20 ? "#b91c1c" : value > 0 ? "#b45309" : "#15803d";
    html += '<div class="kv"><span>' + escapeHtml(item.title) + "</span>"
      + '<b style="color:' + color + '">' + rate + "</b></div>"
      + '<div class="bar"><i style="width:' + Math.min(100, value * 3) + "%;background:" + color + '"></i></div>'
      + '<div class="hint" style="margin:3px 0 9px">共 ' + item.total + " 单 · 赔付类 "
      + item.costly_count + " 单</div>";
  });
  return html + "</div></div>";
}

function metric(label, num, unit, delta, cls) {
  return '<div class="metric ' + (cls || "") + '"><div class="lb">' + escapeHtml(label) + "</div>"
    + '<div class="num">' + num + "<small>" + escapeHtml(unit) + "</small></div>"
    + '<div class="delta">' + escapeHtml(delta) + "</div></div>";
}

/** 陈列页的分类（从**已上架的这批商品**里取，去重保序）。
 *  为什么不写死类目表：卖家改类目后硬编码的胶囊会立刻变成错的分组。 */
function galleryCats() {
  var seen = {}, out = [];
  state.goods.forEach(function (g) {
    var cat = (g.category || "").trim();
    if (cat && !seen[cat]) { seen[cat] = true; out.push(cat); }
  });
  return out;
}

/** 把搜索词切成片段（连续中文 / 连续字母数字），去重保序。
 *  ⚠️ 与后端 `goods_repo.keyword_terms` **必须同口径** —— 否则会出现
 *  「接口能搜到、界面还是空」这种查不出来的怪现象。 */
function kwTerms(kw) {
  var hit = String(kw || "").toLowerCase().match(/[\u4e00-\u9fa5]+|[a-z0-9]+/g) || [];
  var out = [];
  for (var i = 0; i < hit.length; i++) {
    if (out.indexOf(hit[i]) < 0) { out.push(hit[i]); }
  }
  return out;
}

/** 片段**逐段 AND** 命中（标题 + 编号）。
 *  为什么不是整串 indexOf：「英伟达5090显卡」配不上「英伟达RTX5090显卡」，
 *  整串匹配会把"这个词没命中"表现成"店里没有"。 */
function kwHit(g, terms) {
  if (!terms.length) { return true; }
  var hay = (String(g.title || "") + " " + String(g.goods_no || "")).toLowerCase();
  for (var i = 0; i < terms.length; i++) {
    if (hay.indexOf(terms[i]) < 0) { return false; }
  }
  return true;
}

/** 当前陈列页可见的商品：分类 + 关键词（**都在本地过滤**，不重新请求）。
 *  本地过滤的好处：切分类不会闪一下、不会把已加载的列表打折成"搜索结果"。 */
function galleryVisible() {
  var cat = state._galleryCat || "";
  var terms = kwTerms(state._galleryQ);
  return state.goods.filter(function (g) {
    if (cat && (g.category || "").trim() !== cat) { return false; }
    return kwHit(g, terms);
  });
}

function hdGallery() {
  var cur = state._galleryCat || "";
  var pills = '<button class="pill' + (cur ? "" : " on") + '" data-act="gallery-cat" data-cat="">全部</button>'
    + galleryCats().map(function (cat) {
      return '<button class="pill' + (cat === cur ? " on" : "") + '" data-act="gallery-cat" data-cat="'
        + escapeHtml(cat) + '">' + escapeHtml(cat) + "</button>";
    }).join("");
  return '<div class="ghead">'
    + '<div class="grow">'
    + '<div class="gtext"><h2>商品陈列</h2>'
    // 副标题用「像买家一样看到你的橱窗」+ 真实在架数：数字来自当前列表长度，
    // 不是另查一个计数 —— 两处数字不一致时卖家只会更困惑
    + '<div class="gsub">像买家一样看到你的橱窗 · 已上架 <b>' + state.goods.length + "</b> 件</div></div>"
    + '<div class="spacer"></div>'
    + '<div class="gacts">'
    + '<input class="field gsearch" id="gallerySearch" placeholder="搜索商品…" value="'
    + escapeHtml(state._galleryQ || "") + '" />'
    + '<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;white-space:nowrap">'
    + '<input type="checkbox" id="arrangeToggle"' + (state.arrange ? " checked" : "") + " /> 编排模式</label>"
    + '<button class="btn primary" data-act="new-goods">＋ 新增商品</button>'
    + "</div></div>"
    + '<div class="pills">' + pills + "</div>"
    + "</div>";
}

function bodyGallery() {
  var html = errBanner();
  var list = galleryVisible();
  if (state.arrange) {
    html += '<div class="hint" style="margin-bottom:12px">编排模式：<b>拖动卡片</b>调整陈列顺序；置顶的商品固定在最前。</div>';
  }
  if (!state.goods.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center" class="hint">'
      + '<div class="hint">还没有已上架的商品。去「商品管理」新建并上架，或直接点右上角「＋ 新增商品」。</div></div></div>';
  }
  if (!list.length) {
    // 「一件都没有」和「被筛选掉了」是两种事实，不能说同一句话；筛选条件是几个就说几个
    var why = [];
    if (state._galleryCat) { why.push("「" + state._galleryCat + "」类"); }
    if (state._galleryQ) { why.push("「" + state._galleryQ + "」"); }
    return html + '<div class="panel"><div class="pb" style="padding:36px;text-align:center">'
      + '<div class="hint">' + escapeHtml(why.length ? why.join(" + ") + " 下没有匹配的商品。" : "没有匹配的商品。")
      + '<a data-act="gallery-cat" data-cat="" style="color:var(--brand);cursor:pointer">看全部</a></div></div></div>';
  }
  html += '<div class="grid" id="galleryGrid">';
  list.forEach(function (g, i) {
    var thumb = g.cover_url
      ? '<img src="' + escapeHtml(resolveUrl(g.cover_url)) + '" alt="" />'
      : '<span>暂无宣传图</span>';
    html += '<div class="gcard' + (state.sel && state.sel.id === g.id ? " on" : "") + '" data-goods="' + g.id + '" draggable="true">'
      + '<div class="thumb">' + thumb
      + (g.cover_url ? '<span class="corner">主图</span>' : "")
      + (g.pinned ? '<span class="pin">置顶</span>' : "")
      + '<span class="ordnum">' + (i + 1) + '</span><span class="handle">≡</span></div>'
      + '<div class="cbody"><div class="title">' + escapeHtml(g.title) + "</div>"
      + '<div class="desc">' + escapeHtml(g.description || "（暂无文案）") + "</div>"
      + '<div class="foot"><span class="price"><small>¥</small>' + money(g.price) + "</span>"
      + stockChip(g) + "</div></div></div>";
  });
  return html + "</div>";
}

function stockChip(g) {
  var stock = Number(g.stock || 0);
  var alert = Number(g.stock_alert || 0);
  // 断货（≤0）单独一档，且**不显示成「库存 0」** —— "0" 在灰色小字里太容易被扫过去，
  // 而断货意味着这件商品此刻根本卖不了。文案直说「断货」，样式做成实心红底（见 .stk.out）。
  if (stock <= 0) {
    return '<span class="stk out">断货</span>';
  }
  var low = alert > 0 && stock <= alert;
  return '<span class="stk' + (low ? " low" : "") + '">库存 ' + stock + "</span>";
}

/** 生效中的筛选**维度数**（价格上下限算一个维度，库存同理）—— 用于「筛选 ②」角标。 */
function activeFilterCount() {
  var f = state._filter || {};
  var hasPrice = (f.pmin !== "" && f.pmin !== undefined) || (f.pmax !== "" && f.pmax !== undefined);
  var hasStock = (f.smin !== "" && f.smin !== undefined) || (f.smax !== "" && f.smax !== undefined);
  var n = 0;
  if (f.cat) { n += 1; }
  if (hasPrice) { n += 1; }
  if (hasStock) { n += 1; }
  if (f.dmin || f.dmax) { n += 1; }
  return n;
}

function sortArrow(key) {
  var s = state._sort || {};
  if (s.key !== key) { return '<span class="arrow">↕</span>'; }
  return '<span class="arrow on">' + (s.dir === 1 ? "↑" : "↓") + "</span>";
}

/** 筛选输入 → 写进 state → 重渲染，并**还原焦点与光标**。
 *
 *  ⚠️ 重渲染会重建输入框，不还原焦点的话"输入一个字符就失焦"，筛选框根本没法连续输入。
 */
function applyFilterInput(id, value) {
  var map = {
    fltCat: "cat",
    fltPmin: "pmin", fltPmax: "pmax",
    fltSmin: "smin", fltSmax: "smax",
    fltDmin: "dmin", fltDmax: "dmax",
  };
  var key = map[id];
  if (!key) { return; }
  state._filter = state._filter || {};
  state._filter[key] = value;

  var el = document.activeElement;
  var focusId = el ? el.id : "";
  var pos = el && el.selectionStart !== undefined && el.selectionStart !== null ? el.selectionStart : null;

  // 筛选条件变了 → 结果集变了 → 回到顶部（原滚动位置对新结果没有意义）
  renderMid({ reset: true });

  if (focusId) {
    var again = $(focusId);
    if (again) {
      try { again.focus(); } catch (e) { /* 忽略 */ }
      if (pos !== null) {
        try { again.setSelectionRange(pos, pos); } catch (e) { /* 数字框不支持，忽略 */ }
      }
    }
  }
}

/** 陈列页搜索：写 state → 重绘 → 还原焦点与光标。
 *  为什么不用后端 `q=` 参数：搜索与分类是同一种"这一屏看哪些"的筛选，
 *  两者都走本地过滤才能叠加（后端 q 会把分类胶囊的候选一起过滤掉，越筛越少）。
 *
 *  ⚠️ 只在**值真的变了**时才重绘：中文输入法上屏后浏览器会再补一个 input，
 *  不判等就会白重绘一次（200 张卡整体替换，手感会顿）。 */
function applyGallerySearch(value) {
  var next = value || "";
  if ((state._galleryQ || "") === next) { return; }
  state._galleryQ = next;
  var el = document.activeElement;
  var pos = el && el.selectionStart !== undefined && el.selectionStart !== null ? el.selectionStart : null;
  renderMid({ reset: true });
  var again = $("gallerySearch");
  if (!again) { return; }
  try { again.focus(); } catch (e) { /* 忽略 */ }
  if (pos !== null) {
    try { again.setSelectionRange(pos, pos); } catch (e) { /* 忽略 */ }
  }
}

/** 商品管理页搜索：与陈列页同一套（**本地过滤**，不请求后端）。
 *
 *  为什么必须改成本地过滤：原来走 `/goods?q=` 且把结果写回 `state.goods`，
 *  而 `state.goods` 是三个视图共用的同一份 —— 搜一次就会把陈列页、左栏角标一起换成
 *  "搜索结果"，同时重绘出**空输入框**，正在打的字被吞掉（现象就是"吞字直接搜"）。 */
function applyAdminSearch(value) {
  var next = String(value || "");
  if ((state._adminQ || "") === next) { return; }
  state._adminQ = next;
  var el = document.activeElement;
  var pos = el && el.selectionStart !== undefined && el.selectionStart !== null ? el.selectionStart : null;
  renderMid({ reset: true });
  var again = $("adminSearch");
  if (!again) { return; }
  try { again.focus(); } catch (e) { /* 忽略 */ }
  if (pos !== null) {
    try { again.setSelectionRange(pos, pos); } catch (e) { /* 忽略 */ }
  }
}

/** 商品管理搜索的防抖：只在**非组字**状态下才真正过滤。
 *  防抖窗口内切走了视图就只记值不重绘 —— 否则会在别的页面上无故跳回顶部。 */
function scheduleAdminSearch(value) {
  clearTimeout(state._searchTm);
  state._searchTm = setTimeout(function () {
    if (state.view !== "admin") { state._adminQ = String(value || ""); return; }
    applyAdminSearch(value);
  }, 140);
}

/** 输入框的"该做什么"统一在这里分发（input 与 compositionend 共用一套判断），
 *  避免两条路径各写一份、日后改一处漏一处。 */
function applySearchLike(el) {
  if (!el || !el.id) { return; }
  var id = el.id;
  if (id === "gallerySearch") { applyGallerySearch(el.value); return; }
  if (id === "adminSearch") { scheduleAdminSearch(el.value); return; }
  if (id.indexOf("flt") === 0) { applyFilterInput(id, el.value); }
}

function hdAdmin() {
  var fc = activeFilterCount();
  return '<h2>商品管理</h2><span class="sub">全部商品 · 与「商品陈列」同一份数据</span><div class="spacer"></div>'
    + '<select class="field" id="adminStatus" style="width:118px">'
    + ["", "on_shelf", "off_shelf", "draft"].map(function (s) {
      return '<option value="' + s + '"' + (state._status === s ? " selected" : "") + ">"
        + (s === "" ? "全部状态" : s === "on_shelf" ? "已上架" : s === "off_shelf" ? "已下架" : "草稿") + "</option>";
    }).join("")
    + "</select>"
    + '<input class="field" id="adminSearch" placeholder="搜索…" style="width:150px" value="'
    + escapeHtml(state._adminQ || "") + '" />'
    // 筛选默认**折叠**：只占一行。生效时按钮上标条件数 —— 避免"筛了东西忘了"看着残缺数据做决策。
    + '<button class="btn' + (state._filterOpen ? " open" : "") + '" data-act="toggle-filter">筛选'
    + (fc ? '<span class="cnt">' + fc + "</span>" : "") + '<span class="caret">▾</span></button>'
    + '<button class="btn primary" data-act="new-goods">＋ 新增商品</button>';
}

/** 是否低库存。与后端**同口径**：
 *  · **断货（stock ≤ 0）一律算** —— 不论有没有设预警阈值（断货是绝对异常）
 *  · 设了阈值（>0）且低于它，也算
 */
function isLowStock(g) {
  var alert = Number(g.stock_alert || 0);
  var stock = Number(g.stock || 0);
  return stock <= 0 || (alert > 0 && stock <= alert);
}

/** 商品管理的子功能栏（全部 / 低库存 / 回收站）。 */
function adminTabs(tab, lowCount) {
  return '<div class="tabs">'
    + '<button class="tab' + (tab === "all" ? " on" : "") + '" data-act="admin-tab" data-tab="all">'
    + '全部商品<span class="cnt">' + state.goods.length + "</span></button>"
    + '<button class="tab' + (tab === "low" ? " on" : "") + '" data-act="admin-tab" data-tab="low">'
    + '低库存<span class="cnt warn">' + lowCount + "</span></button>"
    + '<button class="tab' + (tab === "trash" ? " on" : "") + '" data-act="admin-tab" data-tab="trash">'
    + '回收站<span class="cnt">' + (state.trashCount || 0) + "</span></button>"
    + "</div>";
}

/** 回收站视图：删除的商品在这里，可恢复或彻底删除。 */
function bodyTrash(lowCount) {
  var items = state.trash || [];
  var days = state.trashDays || 7;
  var html = errBanner() + adminTabs("trash", lowCount)
    + '<div class="hint" style="margin-bottom:10px">回收站保留 ' + days
    + " 天，到期自动彻底删除（不可恢复）。图片也一并保留着。</div>";

  if (!items.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center">'
      + '<div class="hint">回收站是空的。删除商品后会先进这里，' + days + " 天内都能恢复。</div></div></div>";
  }

  html += '<table class="tbl"><thead><tr>'
    + '<th>商品</th><th style="width:100px">价格</th>'
    + '<th style="width:120px">剩余可恢复</th><th style="width:168px">操作</th></tr></thead><tbody>';
  items.forEach(function (g) {
    var left = g.days_left;
    html += "<tr>"
      + '<td><div class="cell-good"><div class="gmeta"><div class="t">' + escapeHtml(g.title) + "</div>"
      + '<div class="s">' + escapeHtml(g.goods_no) + "</div></div></div></td>"
      + '<td class="price" style="font-size:13px">¥' + money(g.price) + "</td>"
      + "<td>" + (left === null || left === undefined
        ? "—" : '<span class="low">' + left + " 天</span>") + "</td>"
      + '<td><div class="ops">'
      + '<button class="btn sm" data-act="restore-goods" data-id="' + g.id + '">恢复</button>'
      + '<button class="btn sm danger" data-act="purge-goods" data-id="' + g.id + '">彻底删除</button>'
      + "</div></td></tr>";
  });
  return html + "</tbody></table>";
}

async function loadTrash() {
  var r = await api("/api/seller/goods/trash");
  state.trash = r.ok ? (r.data.items || []) : [];
  state.trashCount = state.trash.length;
}

async function restoreGoods(goodsId) {
  var r = await api("/api/seller/goods/" + goodsId + "/restore", { method: "POST" });
  if (!r.ok) { toast(r.error, true); return; }
  toast("已恢复（原状态一并回来）");
  state._adminTab = "all";
  await refreshAll();
  render();
}

function purgeGoods(goodsId) {
  var item = (state.trash || []).filter(function (g) { return g.id === goodsId; })[0];
  askConfirm({
    title: "彻底删除？",
    text: "此操作不可恢复：商品及其图片会被永久删除。",
    box: item ? "商品：<b>" + escapeHtml(item.title) + "</b><br />资产：¥" + money(item.price) : "",
    ico: "!",
  }, async function () {
    closeConfirm();
    var r = await api("/api/seller/goods/" + goodsId + "/purge?confirm=true", { method: "DELETE" });
    if (!r.ok) { toast(r.error, true); return; }
    toast("已彻底删除");
    await refreshAll();
    render();
  });
}

function bodyAdmin() {
  var tab = state._adminTab || "all";
  var lowCount = state.goods.filter(isLowStock).length;
  if (tab === "trash") { return bodyTrash(lowCount); }
  var f = state._filter || {};

  var adminTerms = kwTerms(state._adminQ);
  var rows = state.goods.filter(function (g) {
    if (state._status && g.status !== state._status) { return false; }
    if (tab === "low" && !isLowStock(g)) { return false; }
    // 关键词与陈列页同口径（标题 + 编号；分段 AND），且**只过滤不改数据**
    if (!kwHit(g, adminTerms)) { return false; }
    if (f.cat && (g.category || "") !== f.cat) { return false; }
    var price = Number(g.price || 0);
    var stock = Number(g.stock || 0);
    if (f.pmin !== "" && f.pmin !== undefined && price < Number(f.pmin)) { return false; }
    if (f.pmax !== "" && f.pmax !== undefined && price > Number(f.pmax)) { return false; }
    if (f.smin !== "" && f.smin !== undefined && stock < Number(f.smin)) { return false; }
    if (f.smax !== "" && f.smax !== undefined && stock > Number(f.smax)) { return false; }
    if (f.dmin || f.dmax) {
      var created = String(g.created_at || "").slice(0, 10);   // 只比日期部分
      if (f.dmin && created && created < f.dmin) { return false; }
      if (f.dmax && created && created > f.dmax) { return false; }
    }
    return true;
  });

  // 排序（filter 已返回新数组，可安全就地排）
  var sort = state._sort || {};
  if (sort.key) {
    rows.sort(function (a, b) {
      return (Number(a[sort.key] || 0) - Number(b[sort.key] || 0)) * sort.dir;
    });
  }

  var checked = Object.keys(state.checked).filter(function (k) { return state.checked[k]; });

  var html = errBanner();
  // 子功能栏：低库存 / 回收站都是「商品管理」的视图，**不是左栏的并列项**
  html += adminTabs(tab, lowCount);

  // ---- 筛选面板（默认折叠，只占一行）----
  var cats = [];
  state.goods.forEach(function (g) {
    if (g.category && cats.indexOf(g.category) < 0) { cats.push(g.category); }
  });

  function fieldVal(v) { return v === undefined || v === null ? "" : String(v); }

  html += '<div class="filters' + (state._filterOpen ? " open" : "") + '">'
    + '<div class="frow">'
    + "<label>类目</label>"
    + '<select class="field" id="fltCat" style="width:140px">'
    + '<option value="">全部类目</option>'
    + cats.map(function (c) {
        return "<option" + (f.cat === c ? " selected" : "") + ">" + escapeHtml(c) + "</option>";
      }).join("")
    + "</select>"
    + "<label>价格</label>"
    + '<input class="field" id="fltPmin" placeholder="最低" style="width:76px" value="' + escapeHtml(fieldVal(f.pmin)) + '" />'
    + '<span class="dash">—</span>'
    + '<input class="field" id="fltPmax" placeholder="最高" style="width:76px" value="' + escapeHtml(fieldVal(f.pmax)) + '" />'
    + "<label>库存</label>"
    + '<input class="field" id="fltSmin" placeholder="最低" style="width:76px" value="' + escapeHtml(fieldVal(f.smin)) + '" />'
    + '<span class="dash">—</span>'
    + '<input class="field" id="fltSmax" placeholder="最高" style="width:76px" value="' + escapeHtml(fieldVal(f.smax)) + '" />'
    + '<span class="spacer"></span>'
    + '<button class="btn sm" data-act="reset-filter">重置</button>'
    + "</div>"
    // 创建时间：补上之前"确认字段后再做"的那一项
    + '<div class="frow" style="margin-top:9px">'
    + "<label>创建时间</label>"
    + '<input class="field" id="fltDmin" type="date" style="width:132px" value="' + escapeHtml(fieldVal(f.dmin)) + '" />'
    + '<span class="dash">—</span>'
    + '<input class="field" id="fltDmax" type="date" style="width:132px" value="' + escapeHtml(fieldVal(f.dmax)) + '" />'
    + "</div></div>";

  // ---- 已选条件 chip（生效的条件必须看得见、能单个去掉）----
  var chips = [];
  if (f.cat) { chips.push(["类目", f.cat, "cat"]); }
  if ((f.pmin !== "" && f.pmin !== undefined) || (f.pmax !== "" && f.pmax !== undefined)) {
    chips.push(["价格", "¥" + (f.pmin || 0) + " — ¥" + (f.pmax === undefined || f.pmax === "" ? "不限" : f.pmax), "price"]);
  }
  if ((f.smin !== "" && f.smin !== undefined) || (f.smax !== "" && f.smax !== undefined)) {
    chips.push(["库存", (f.smin || 0) + " — " + (f.smax === undefined || f.smax === "" ? "不限" : f.smax), "stock"]);
  }
  if (f.dmin || f.dmax) {
    chips.push(["创建", (f.dmin || "最早") + " — " + (f.dmax || "至今"), "date"]);
  }
  if (chips.length) {
    html += '<div class="chips">' + chips.map(function (c) {
      return '<span class="chip"><b>' + c[0] + "</b>：" + escapeHtml(String(c[1]))
        + '<span class="x" data-act="clear-filter" data-key="' + c[2] + '">×</span></span>';
    }).join("")
      + '<a class="chipx" data-act="reset-filter">清空全部</a></div>';
  }

  // 批量栏**常驻占位**：未选时置灰 + 按钮 disabled（CSS 见 .bulkbar 注释 ——
  //  显示/隐藏切换会改变滚动内容上方的高度，勾选一行列表就"跳一格"）
  var dis = checked.length ? "" : " disabled";
  html += '<div class="bulkbar' + (checked.length ? " on" : "") + '" id="bulkbar">'
    + "已选 <b>" + checked.length + "</b> 项"
    + '<button class="btn sm" data-act="bulk" data-mode="on_shelf"' + dis + ">批量上架</button>"
    + '<button class="btn sm" data-act="bulk" data-mode="off_shelf"' + dis + ">批量下架</button>"
    + '<button class="btn sm" data-act="bulk-price"' + dis + ">批量改价</button>"
    + '<button class="btn sm danger" data-act="bulk" data-mode="delete"' + dis + ">批量删除</button>"
    + '<span class="hint" style="margin-left:auto">批量删除需二次确认</span></div>';

  if (!rows.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center">'
      + '<div class="hint">' + (chips.length ? "没有符合筛选条件的商品" : "没有符合条件的商品") + "</div></div></div>";
  }

  // ---- 表格：编号/宣传图/标题/类目 **合并成「商品」一列**（8 列 → 7 列）----
  html += '<table class="tbl"><thead><tr>'
    + '<th style="width:32px"><input type="checkbox" id="ckAll" /></th>'
    + "<th>商品</th>"
    + '<th style="width:96px">类目</th>'
    + '<th class="sortable" data-act="sort" data-key="price" style="width:92px">价格' + sortArrow("price") + "</th>"
    + '<th class="sortable" data-act="sort" data-key="stock" style="width:100px">库存' + sortArrow("stock") + "</th>"
    + '<th style="width:86px">状态</th>'
    + '<th style="width:168px">操作</th></tr></thead><tbody>';

  rows.forEach(function (g) {
    var thumb = g.cover_url
      ? '<img class="mini" src="' + escapeHtml(resolveUrl(g.cover_url)) + '" alt="" />'
      : '<span class="mini"></span>';
    var low = g.stock_alert > 0 && g.stock <= g.stock_alert;
    html += '<tr data-goods="' + g.id + '"' + (state.sel && state.sel.id === g.id ? ' class="on"' : "") + ">"
      + '<td><input type="checkbox" class="ckRow" data-id="' + g.id + '"'
      + (state.checked[g.id] ? " checked" : "") + " /></td>"
      + '<td><div class="cell-good">' + thumb
      + '<div class="gmeta"><div class="t" title="' + escapeHtml(g.title) + '">' + escapeHtml(g.title) + "</div>"
      + '<div class="s">' + escapeHtml(g.goods_no)
      + (g.category ? " · " + escapeHtml(g.category) : "") + "</div></div></div></td>"
      + "<td>" + escapeHtml(g.category || "—") + "</td>"
      + '<td class="price" style="font-size:13px">¥' + money(g.price) + "</td>"
      + "<td>" + (low ? '<span class="stk low">' + g.stock + " 低</span>" : g.stock) + "</td>"
      + "<td>" + statusBadge(g.status) + "</td>"
      + '<td><div class="ops">'
      + (g.status === "on_shelf"
        ? '<button class="btn sm" data-act="status" data-status="off_shelf">下架</button>'
        : '<button class="btn sm" data-act="status" data-status="on_shelf">上架</button>')
      + '<button class="btn sm danger" data-act="del">删除</button>'
      + "</div></td></tr>";
  });
  return html + "</tbody></table>";
}

function statusBadge(status) {
  var label = status === "on_shelf" ? "已上架" : status === "off_shelf" ? "已下架" : "草稿";
  return '<span class="badge ' + status + '">' + label + "</span>";
}

function hdAudit() {
  return "<h2>操作日志</h2><span class=\"sub\">谁在什么时候改了什么 · 人工与 AI 都记</span>"
    + '<div class="spacer"></div>'
    + '<select class="field" id="auditActor" style="width:118px">'
    + '<option value="">全部来源</option><option value="human">仅人工</option><option value="ai">仅 AI</option></select>';
}

function bodyAudit() {
  var html = errBanner();
  if (!state.logs.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center">'
      + '<div class="hint">暂无操作记录</div></div></div>';
  }
  html += '<div class="hint" style="margin-bottom:12px">可撤回的动作会给出「撤销」入口；撤销本身也会记入日志。</div>'
    + '<table class="tbl"><thead><tr><th style="width:140px">时间</th><th style="width:78px">来源</th>'
    + "<th>动作</th><th>对象</th><th>变更</th><th style=\"width:88px\">操作</th></tr></thead><tbody>";
  state.logs.forEach(function (log) {
    html += '<tr data-log="' + log.id + '"' + (state.log && state.log.id === log.id ? ' class="on"' : "") + ">"
      + "<td>" + escapeHtml(shortTime(log.created_at)) + "</td>"
      + "<td>" + actorChip(log.actor) + "</td>"
      + "<td>" + escapeHtml(actionLabel(log.action)) + "</td>"
      + "<td>" + escapeHtml(log.target_id || "—") + "</td>"
      + "<td>" + escapeHtml(log.summary || "") + "</td>"
      + "<td>" + (log.undoable && !log.undone_at
        ? '<button class="btn sm" data-act="undo" data-log="' + log.id + '">撤销</button>'
        : '<span class="hint">' + (log.undone_at ? "已撤销" : "—") + "</span>") + "</td></tr>";
  });
  return html + "</tbody></table>";
}

function actorChip(actor) {
  var map = { human: ["human", "卖家"], ai: ["ai", "AI"], system: ["system", "系统"] };
  var m = map[actor] || ["system", actor || "?"];
  return '<span class="who-chip ' + m[0] + '">' + m[1] + "</span>";
}

function actionLabel(action) {
  var map = {
    create_goods: "新建商品", update_goods: "修改商品", update_price: "修改价格",
    set_status: "上下架", set_pinned: "置顶", delete_goods: "删除商品",
    upload_media: "上传图片", delete_media: "删除图片",
    bulk_on_shelf: "批量上架", bulk_off_shelf: "批量下架", bulk_delete: "批量删除",
    bulk_price_delta: "批量调价", reorder: "陈列编排", undo: "撤销",
    ai_generate_copy: "AI 生成文案", ai_suggest_price: "AI 定价建议"
  };
  return map[action] || action;
}

function shortTime(value) {
  if (!value) { return "—"; }
  var s = String(value).replace("T", " ");
  return s.length > 16 ? s.slice(5, 16) : s;
}

function hdAftersale() {
  return "<h2>售后</h2><span class=\"sub\">实时工单台 · 收件箱 / 工单列表 / 处理详情</span>";
}

function bodyAftersale() {
  // v 参数随 FRONTEND_VERSION 走：内嵌页（dist/console.html 及其引用的 css/js）改版后，
  // URL 变化可强制浏览器弃用旧缓存 —— 否则会出现"改了样式页面不变"的缓存坑。
  // 售后指挥台已迁移到 Vue 3 + Vite，构建产物位于 frontend/dist/（后端经 /ui/dist/ 托管）。
  return '<div style="height:100%">'
    + '<iframe class="embed-frame" src="dist/console.html?embed=1&v=' + FRONTEND_VERSION + '" title="售后指挥台"></iframe></div>';
}

/* ---------- 渲染：右栏（上下文面板） ---------- */

function renderRight() {
  var pane = $("right");
  if (state.view === "orders" || state.view === "service") {
    renderCommerceRight(pane, state.view);
    return;
  }
  var isLog = state.view === "audit";
  var sel = isLog ? state.log : state.sel;
  var noPaneView = state.view === "overview" || state.view === "aftersale";

  // ① 概览 / 售后本来就没有右栏；
  // ② **没选中任何东西时不占位** —— 空面板除了"未选择"什么也不提供，却白吃 380px 宽度；
  // ③ 卖家手动收起了。
  if (noPaneView || !sel || !state.rightOpen) {
    pane.style.display = "none";
    // 只有"收起了 + 确实有内容"时才给恢复入口，否则点了也没东西可看
    updateRightToggle(!state.rightOpen && !!sel && !noPaneView);
    return;
  }
  pane.style.display = "flex";
  updateRightToggle(false);

  var isLog = state.view === "audit";
  var sel = isLog ? state.log : state.sel;
  pane.setAttribute("data-mode", isLog ? "log" : state.view);

  var title = "未选择";
  if (isLog && sel) { title = "变更详情"; }
  else if (!isLog && sel) { title = state.view === "gallery" ? "商品详情 · 陈列预览" : "商品详情 / 编辑"; }

  var tips = {
    gallery: "从左侧选一件商品，查看它在陈列里的样子",
    admin: "从左侧选一件商品进行编辑",
    audit: "从左侧选一条日志，查看这次改了什么",
    // 知识库没有"选中对象"，右栏不参与 —— 这里说清楚，避免看起来像加载失败
    knowledge: "知识库不需要选中商品：拖不到右栏来，直接在中间上传与检索"
  };

  var body = "";
  if (!sel) {
    body = '<div class="rpanel on"><div class="empty-r"><div class="ico">◇</div>'
      + '<div class="tx">' + escapeHtml(tips[state.view] || "") + "</div></div></div>";
  } else if (isLog) {
    body = rpLog(sel);
  } else {
    body = rpGoods(sel);
  }

  var foot = "";
  if (sel && isLog) {
    foot = '<button class="btn" id="btnUndo" ' + (sel.undoable && !sel.undone_at ? "" : "disabled")
      + ">撤销这次改动</button>";
  } else if (sel) {
    foot = '<button class="btn primary" id="btnSave">保存</button>'
      + '<button class="btn" id="btnReload">重置</button>'
      + '<div style="margin-left:auto">'
      + '<button class="btn danger admin-only" id="btnDel">删除商品</button></div>';
  }

  pane.innerHTML = '<div class="hd"><h3>' + escapeHtml(title) + "</h3>"
    + (!isLog && sel ? statusBadge(sel.status) : "")
    // 「收起」只在**真有内容**时出现 —— 空态下没有可收的东西，摆在那里只会让人点了疑惑
    + (sel ? '<button class="btn sm" data-act="collapse-right">收起</button>' : "")
    + "</div>"
    + '<div class="body">' + body + "</div>"
    + (foot ? '<div class="foot">' + foot + "</div>" : "");
  bindRight();
}

/** 右栏展开状态持久化：刷新后保持你上次的选择（收起过就一直收起）。 */
function persistRight() {
  try {
    localStorage.setItem("seller.rightOpen", state.rightOpen ? "1" : "0");
  } catch (e) { /* 隐私模式 / 禁用存储时忽略，不影响功能 */ }
}

/** 顶栏「显示详情」按钮的显隐：右栏被收起时才需要它，否则无从恢复。 */
function updateRightToggle(show) {
  var btn = $("btnShowRight");
  if (btn) { btn.hidden = !show; }
}

function rpGoods(g) {
  var media = g.media || [];
  var html = "";

  html += '<div class="preview-box gallery-only"><div class="pv-lb">陈列视角 · 买家看到的样子</div>'
    + '<div class="pv-card"><div class="pv-thumb">'
    + (g.cover_url ? '<img src="' + escapeHtml(resolveUrl(g.cover_url)) + '" alt="" />' : "图")
    + '</div><div style="min-width:0;flex:1"><div class="pv-title">' + escapeHtml(g.title) + "</div>"
    + '<div class="pv-price">¥' + money(g.price) + "</div></div></div></div>";

  html += '<div class="f"><label>宣传图（首张为主图）</label>'
    + '<div class="up" id="uploadBox">点击上传图片，或把图片拖到这里<br />'
    + '<span style="font-size:11.5px">JPG / PNG / WebP，单张 ≤ 5MB</span>'
    + '<input type="file" id="fileInput" accept="image/jpeg,image/png,image/webp" multiple hidden /></div>'
    + '<div class="row" style="margin-top:7px">'
    + '<button class="ai-btn" data-act="ai-cover"' + (state._aiBusy ? " disabled" : "")
    + ">AI 生成宣传图</button>"
    + '<span class="hint">按商品信息自动出图（约 30—60 秒 · 1 张图计费）</span></div>';
  if (media.length) {
    html += '<div class="mlis">' + media.map(function (m, i) {
      return '<div class="mli">'
        + '<img src="' + escapeHtml(resolveUrl(m.url)) + '" alt="" />'
        + (i === 0 ? '<span class="main">主图</span>' : "")
        + '<span class="x" data-act="del-media" data-media="' + m.id + '" title="删除这张">×</span></div>';
    }).join("") + "</div>";
  }
  html += "</div>";

  html += '<div class="f"><label>商品编号</label><div class="hint">' + escapeHtml(g.goods_no) + "</div></div>";
  html += '<div class="f"><label>商品标题</label>'
    + '<input class="field" id="fTitle" value="' + escapeHtml(g.title) + '" /></div>';
  html += '<div class="f"><label>商品文案</label>'
    + '<textarea class="field" id="fDesc" style="height:76px;resize:vertical">'
    + escapeHtml(g.description || "") + "</textarea>"
    + '<div class="row" style="margin-top:7px">'
    + '<button class="ai-btn" data-act="ai-copy"' + (state._aiBusy ? " disabled" : "")
    + ">AI 生成文案</button>"
    + '<select class="field" id="copyStyle" style="max-width:104px"'
    + ' title="文案语气：会影响生成结果的口吻和用词">'
    + COPY_STYLES.map(function (s) {
        // 用 state 回填 selected：右栏重绘后不能跳回第一项（否则卖家选的语气会被悄悄改掉）
        return '<option value="' + s + '"'
          + (state._aiStyle === s ? " selected" : "") + ">" + s + "</option>";
      }).join("")
    + "</select>"
    + "</div>" + aiCopyCard() + "</div>";

  html += '<div class="f"><label>价格（元）</label>'
    + '<div class="row"><input class="field" id="fPrice" type="number" step="0.01" min="0" value="'
    + money(g.price) + '" />'
    + '<button class="ai-btn" data-act="ai-price"' + (state._aiBusy ? " disabled" : "")
    + ">AI 定价</button></div>"
    + aiPriceCard() + "</div>";

  html += '<div class="f admin-only"><label>成本与库存</label><div class="row">'
    + '<span class="hint">成本</span><input class="field" id="fCost" type="number" step="0.01" min="0" value="' + money(g.cost_price) + '" />'
    + '<span class="hint">库存</span><input class="field" id="fStock" type="number" min="0" value="' + g.stock + '" />'
    + '<span class="hint">预警</span><input class="field" id="fAlert" type="number" min="0" value="' + g.stock_alert + '" />'
    + "</div>"
    + '<div class="hint" style="margin-top:6px">毛利 ¥' + money((Number(g.price) || 0) - (Number(g.cost_price) || 0))
    + (g.stock_alert > 0 && g.stock <= g.stock_alert
      ? ' · <span style="color:#b91c1c">库存 ' + g.stock + " 低于阈值 " + g.stock_alert + "，已预警</span>" : "")
    + "</div></div>";

  html += '<div class="f admin-only"><label>类目 / 状态</label><div class="row">'
    + '<input class="field" id="fCategory" value="' + escapeHtml(g.category || "") + '" />'
    + '<select class="field" id="fStatus" style="max-width:130px">'
    + ["draft", "on_shelf", "off_shelf"].map(function (s) {
      var label = s === "on_shelf" ? "已上架" : s === "off_shelf" ? "已下架" : "草稿";
      return '<option value="' + s + '"' + (g.status === s ? " selected" : "") + ">" + label + "</option>";
    }).join("") + "</select></div></div>";

  html += '<div class="f gallery-only"><label>陈列设置</label><div class="row" style="gap:12px">'
    + '<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">'
    + '<input type="checkbox" id="fPinned"' + (g.pinned ? " checked" : "") + " /> 置顶展示</label>"
    + '<span class="hint">当前排序 ' + (g.sort_no || 0) + " · 可用「编排模式」拖动调整</span></div></div>";

  html += '<div class="split"></div>';
  html += '<div class="minicard" style="margin-bottom:12px">'
    + '<div class="mc-hd">该商品的售后情况<span class="hint" style="margin-left:auto">商品 × 售后</span></div>'
    + '<div id="goodsAftersale" class="hint">加载中…</div></div>';
  html += '<div class="minicard"><div class="mc-hd">这件商品的操作记录</div><div id="goodsLogs" class="hint">加载中…</div></div>';
  return html;
}

/* ---------- AI 生成结果卡（阶段三） ----------
   生成只是**候选**：点「采纳」只把内容填进表单，仍需卖家点「保存」才写库。
   这样"AI 出主意、人拍板"在交互上也是成立的，不会出现"按一下 AI 就把数据改了"。
   ------------------------------------------------------------ */

function aiCopyCard() {
  var d = state._aiCopy;
  if (!d) { return ""; }
  return '<div class="ai-out"><div class="t">AI 生成结果（' + escapeHtml(d.style || "") + '）</div>'
    + "<div>" + escapeHtml(d.copy || "") + "</div>"
    + '<div class="meta">模型：' + escapeHtml(d.model || "—") + "</div>"
    + '<div class="acts">'
    + '<button class="btn sm primary" data-act="copy-apply">采纳</button>'
    + '<button class="btn sm" data-act="ai-copy">换一版</button>'
    + '<button class="btn sm" data-act="copy-drop">放弃</button>'
    + "</div></div>";
}

function aiPriceCard() {
  var d = state._aiPrice;
  if (!d) { return ""; }
  var src = d.source === "rule_fallback" ? "规则估算（模型未参与）" : "模型建议";
  var meta = "成本 ¥" + money(d.cost_price)
    + (d.margin_percent != null ? " · 毛利 " + d.margin_percent + "%" : "")
    + (d.peer_count
        ? " · 同类目均价 ¥" + money(d.peer_avg) + "（" + d.peer_count + " 件）"
        : " · 无同类目参考");
  return '<div class="ai-out"><div class="t">定价建议 ¥' + money(d.suggested)
    + "（区间 ¥" + money(d.min) + "—¥" + money(d.max) + "）</div>"
    + "<div>" + escapeHtml(d.reason || "") + "</div>"
    + '<div class="meta">' + escapeHtml(meta) + " · 来源：" + escapeHtml(src) + "</div>"
    + '<div class="acts">'
    + '<button class="btn sm primary" data-act="price-apply">采纳 ¥' + money(d.suggested) + "</button>"
    + '<button class="btn sm" data-act="price-drop">保持原价</button>'
    + "</div></div>";
}

async function aiCopy() {
  if (!state.sel || state._aiBusy) { return; }
  // ⚠️ 取值必须在 `renderRight()` **之前**。
  // 老实现是先重绘再读 `$("copyStyle")`：`<select>` 已被重建回第一项，
  // 于是永远读到「口语」—— 卖家选了「专业」，界面立刻跳回口语，生成出来也是口语。
  var styleEl = $("copyStyle");
  var style = (styleEl && styleEl.value) || state._aiStyle || "口语";
  state._aiStyle = style;              // 记住这次的选择，重绘后据此回填
  state._aiBusy = true;
  toast("正在生成文案（" + style + "）…");
  renderRight();
  var r = await api("/api/seller/goods/" + state.sel.id + "/ai/copy", {
    method: "POST", body: { style: style }
  });
  state._aiBusy = false;
  if (!r.ok) { toast(r.error, true); renderRight(); return; }
  state._aiCopy = r.data;
  renderRight();
}

/** 落库一个字段补丁。
 *
 *  成功则**回读服务端数据**，保证界面与库一致；失败则把表单恢复成服务端的真实值。
 *  这样就不存在"表单显示 A、库里是 B"的中间态。
 */
async function patchGoods(fields, okMsg) {
  var r = await api("/api/seller/goods/" + state.sel.id, { method: "PATCH", body: fields });
  if (!r.ok) {
    toast(r.error, true);
    renderRight();
    return false;
  }
  toast(okMsg || "已保存");
  await refreshAll();
  await selectGoods(state.sel.id);
  return true;
}

/** 采纳 AI 文案：**立即写入商品**。
 *
 *  ⚠️ 这里曾有一个真 bug（用户实测发现）：老实现只把文本填进 textarea，然后调 `renderRight()` ——
 *  而重渲染会用**服务端的旧文案**把 textarea 覆盖掉，于是"采纳"看起来毫无效果、也从未落库。
 *  现在改为**直接 PATCH 落库**，界面回读服务端，不再留中间态。
 */
async function applyCopy() {
  if (!state.sel || !state._aiCopy) { return; }
  var text = String(state._aiCopy.copy || "").trim();
  if (!text) { toast("生成结果为空，未采纳", true); return; }
  var el = $("fDesc");
  if (el) { el.value = text; }          // 先让卖家在表单里看到效果
  state._aiCopy = null;
  await patchGoods({ description: text }, "已采纳并保存文案");
}

async function aiPrice() {
  if (!state.sel || state._aiBusy) { return; }
  state._aiBusy = true;
  toast("正在计算定价建议…");
  renderRight();
  var r = await api("/api/seller/goods/" + state.sel.id + "/ai/price", { method: "POST" });
  state._aiBusy = false;
  if (!r.ok) { toast(r.error, true); renderRight(); return; }
  state._aiPrice = r.data;
  renderRight();
}

/** 采纳 AI 建议价：**立即写入商品**（与 `applyCopy` 同理，修掉"填了表单没落库"的问题）。 */
async function applyPrice() {
  if (!state.sel || !state._aiPrice) { return; }
  var value = Number(state._aiPrice.suggested);
  if (!isFinite(value) || value <= 0) { toast("建议价非法，未采纳", true); return; }
  var el = $("fPrice");
  if (el) { el.value = money(value); }
  state._aiPrice = null;
  await patchGoods({ price: value }, "已采纳并保存价格 ¥" + money(value));
}

/** AI 生成宣传图。
 *
 *  ⚠️ 与文案/定价不同：这一步**没有"采纳"环节** —— 图生成后直接进商品图片列表。
 *  因为它是**新增**而不是覆盖，不满意删掉那张图即可（已有的删除按钮），
 *  再叠一层"确认"只会让人多点一次。
 */
async function aiCover() {
  if (!state.sel || state._aiBusy) { return; }
  state._aiBusy = true;
  toast("正在生成宣传图，约 30—60 秒，请稍候…");
  renderRight();
  var r = await api("/api/seller/goods/" + state.sel.id + "/ai/cover", {
    method: "POST", body: { hint: "" }
  });
  state._aiBusy = false;
  if (!r.ok) { toast(r.error, true); renderRight(); return; }
  toast("宣传图已生成并加入商品");
  await refreshAll();
  await selectGoods(state.sel.id);
}

function rpLog(log) {
  var before = log.before_json, after = log.after_json;
  var rows = "";
  if (before && after) {
    Object.keys(after).forEach(function (k) {
      rows += '<div class="drow"><span class="k">' + escapeHtml(fieldLabel(k)) + "</span>"
        + '<span class="old">' + escapeHtml(fmt(before[k])) + "</span>"
        + '<span class="new">' + escapeHtml(fmt(after[k])) + "</span></div>";
    });
  } else {
    rows = '<div class="drow"><span class="k">变更</span><span>' + escapeHtml(log.summary || "—") + "</span></div>";
  }
  return '<div class="minicard" style="margin-bottom:14px"><div class="mc-hd">本次变更</div>'
    + '<div class="kv"><span>时间</span><b>' + escapeHtml(shortTime(log.created_at)) + "</b></div>"
    + '<div class="kv"><span>来源</span><b>' + escapeHtml(log.actor) + "</b></div>"
    + '<div class="kv"><span>动作</span><b>' + escapeHtml(actionLabel(log.action)) + "</b></div>"
    + '<div class="kv"><span>对象</span><b>' + escapeHtml(log.target_id) + "</b></div></div>"
    + '<div class="f"><label>改了什么</label><div class="diff">' + rows + "</div></div>"
    + '<div class="hint">撤销会把该字段恢复为变更前的值；撤销本身也会记入日志。</div>';
}

function fieldLabel(key) {
  var map = { title: "标题", description: "文案", price: "价格", cost_price: "成本价",
    stock: "库存", stock_alert: "预警阈值", category: "类目", status: "状态", pinned: "置顶" };
  return map[key] || key;
}

function fmt(value) {
  if (value === null || value === undefined) { return "—"; }
  if (typeof value === "boolean") { return value ? "是" : "否"; }
  if (typeof value === "object") { return JSON.stringify(value); }
  return String(value);
}

/** 后端返回的图片 URL 若是相对路径（本地存储），拼上 API 基址。 */
function resolveUrl(url) {
  if (!url) { return ""; }
  return /^https?:\/\//.test(url) ? url : API_BASE + url;
}

/* ---------- 右栏事件绑定 ---------- */

function bindRight() {
  var box = $("uploadBox");
  if (box) {
    box.onclick = function () { var f = $("fileInput"); if (f) { f.click(); } };
    var input = $("fileInput");
    if (input) { input.onchange = function () { uploadFiles(input.files); }; }
    box.ondragover = function (e) { e.preventDefault(); };
    box.ondrop = function (e) {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files) { uploadFiles(e.dataTransfer.files); }
    };
  }
  var save = $("btnSave");
  if (save) { save.onclick = saveGoods; }
  var reload = $("btnReload");
  if (reload) { reload.onclick = function () { if (state.sel) { selectGoods(state.sel.id); } }; }
  var del = $("btnDel");
  if (del) { del.onclick = function () { confirmDeleteGoods(state.sel); }; }
  var undo = $("btnUndo");
  if (undo) { undo.onclick = function () { if (state.log) { doUndo(state.log.id); } }; }

  if (state.sel) { loadGoodsLogs(state.sel.goods_no); loadGoodsAftersale(state.sel.id); }
}

/** 该商品的售后情况（商品 × 售后联动）。**后端无模型参与**，纯确定性聚合。 */
async function loadGoodsAftersale(goodsId) {
  var el = $("goodsAftersale");
  if (!el) { return; }
  var r = await api("/api/seller/goods/" + goodsId + "/aftersale-stats");
  if (!r.ok) { el.innerHTML = '<div class="hint">' + escapeHtml(r.error) + "</div>"; return; }
  var d = r.data;
  if (!d.total) {
    // 没有关联工单时**如实说明原因**，而不是显示一堆 0 让人以为"这商品很健康"
    el.innerHTML = '<div class="hint">' + escapeHtml(d.note || "该商品暂无关联的售后工单") + "</div>";
    return;
  }
  var rate = d.refund_rate == null ? "—" : d.refund_rate + "%";
  var rateColor = (d.refund_rate || 0) >= 20 ? "#b91c1c" : (d.refund_rate || 0) > 0 ? "#b45309" : "#15803d";
  var html = '<div class="kv"><span>售后工单</span><b>' + d.total + " 单</b></div>"
    + '<div class="kv"><span>退货率</span><b style="color:' + rateColor + '">' + rate + "</b></div>"
    + '<div class="kv"><span>转人工</span><b>' + d.escalated + " 单</b></div>"
    + '<div class="kv"><span>处理中</span><b>' + d.processing + " 单</b></div>";

  var outcomes = d.outcomes || {};
  var keys = Object.keys(outcomes);
  if (keys.length) {
    html += '<div class="hint" style="margin-top:7px">结果分布：'
      + keys.map(function (k) {
          return escapeHtml(OUTCOME_LABELS[k] || k) + " " + outcomes[k];
        }).join(" · ")
      + "</div>";
  }
  var reasons = d.reasons || {};
  var rkeys = Object.keys(reasons);
  if (rkeys.length) {
    html += '<div class="hint" style="margin-top:4px">转人工原因：'
      + rkeys.map(function (k) {
          return escapeHtml(REASON_LABELS[k] || k) + " " + reasons[k];
        }).join(" · ")
      + "</div>";
  }
  var recent = d.recent_cases || [];
  if (recent.length) {
    html += '<div class="hint" style="margin-top:8px">最近工单：'
      + recent.map(function (c) {
          return '<a href="javascript:void(0)" data-act="open-case" data-case="'
            + escapeHtml(c.case_id) + '" style="color:var(--brand)">'
            + escapeHtml(c.case_id) + "</a>";
        }).join("、")
      + "</div>";
  }
  el.innerHTML = html;
}

async function loadGoodsLogs(goodsNo) {
  var el = $("goodsLogs");
  if (!el) { return; }
  var r = await api("/api/seller/logs?limit=8&target_id=" + encodeURIComponent(goodsNo));
  if (!r.ok) { el.textContent = r.error; return; }
  var items = r.data.items || [];
  if (!items.length) { el.textContent = "暂无记录"; return; }
  el.innerHTML = items.map(function (log) {
    return '<div class="trace-item"><span class="d"></span><div>'
      + escapeHtml(shortTime(log.created_at)) + " " + escapeHtml(log.summary || actionLabel(log.action))
      + "</div></div>";
  }).join("");
}

/* ---------- 动作 ---------- */

async function saveGoods() {
  if (!state.sel) { return; }
  var payload = {
    title: $("fTitle").value,
    description: $("fDesc").value,
    price: Number($("fPrice").value)
  };
  if (state.view === "admin") {
    payload.cost_price = Number($("fCost").value);
    payload.stock = Number($("fStock").value);
    payload.stock_alert = Number($("fAlert").value);
    payload.category = $("fCategory").value;
    payload.status = $("fStatus").value;
  } else {
    var pin = $("fPinned");
    if (pin) { payload.pinned = pin.checked; }
  }
  var r = await api("/api/seller/goods/" + state.sel.id, { method: "PATCH", body: payload });
  if (!r.ok) { toast(r.error, true); return; }
  toast("已保存");
  await refreshAll();
  await selectGoods(state.sel.id);
}

async function uploadFiles(files) {
  if (!state.sel || !files || !files.length) { return; }
  var done = 0;
  for (var i = 0; i < files.length; i++) {
    var fd = new FormData();
    fd.append("file", files[i]);
    var r = await api("/api/seller/goods/" + state.sel.id + "/media", { method: "POST", form: fd });
    if (!r.ok) { toast("第 " + (i + 1) + " 张上传失败：" + r.error, true); break; }
    done += 1;
  }
  if (done) { toast("已上传 " + done + " 张图片"); }
  await refreshAll();
  await selectGoods(state.sel.id);
}

async function deleteMedia(mediaId) {
  var r = await api("/api/seller/goods/" + state.sel.id + "/media/" + mediaId, { method: "DELETE" });
  if (!r.ok) { toast(r.error, true); return; }
  toast("图片已删除");
  await selectGoods(state.sel.id);
}

function confirmDeleteGoods(goods) {
  if (!goods) { return; }
  askConfirm({
    title: "确认删除商品？",
    text: "此操作不可撤销。删除后该商品会同时从「商品陈列」与「商品管理」中消失，图片也会一并删除。",
    box: "商品：<b>" + escapeHtml(goods.title) + "</b><br />资产：¥" + money(goods.price)
      + " · " + ((goods.media || []).length) + " 张图片",
    ico: "!"
  }, async function () {
    closeConfirm();
    var r = await api("/api/seller/goods/" + goods.id + "?confirm=true", { method: "DELETE" });
    if (!r.ok) { toast(r.error, true); return; }
    toast("商品已删除");
    state.sel = null;
    await refreshAll();
    render();
  });
}

async function setStatus(goods, status) {
  var r = await api("/api/seller/goods/" + goods.id, { method: "PATCH", body: { status: status } });
  if (!r.ok) { toast(r.error, true); return; }
  toast(status === "on_shelf" ? "已上架" : "已下架");
  await refreshAll();
  render();
}

var BULK_LABEL = {
  on_shelf: "批量上架", off_shelf: "批量下架",
  delete: "批量删除", price_delta: "批量改价"
};

var STATUS_TEXT = { on_shelf: "已上架", off_shelf: "已下架", draft: "草稿" };

/** 生成**逐件前后对比**的 HTML。
 *
 *  为什么必须做：AI 侧的确认卡本来就有 diff（`_impact_bulk` 会写「¥99 → ¥89」），
 *  但**人工在表格里批量操作时是直接执行的** —— 改价连"改到多少"都看不到。
 *  这不是体验问题，是"人会核对不了自己即将造成的改动"。
 */
function bulkDiffHtml(rows, mode, delta) {
  var bad = 0;
  var head = '<div class="dl dh"><span class="n">商品</span><span class="o">现在</span>'
    + '<span class="a"></span><span class="v">改后</span></div>';

  var body = rows.map(function (g) {
    var before = "";
    var after = "";
    var cls = "";

    if (mode === "price_delta") {
      var next = Number(g.price || 0) + delta;
      before = "¥" + money(g.price);
      after = "¥" + money(next);
      if (next <= 0) { cls = " bad"; after += " ⚠"; bad += 1; }
    } else if (mode === "on_shelf" || mode === "off_shelf") {
      before = STATUS_TEXT[g.status] || g.status || "—";
      after = STATUS_TEXT[mode] || mode;
      if (g.status === mode) { cls = " same"; after += "（不变）"; }
    } else {
      before = "¥" + money(g.price) + " · " + ((g.media || []).length) + " 图";
      after = "删除";
      cls = " bad";
    }

    return '<div class="dl"><span class="n" title="' + escapeHtml(g.title) + '">'
      + escapeHtml(g.title) + "</span>"
      + '<span class="o">' + escapeHtml(before) + "</span>"
      + '<span class="a">→</span>'
      + '<span class="v' + cls + '">' + escapeHtml(after) + "</span></div>";
  }).join("");

  var tip = bad
    ? '<div class="dtip">⚠ 有 ' + bad + " 件改后价格 ≤ 0，后端会拒绝这几件（其余照常执行）。</div>"
    : "";
  return { html: head + body + tip, bad: bad };
}

async function runBulk(ids, mode, delta) {
  var body = { goods_ids: ids, action: mode, confirm: true };
  if (mode === "price_delta") { body.price_delta = delta; }
  var r = await api("/api/seller/goods/bulk", { method: "POST", body: body });
  if (!r.ok) { toast(r.error, true); return; }
  var d = r.data || {};
  var failed = (d.failed || []).length;
  toast("成功 " + (d.done || []).length + " 件" + (failed ? "，失败 " + failed + " 件" : ""));
  if (failed) { toast("失败明细见「任务」页（任务 #" + (d.task_id || "—") + "）", true); }
  state.checked = {};
  await refreshAll();
  render();
}

async function bulkAction(mode, priceDelta) {
  var ids = Object.keys(state.checked).filter(function (k) { return state.checked[k]; }).map(Number);
  if (!ids.length) { toast("未选择任何商品", true); return; }

  var delta = 0;
  if (mode === "price_delta") {
    var raw = window.prompt("输入价格调整金额（元，可为负）：", "-10");
    if (raw === null) { return; }
    delta = Number(raw);
    if (!isFinite(delta) || delta === 0) { toast("金额非法或为 0", true); return; }
  }

  var rows = state.goods.filter(function (g) { return ids.indexOf(g.id) >= 0; });
  var preview = bulkDiffHtml(rows, mode, delta);
  var label = BULK_LABEL[mode] || mode;

  // **一律先预览再执行**（删除、上下架、改价都走同一条路）
  askConfirm({
    title: "确认" + label + "？",
    text: mode === "delete"
      ? "此操作不可撤销。所选商品及其图片会被一并删除。"
      : "请核对下面每件商品将要发生的改动，确认后立即执行。",
    box: preview.html,
    ico: "!"
  }, function () {
    closeConfirm();
    runBulk(ids, mode, delta);
  });
}

async function doUndo(logId) {
  var r = await api("/api/seller/logs/" + logId + "/undo", { method: "POST" });
  if (!r.ok) { toast(r.error, true); return; }
  toast("已撤销");
  state.log = null;
  await refreshAll();
  render();
}

async function reorder(orderedIds) {
  var r = await api("/api/seller/goods/reorder", { method: "POST", body: { ordered_ids: orderedIds } });
  if (!r.ok) { toast(r.error, true); return; }
  toast("陈列顺序已保存");
  await loadGoods();
  render();
}

async function newGoods() {
  var title = window.prompt("新商品的标题：", "");
  if (!title) { return; }
  var r = await api("/api/seller/goods", { method: "POST", body: { title: title } });
  if (!r.ok) { toast(r.error, true); return; }
  toast("已创建草稿，请在右侧完善信息后上架");
  await refreshAll();
  state.view = "admin";
  render();
  await selectGoods(r.data.id);
}

async function refreshAll() {
  state.lastError = "";
  // ⚠️ **必须并行**：这些请求互不依赖（谁先回来都不影响别人），串行时每一个都要
  // 先付一道"网络往返 + 后端建库连接"的固定开销，六项叠加就是"点一下卡一秒"。
  // 串行版本实测 1.62s / 39 条 MySQL 连接；并行后墙钟由最慢一项决定。
  // 依赖关系已在此处**先算好**：`state.view` 与 `state.knowledge.loaded` 都是同步读的，
  // 放进 jobs 之后再 await，不会读到别人中途改过的值。
  var jobs = [
    loadHealth(),
    loadAftersaleCount(),
    // 左栏的「陈列 10 / 管理 118 / 日志 5」全部来自 overview：只在概览页拉，
    // 直接进「商品陈列」时角标就是 0，与页面上实际 10 件商品自相矛盾。
    loadOverview()
  ];
  if (state.view === "overview") { jobs.push(loadGoods()); }
  else if (state.view === "audit") { jobs.push(loadLogs()); }
  else if (state.view === "knowledge") { jobs.push(loadKnowledge()); }
  else if (state.view === "orders" || state.view === "service") { jobs.push(loadCommerce(state.view)); }
  else if (state.view !== "aftersale" && state.view !== "tasks") { jobs.push(loadGoods()); }
  // 知识库：**任何视图都要**文档数（左栏角标），但只在首次拉一次，避免每次刷新多一次请求
  if (state.view !== "knowledge" && !state.knowledge.loaded) { jobs.push(loadKnowledge()); }
  // 商品管理需要回收站数量（tab 角标）
  if (state.view === "admin") { jobs.push(loadTrash()); }
  // 任务数要进左栏角标，任何视图都需要
  jobs.push(loadTasks());
  // `api()` 内部已 catch 网络与 HTTP 错误并返回 `{ok:false}`，这里不会有 reject，
  // 因此 `Promise.all` 不会因为单项失败而丢掉整批结果。
  await Promise.all(jobs);
}

/* ---------- 任务中心（长任务 + 批量操作记录） ---------- */

var TASK_STATUS = {
  queued: "排队中", running: "执行中", done: "已完成",
  failed: "失败", interrupted: "已中断"
};

var TASK_KIND = {
  ai_cover: "AI 生图", ai_copy: "AI 文案", ai_price: "AI 定价", bulk_op: "批量操作"
};

async function loadTasks() {
  var r = await api("/api/seller/tasks?limit=50");
  state.tasks = r.ok ? (r.data.items || []) : [];
  state.taskRunning = state.tasks.filter(function (t) {
    return t.status === "running" || t.status === "queued";
  }).length;
  scheduleTaskPoll();
}

/** 有进行中的任务时每 3 秒刷新一次。
 *
 *  这就是"关掉浮窗也在跑、回来还能看"的落点：任务在**后端**协程里跑，
 *  前端只是定时问一句"到哪了"，关掉页面不影响任务本身。
 */
function scheduleTaskPoll() {
  clearTimeout(state._taskTm);
  if (!state.taskRunning) { return; }
  state._taskTm = setTimeout(function () {
    loadTasks().then(function () {
      renderSide();
      if (state.view === "tasks") { renderMid(); }
    });
  }, 3000);
}

function hdTasks() {
  return '<h2>任务</h2><span class="sub">AI 长任务与批量操作的结果都在这里 · '
    + (state.taskRunning ? state.taskRunning + " 个进行中" : "没有进行中的任务") + "</span>"
    + '<div class="spacer"></div>'
    + '<button class="btn" data-act="task-refresh">刷新</button>';
}

/** 重试失败任务。生图重试会**再计费一次**，所以走二次确认而不是直接发请求。 */
function retryTask(taskId) {
  var t = (state.tasks || []).filter(function (x) { return x.id === taskId; })[0] || {};
  askConfirm({
    title: "重试这个任务？",
    text: t.kind === "ai_cover"
      ? "会重新生成一张宣传图，再产生一次费用。"
      : "会重新执行一次这个任务。",
    box: escapeHtml(t.title || ""),
    ico: "?",
  }, async function () {
    closeConfirm();
    var r = await api("/api/seller/tasks/" + taskId + "/retry", { method: "POST" });
    if (!r.ok) { toast(r.error, true); return; }
    toast("已重新提交，进度可在任务中心查看");
    await refreshAll();
    render();
  });
}

function bodyTasks() {
  var items = state.tasks || [];
  if (!items.length) {
    return errBanner() + '<div class="panel"><div class="pb" style="padding:44px;text-align:center">'
      + '<div class="hint">还没有任务。<br />AI 生图、生成文案、批量操作都会记录在这里 —— '
      + "即使关掉这个页面，后台也会继续跑。</div></div></div>";
  }

  var html = errBanner();
  items.forEach(function (t) {
    var st = t.status;
    var cls = st === "failed" ? "bad" : st === "done" ? "good"
      : st === "interrupted" ? "warn" : "run";
    var running = st === "running" || st === "queued";

    html += '<div class="task ' + cls + '">'
      + '<div class="th"><span class="tb ' + cls + '">' + escapeHtml(TASK_STATUS[st] || st) + "</span>"
      + "<b>" + escapeHtml(t.title || "") + "</b>"
      + '<span class="kind">' + escapeHtml(TASK_KIND[t.kind] || t.kind || "") + "</span>"
      + '<span class="sp"></span>'
      + '<span class="hint">' + escapeHtml(shortTime(t.created_at)) + "</span></div>";

    if (running) {
      html += '<div class="bar" style="margin:7px 0 4px"><i style="width:' + (t.progress || 0)
        + '%"></i></div><div class="hint">' + escapeHtml(t.stage || "排队中")
        + " · " + (t.progress || 0) + "%</div>";
    }
    if (t.result) {
      html += '<div class="hint" style="margin-top:3px">' + escapeHtml(t.result) + "</div>";
    }
    if (t.error) {
      html += '<div class="terr">' + escapeHtml(t.error) + "</div>";
    }
    // 重试：是否可重试由**后端**判定（retryable），前端不维护白名单
    if (t.retryable) {
      html += '<div class="ops" style="margin-top:8px">'
        + '<button class="btn sm" data-act="retry-task" data-id="' + t.id + '">重试</button>'
        + '<span class="hint" style="margin-left:8px">重新执行一次'
        + (t.kind === "ai_cover" ? "（生图会再计费一次）" : "") + "</span></div>";
    }
    // 批量操作的**失败明细** —— 这是"部分失败是常态"的落点，必须能逐条看到
    var failed = (t.detail && t.detail.failed) || [];
    if (failed.length) {
      html += '<div class="tdet"><b>失败明细 ' + failed.length + " 项</b><ul>"
        + failed.slice(0, 10).map(function (f) {
            return "<li>" + escapeHtml(String(f.goods_id || f.id || f.case_id || "?"))
              + " — " + escapeHtml(f.error || "未知原因") + "</li>";
          }).join("")
        + "</ul></div>";
    }
    html += "</div>";
  });
  return '<div class="panel"><div class="pb" style="padding:8px 12px">' + html + "</div></div>";
}

/* ============================================================
   知识库（RAG 检索）
   ============================================================
   与后端的三条对应关系：
   1. 上传**同步入库**（解析 → 分块 → 向量化），接口返回即代表"已经能搜到"——
      不做成后台任务，否则"上传成功但还搜不到"会变成常态；
   2. 试检索走 `/knowledge/search`，与 AI 助手用的是**同一条检索链路**：
      "你搜得到，助手就搜得到"，不会出现两套结果；
   3. 删除资料**不可恢复**，走二次确认（与商品删除同一条纪律）。
   ============================================================ */

function hdKnowledge() {
  var kb = state.knowledge || {};
  var st = kb.stats || null;
  var sub = st
    ? ("共 " + st.documents + " 份资料 · " + (st.kbs || 0) + " 个知识库 · " + st.chunks + " 个片段")
    : "上传售后政策 / 平台规则 / 商品资料，AI 答题时按原文引用";
  return "<h2>知识库</h2><span class=\"sub\">" + escapeHtml(sub) + "</span>"
    + '<div class="spacer"></div>'
    + (st && st.embedding_model
        ? '<span class="hint" style="margin-right:10px">向量模型 ' + escapeHtml(st.embedding_model) + "</span>"
        : "")
    // 图片识别未启用时**说清楚**：否则卖家传了截图一直等不到内容，会以为系统坏了
    + (st && st.ocr_enabled === false
        ? '<span class="hint" style="margin-right:10px;color:#b45309">图片识别未启用</span>'
        : "")
    // 头部不再放「上传资料」按钮：上传前必须先定"进哪个库、打什么标签"，
    // 位于头部的按钮会绕过这两个输入直接开文件框，结果全落默认库还没有标签。
    // 入口统一放在下方上传卡里（按钮贴着那两个输入框）。
    + "";
}

function bodyKnowledge() {
  var kb = state.knowledge || {};
  var kbs = kb.kbs || [];
  var st = kb.stats || null;
  var html = errBanner();

  // ---- 上传卡：目标知识库 + 标签 + 选文件 ----
  // 知识库名做成**可输入 + 候选**（datalist），而不是下拉：
  // 下拉只能选已有的，"建新库"就得再加一个「＋新建…」分支和一排临时输入框，
  // 多一层状态多一个重绘坑；直接输入名字，留空＝默认知识库，写名字＝（新建或并入）同名库。
  html += '<div class="panel" style="margin-bottom:14px">'
    + '<div class="ph"><b>上传资料</b><span class="spacer"></span>'
    + '<span class="hint">PDF / Word / Markdown / 纯文本 / 截图</span></div>'
    + '<div class="pb"><div class="kbform">'
    + '<input class="field" id="kbTarget" list="kbNames" style="width:210px"'
    + ' placeholder="知识库（留空＝默认知识库）" value="' + escapeHtml(kb.uploadKb || "") + '" />'
    + kbNamesHtml()
    + '<input class="field" id="kbTags" style="width:210px"'
    + ' placeholder="标签，逗号分隔（如：退货,时效）" value="' + escapeHtml(kb.tags || "") + '" />'
    + '<button class="btn primary" data-kb="pick">选择文件并入库</button>'
    + '<input type="file" id="kbFile" hidden'
    + ' accept=".txt,.md,.markdown,.pdf,.docx,.png,.jpg,.jpeg,.webp,.bmp" />'
    + "</div>"
    + '<div class="hint" style="margin-top:7px;padding:0 16px 12px;line-height:1.75">'
    // 图片能不能用，取决于视觉模型是否启用 —— 不能不说，否则传了截图一直没反应像"坏了"
    + (st && st.ocr_enabled === false
        ? "图片识别未启用：现在只能上传文本类文件。"
        : "截图会先识别成文字再入库（这一步会消耗视觉模型用量）。")
    + "职责不同的资料建议分开建库（如「售后政策」「平台规则」「商品资料」），"
    + "助手回答时可以只在你指定的库里找，不会被别的资料干扰。"
    + "</div></div></div>";

  // ---- 试检索：卖家自己先确认"资料能被找到" ----
  html += '<div class="panel" style="margin-bottom:14px">'
    + '<div class="ph"><b>先搜一下试试</b><span class="spacer"></span>'
    + '<span class="hint">你搜到什么，助手就按这些原文回答</span></div>'
    + '<div class="pb">'
    + '<div class="kbsearch">'
    + kbScopeSelect()
    + '<input class="field" id="kbQ" placeholder="例如：退货时限是多久 / 补发条件 / 发货时效"'
    + ' value="' + escapeHtml(kb.q || "") + '" />'
    + '<button class="btn primary" data-kb="search">检索</button>'
    + "</div>"
    + '<div id="kbResults" class="kbres">' + kbResultsHtml() + "</div>"
    + "</div></div>";

  // ---- 已入库资料 ----
  var docs = kb.docs || [];
  html += '<div class="panel"><div class="ph"><b>已入库资料</b><span class="spacer"></span>'
    + kbFilterSelect()
    + '<span class="hint">' + docs.length + " 份</span></div>"
    + '<div class="pb" style="padding:8px 12px">';

  if (!docs.length) {
    html += '<div class="hint" style="padding:20px 6px;text-align:center">'
      + (kb.kbFilter
          // 筛选导致的空，与真的没有资料是两件事 —— 提示语必须分开，否则卖家会以为资料丢了
          ? "「" + escapeHtml(kb.kbFilter) + "」里还没有资料。把上方下拉切回「全部知识库」看看，"
            + "或者在「上传资料」里把目标知识库填成它。"
          : "还没有资料。把售后政策、平台规则、操作手册传到上面那个框里"
            + "（支持 PDF / Word / Markdown / 纯文本 / 截图，单份不超过 10 MB）。<br />"
            + "入库后，助手回答「我们退货怎么规定」这类问题时就会引用原文，而不是凭常识编。")
      + "</div>";
  } else {
    docs.forEach(function (d) { html += kbDocRow(d, kbs); });
  }
  html += "</div></div>";
  return html;
}

/** 知识库候选清单（datalist）：让「建过的库」出现在输入提示里，不用记名字。 */
function kbNamesHtml() {
  var kbs = (state.knowledge && state.knowledge.kbs) || [];
  return '<datalist id="kbNames">'
    + kbs.map(function (k) {
        return '<option value="' + escapeHtml(k.kb) + '">' + escapeHtml(String(k.documents)) + " 份</option>";
      }).join("")
    + "</datalist>";
}

/** 检索范围：全部知识库 / 指定库。库只有一个时不摆这个下拉（摆着也是白占位）。 */
function kbScopeSelect() {
  var kb = state.knowledge || {};
  var kbs = kb.kbs || [];
  if (!kbs.length) { return ""; }
  return '<select class="field" id="kbScope" style="width:150px">'
    + '<option value="">全部知识库</option>'
    + kbs.map(function (k) {
        return '<option value="' + escapeHtml(k.kb) + '"'
          + ((kb.searchKb || "") === k.kb ? " selected" : "") + ">"
          + escapeHtml(k.kb) + "（" + escapeHtml(String(k.documents)) + "）</option>";
      }).join("")
    + "</select>";
}

/** 文档清单筛选：只在存在多个库时出现。 */
function kbFilterSelect() {
  var kb = state.knowledge || {};
  var kbs = kb.kbs || [];
  if (kbs.length < 2) { return ""; }
  return '<select class="field" id="kbFilter" style="width:150px;margin-right:10px">'
    + '<option value="">全部知识库</option>'
    + kbs.map(function (k) {
        return '<option value="' + escapeHtml(k.kb) + '"'
          + ((kb.kbFilter || "") === k.kb ? " selected" : "") + ">"
          + escapeHtml(k.kb) + "（" + escapeHtml(String(k.documents)) + "）</option>";
      }).join("")
    + "</select>";
}

/** 一份资料的整行。改归属时**只把这一行换成输入框**（其余行不动）。 */
function kbDocRow(d, kbs) {
  var kb = state.knowledge || {};
  var editing = kb.moving === d.doc_id;
  var tags = String(d.tags || "").split(",").filter(function (t) { return t.trim(); });

  var html = '<div class="kbdoc">'
    + '<div class="kbi">档</div>'
    + '<div class="kbtx"><b>' + escapeHtml(d.filename) + "</b>"
    + '<div class="kbbadge">'
    + '<span class="kgtag">' + escapeHtml(d.kb || "默认知识库") + "</span>"
    + tags.map(function (t) { return '<span class="kbtag">' + escapeHtml(t.trim()) + "</span>"; }).join("")
    + "</div>"
    + "<em>" + escapeHtml(String(d.chunks)) + " 个片段 · "
    + escapeHtml(shortTime(d.created_at) || "—") + "</em></div>";

  if (editing) {
    html += '<div class="kbmove">'
      + '<input class="field" id="kbMoveTo" list="kbNames" style="width:150px"'
      + ' placeholder="知识库名称" value="' + escapeHtml(d.kb || "") + '" />'
      + '<button class="btn sm primary" data-kb="moveSave" data-id="' + escapeHtml(d.doc_id) + '">保存</button>'
      + '<button class="btn sm" data-kb="moveCancel">取消</button>'
      + "</div>";
  } else {
    html += '<div class="ops">'
      + '<button class="btn sm" data-kb="move" data-id="' + escapeHtml(d.doc_id) + '">改归属</button>'
      + '<button class="btn sm danger" data-kb="del" data-id="' + escapeHtml(d.doc_id) + '">删除</button>'
      + "</div>";
  }
  return html + "</div>";
}

/** 检索结果区。抽成函数是因为**只重绘这一块**，不整栏重绘 ——
 *  整栏重绘会重建输入框，正在输入的内容和光标都会丢（本项目已踩过这个坑）。 */
function kbResultsHtml() {
  var kb = state.knowledge || {};
  if (kb.results === null || kb.results === undefined) {
    return '<div class="hint">输入一个问题，看看资料里有没有答案。</div>';
  }
  if (!kb.results.length) {
    return '<div class="hint">没有检索到相关内容。<br />'
      + "可以换个说法；或先上传对应资料（检索只看已入库的内容）。</div>";
  }
  var html = "";
  kb.results.forEach(function (it, index) {
    // 出处要带**知识库名**：同名文件可能存在于不同库里，只写文件名分不清是哪一份
    var where = (it.kb ? it.kb + " · " : "") + (it.filename || "")
      + (it.section_title ? " · " + it.section_title : "")
      + (it.page_number ? " · 第 " + it.page_number + " 页" : "");
    html += '<div class="kbhit">'
      + '<div class="kbh"><b>[' + (index + 1) + "]</b> " + escapeHtml(where)
      + '<span class="spacer"></span><span class="hint">相关度 '
      + escapeHtml(String(it.similarity)) + "</span></div>"
      + '<div class="kbt">' + escapeHtml(it.text) + "</div>"
      + "</div>";
  });
  return html;
}

async function loadKnowledge() {
  var kb = state.knowledge;
  // 按库筛选走服务端（`?kb=`）：库里的资料可能很多，前端过滤既费流量又容易与统计口径不一致
  var url = "/api/seller/knowledge"
    + (kb.kbFilter ? "?kb=" + encodeURIComponent(kb.kbFilter) : "");
  var r = await api(url);
  kb.loaded = true;
  if (!r.ok) { state.lastError = r.error; return; }
  kb.docs = r.data.items || [];
  kb.kbs = r.data.kbs || [];
  kb.stats = r.data.stats || null;
}

function pickKnowledgeFile() {
  var input = $("kbFile");
  if (input) { input.click(); }
}

async function uploadKnowledge(file, opts) {
  if (!file) { return; }
  var kb = state.knowledge;
  if (kb.busy) { toast("上一份还在入库，稍等一下", true); return; }

  // 目标知识库与标签**只在"中栏上传卡"存在时**从界面读；
  // 从对话浮窗（回形针）上传时没有这张卡，就沿用上次的值，没有则落默认知识库。
  var targetEl = $("kbTarget");
  var tagsEl = $("kbTags");
  var kbName = targetEl ? String(targetEl.value || "").trim() : (kb.uploadKb || "");
  var tags = tagsEl ? String(tagsEl.value || "").trim() : (kb.tags || "");
  kb.uploadKb = kbName;
  kb.tags = tags;
  if (!kbName) { kbName = ""; }        // 空 = 后端归一成「默认知识库」

  kb.busy = true;
  toast("正在解析并入库：" + file.name);
  var fd = new FormData();
  fd.append("file", file);
  fd.append("kb", kbName);
  fd.append("tags", tags);
  var r = await api("/api/seller/knowledge/upload", { method: "POST", form: fd });
  kb.busy = false;
  if (!r.ok) { toast(r.error, true); return null; }

  toast(r.data.message || "已入库");
  // 传进了别的库而当前正筛选着另一个库 → 新资料会"看不见"，直接把筛选放开
  if (kb.kbFilter && r.data.kb && r.data.kb !== kb.kbFilter) { kb.kbFilter = ""; }
  await loadKnowledge();
  if (!opts || opts.render !== false) { render(); }
  return r.data;
}

async function searchKnowledge() {
  var input = $("kbQ");
  var q = ((input && input.value) || "").trim();
  var scopeEl = $("kbScope");
  var kb = state.knowledge;
  kb.q = q;
  kb.searchKb = scopeEl ? String(scopeEl.value || "") : (kb.searchKb || "");
  if (!q) { toast("先输入要查的内容", true); return; }
  if (kb.busy) { return; }
  kb.busy = true;

  var box = $("kbResults");
  if (box) { box.innerHTML = '<div class="hint">检索中…</div>'; }

  var r = await api("/api/seller/knowledge/search",
    { method: "POST", body: { query: q, kb: kb.searchKb || "" } });
  kb.busy = false;
  if (!r.ok) {
    kb.results = null;
    if (box) {
      box.innerHTML = '<div class="hint" style="color:#b91c1c">' + escapeHtml(r.error) + "</div>";
    }
    return;
  }
  kb.results = r.data.items || [];
  // 只更新结果容器（不整栏重绘）：保留输入框内容与焦点
  if (box) { box.innerHTML = kbResultsHtml(); }
}

function deleteKnowledgeDoc(docId) {
  var doc = (state.knowledge.docs || []).filter(function (d) { return d.doc_id === docId; })[0];
  askConfirm({
    title: "删除这份资料？",
    text: "删除后助手就检索不到它的内容了，且**无法恢复**（需要重新上传）。",
    box: doc ? "资料：<b>" + escapeHtml(doc.filename) + "</b><br />片段：" + doc.chunks + " 个" : "",
    ico: "!",
  }, async function () {
    closeConfirm();
    var r = await api("/api/seller/knowledge/" + encodeURIComponent(docId), { method: "DELETE" });
    if (!r.ok) { toast(r.error, true); return; }
    toast("已删除");
    state.knowledge.results = null;      // 结果可能引用了已删资料，清掉更诚实
    await loadKnowledge();
    render();
  });
}

/** 开始改归属：只把这一行切成输入框（`state.knowledge.moving`），不弹窗、不整栏重建。 */
function beginMoveKnowledgeDoc(docId) {
  state.knowledge.moving = docId || "";
  renderMid();          // 同视图重绘会保留 scrollTop（见 renderMid 里的 keep）
  var input = $("kbMoveTo");
  if (input) { input.focus(); if (input.select) { input.select(); } }
}

function cancelMoveKnowledgeDoc() {
  state.knowledge.moving = "";
  renderMid();
}

async function saveKnowledgeDocKb(docId) {
  var input = $("kbMoveTo");
  var name = String((input && input.value) || "").trim();
  var kb = state.knowledge;
  if (!docId) { return; }
  // 空名字 = 挪回默认知识库（后端会把空串归一成默认库，这里先说清楚，避免"点了保存没变化"的错觉）
  var r = await api("/api/seller/knowledge/" + encodeURIComponent(docId),
    { method: "PATCH", body: { kb: name } });
  if (!r.ok) { toast(r.error, true); return; }
  kb.moving = "";
  toast("已改到「" + (r.data.kb || "默认知识库") + "」");
  // 筛选着旧库时，改走的那份会在当前筛选下消失 —— 这是对的，但要说一声（toast 已说明去向）
  await loadKnowledge();
  render();
}

/* ============================================================
   AI 经营助手（对话抽屉）
   ============================================================
   两点与后端机制的对应关系：
   1. **待确认卡**不是装饰：确认调用 `POST /actions/{id}/confirm` 才真正执行，
      取消则是 `…/cancel`，数据零改动。
   2. 助手做过任何工具调用后，界面会**重新拉取数据** —— 否则你会在抽屉里看到
      "已把价格改成 89"，而右边的列表还是旧价。
   ============================================================ */

var VIEW_LABELS = {
  overview: "概览", gallery: "商品陈列", admin: "商品管理",
  orders: "订单", service: "客服", aftersale: "售后", audit: "操作日志", tasks: "任务", knowledge: "知识库"
};

/** 售后判定结果的中文（后端只给码，展示层负责翻译）。 */
var OUTCOME_LABELS = {
  refund: "退款", reship: "补发", reject: "拒绝", manual: "转人工",
  pending_evidence: "需补证", processing: "处理中"
};

/** 转人工原因码的中文。 */
var REASON_LABELS = {
  round_limit: "轮数耗尽", msg_too_long: "字数超限", cost_paused: "成本暂停",
  rule_to_human: "规则转人工", tool_failed: "工具失败", buyer_request: "买家要求",
  session_timeout: "会话超时", model_error: "模型异常", model_unavailable: "模型不可用"
};

var CHAT = {
  open: false, messages: [], caps: [], capsLoaded: false, pending: [], busy: false,
  issues: [],          // 巡检结果（纯数据聚合，不调模型）
  skipped: {},         // 多步确认卡里被「划掉」的步骤（id → true），随卡一起提交
  // 已经点过确认/取消的卡：`{ <卡 key>: "confirm" | "cancel" }`。
  // **点下去就撤掉按钮** —— 请求往返期间按钮还在，卖家会以为没点上、再点一次
  // （或被"是否要重复执行"吓到）。卡片留在原地只是**不再可点**，等结果出来再定去留。
  pendingBusy: {},
  // 刚执行完、有步骤失败的批次：`{ batchId, count }` —— 在聊天里给一个「重试失败项」入口。
  failedBatch: null,
};

var ISSUE_LABELS = {
  low_stock: "低库存", draft_incomplete: "草稿不完整", aftersale_waiting: "售后待接手",
  task_failed: "任务失败", trash_expiring: "回收站到期",
};

function aiContext() {
  return {
    view: state.view,
    selected_goods_no: (state.sel && state.sel.goods_no) || ""
  };
}

function updateAiCtx() {
  var el = $("aiCtx");
  if (!el) { return; }
  var ctx = aiContext();
  el.textContent = "当前上下文：" + (VIEW_LABELS[ctx.view] || ctx.view || "—")
    + (ctx.selected_goods_no ? " · 已选中 " + ctx.selected_goods_no : "");
}

/** 把浮窗摆到 AI 按钮**上方**（上方放不下就放下方），右边缘与按钮对齐。
 *
 *  浮窗**跟着按钮走**：按钮被拖到哪里，浮窗就在哪里出现 —— 这是"浮窗与按钮绑定"的实现。
 *  ⚠️ 摆位用 `left/top` 绝对坐标，不用 `transform`；开关用 `display`（见 CSS 注释），
 *  因此不存在"滑出屏幕却露出一角"这种假收起。
 */
function positionDrawer() {
  var drawer = $("drawer");
  var fab = $("fab");
  if (!drawer || !fab) { return; }

  var f = fab.getBoundingClientRect();
  var w = drawer.offsetWidth || 384;
  var h = drawer.offsetHeight || 480;
  var gap = 14;                              // 与按钮留出空隙，避免视觉上"贴着/压着"

  var left = f.right - w;                    // 右边缘与按钮右边缘对齐
  var top = f.top - h - gap;                 // 优先放上方
  if (top < 8) { top = f.bottom + gap; }     // 上方放不下 → 放下方
  left = Math.max(8, Math.min(window.innerWidth - w - 8, left));
  top = Math.max(8, Math.min(window.innerHeight - h - 8, top));

  drawer.style.left = left + "px";
  drawer.style.top = top + "px";
  drawer.style.right = "auto";
  drawer.style.bottom = "auto";
}

function toggleDrawer(open) {
  CHAT.open = (open === undefined) ? !CHAT.open : !!open;
  $("drawer").classList.toggle("open", CHAT.open);
  if (!CHAT.open) { return; }          // 收起：class 移除 → display:none，硬性消失
  updateAiCtx();
  positionDrawer();                    // 展开后先摆位（此时 display:flex，能量到真实高度）
  if (!CHAT.messages.length && !CHAT.pending.length) { loadChat(); }
  var box = $("chat");
  if (box) { box.scrollTop = box.scrollHeight; }
}

async function loadChat() {
  // 三项互不依赖；并行读取，避免首次展开时把数据库等待时间相加。
  var results = await Promise.all([
    api("/api/seller/assistant/messages"),
    api("/api/seller/assistant/actions"),
    api("/api/seller/assistant/issues")
  ]);
  var r = results[0];
  CHAT.messages = r.ok ? (r.data.items || []) : [];
  var p = results[1];
  CHAT.pending = p.ok ? (p.data.items || []) : [];
  // 只保留"还在清单里"的划掉标记：否则项被处理掉后 id 被复用，新卡会莫名处于已划掉状态
  pruneSkipped();
  // 巡检：**纯数据聚合、不调模型**（打开助手时安全调用）
  var i = results[2];
  CHAT.issues = i.ok ? (i.data.issues || []) : [];
  renderChat();
}

/** 待确认角标：**显示数量**，而不是一个没有信息的红点。
 *
 *  纯红点是"未读"的语义（看过就该消失），而待确认是"待办"（未处理前应持续提醒）。
 *  两者混用会让人困惑「我是不是漏看了什么」，也分不清轻重。
 *  所以改成数字角标 + 文字提示；另外后端会作废**超过 24 小时**未处理的项，
 *  避免"关掉页面忘了就永远亮着红点"。
 */
function updatePendingDot() {
  var dot = $("fabDot");
  if (!dot) { return; }
  var n = CHAT.pending.length;
  dot.hidden = n === 0;
  dot.textContent = n > 9 ? "9+" : String(n);
  dot.title = "有 " + n + " 项待你确认（24 小时内有效）";
}

function renderChat() {
  var box = $("chat");
  if (!box) { return; }
  var html = "";
  if (!CHAT.messages.length && !CHAT.pending.length) {
    html += '<div class="msg bot">我是你的经营助手，可以帮你管商品、排陈列、看售后。\n'
      + '比如：「看看哪些商品快没货了」「把 G-0001 的价格改成 89」「这件商品还有多少库存」。</div>';

    // 巡检摘要：让助手**先开口**（数据真实、零模型调用；只提建议，不自动执行）
    if (CHAT.issues && CHAT.issues.length) {
      html += '<div class="msg bot">🩺 我顺手巡了一遍，有 <b>' + CHAT.issues.length + '</b> 件事值得处理：\n'
        + CHAT.issues.slice(0, 5).map(function (it) {
            return "· [" + escapeHtml(ISSUE_LABELS[it.kind] || it.kind) + "] "
              + escapeHtml(it.title) + " —— " + escapeHtml(it.detail);
          }).join("\n")
        + (CHAT.issues.length > 5 ? "\n· ……还有 " + (CHAT.issues.length - 5) + " 件" : "")
        + "\n\n想先处理哪件？说一声我就去办（改动数据或产生费用时仍会让你确认）。\n"
        + "（我固定检查 5 类；想知道每类的判定标准、或**我不看什么**，可以直接问我。）</div>";
    }
  }
  CHAT.messages.forEach(function (m) {
    html += '<div class="msg ' + (m.role === "seller" ? "me" : "bot") + '">'
      + escapeHtml(m.content) + "</div>";
  });
  // 待确认项**按批次收成一张卡**：同一句要求里的多步 = 一次确认、全部执行。
  // 旧卡没有批次号 → 每个自成一组，仍按原来的单卡交互（不假装能合并）。
  pendingBatches().forEach(function (group) { html += pendingCard(group); });
  // 刚执行完有步骤失败的批次：给一个「重试失败项」入口，否则失败项彻底消失、
  // 卖家只能重新跟助手说一遍（会把成功项也重新提交）。
  if (CHAT.failedBatch) {
    html += '<div class="pend batch failed-retry">'
      + '<div class="ttl">有 ' + CHAT.failedBatch.count + ' 步失败了</div>'
      + '<div class="acts">'
      + '<button class="btn sm" data-ai="batch-retry" data-bid="'
      + escapeHtml(CHAT.failedBatch.batchId) + '">重试失败项</button>'
      + '</div></div>';
  }
  if (CHAT.busy) { html += '<div class="msg bot busy">正在处理…</div>'; }
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
  updatePendingDot();
}

/** 丢掉已不在清单里的「划掉」标记（项被处理掉后 id 可能被复用）。 */
function pruneSkipped() {
  var alive = {};
  (CHAT.pending || []).forEach(function (x) {
    if (CHAT.skipped[x.id]) { alive[x.id] = true; }
  });
  CHAT.skipped = alive;
}

/** 一张卡在 `pendingBusy` 里的键：批次号，或旧卡（无批次号）的 `solo-<id>`。
 *  分组与"是否已提交"必须用**同一个键**，否则按钮撤了却按另一个键查状态。 */
function cardKey(p) {
  return (p && p.batch_id) || ("solo-" + (p && p.id));
}

/** 把待确认项按 `batch_id` 分组（保持服务端给出的顺序）。 */
function pendingBatches() {
  var order = [], map = {};
  (CHAT.pending || []).forEach(function (p) {
    var key = cardKey(p);      // 没有批次号的旧卡各自成组
    if (!map[key]) { map[key] = { id: p.batch_id || "", key: key, items: [] }; order.push(map[key]); }
    map[key].items.push(p);
  });
  return order;
}

/** 一张确认卡：单步沿用原样式；多步列出全部步骤 + 一个「全做」按钮。 */
function pendingCard(group) {
  var items = group.items || [];
  var batchId = group.id || "";
  var multi = items.length > 1;
  var billable = items.some(function (p) { return p.kind === "billable"; });
  var billableCount = items.filter(function (p) { return p.kind === "billable"; }).length;
  var destructive = items.some(function (p) { return p.kind === "destructive"; });
  var key = group.key || (items[0] && cardKey(items[0])) || "";
  // 已点过（确认/取消）的卡：**不再渲染任何按钮**，只留一句状态 ——
  // 这样"点过一次"在界面上是看得见的，也没法被点第二次。
  var commit = CHAT.pendingBusy[key] || "";

  var cls = "pend" + (multi ? " batch" : "")
    + (destructive ? "" : (billable ? " billable" : ""))
    + (commit ? " submitted" : "");
  var html = '<div class="' + cls + '" data-batch="' + escapeHtml(batchId) + '">';

  html += '<div class="ttl">'
    + (multi ? "这次有 " + items.length + " 步要你确认"
               + (billableCount ? "，其中 " + billableCount + " 步会产生费用" : "")
             : (billable ? "这一步会产生费用，确认后执行" : "需要你确认后才能执行"))
    + "</div>";

  if (multi) {
    // 已提交后不再提"可以单独划掉" —— 跳过按钮已经撤了，还说能划就是自相矛盾
    html += (commit ? "" : '<div class="bsub">一次确认就<b>全部执行</b>；其中某步不想做，可以单独划掉。</div>')
      + '<ul class="psteps">';
    items.forEach(function (p, i) {
      var skipped = !!CHAT.skipped[p.id];
      var kindText = p.kind === "billable" ? "会产生费用"
        : (p.kind === "destructive" ? "不可撤销" : "");
      html += '<li class="pstep' + (skipped ? " skip" : "") + '" data-step="' + p.id + '">'
        + '<span class="pn">' + (i + 1) + "</span>"
        + '<div class="ptx"><b>' + escapeHtml(p.summary || "（未命名操作）") + "</b>"
        + (kindText || p.confirm_note
            ? "<em>" + escapeHtml(kindText)
              + (kindText && p.confirm_note ? " · " : "")
              + escapeHtml(p.confirm_note || "") + "</em>"
            : "")
        + (p.detail ? '<em class="pdet">' + escapeHtml(p.detail) + "</em>" : "")
        + "</div>"
        + (batchId && !commit
            ? '<button class="btn sm ghost" data-ai="skip" data-id="' + p.id + '">'
              + (skipped ? "恢复" : "跳过") + "</button>"
            : "")
        + "</li>";
    });
    html += "</ul>";
  } else {
    var p = items[0] || {};
    html += escapeHtml(p.summary || "（未命名操作）");
    if (p.detail) { html += '<div class="det">' + escapeHtml(p.detail) + "</div>"; }
    if (p.confirm_note) { html += '<div class="det">' + escapeHtml(p.confirm_note) + "</div>"; }
  }

  html += '<div class="acts">';
  if (commit) {
    html += '<span class="hint">'
      + (commit === "cancel" ? "正在取消…" : "正在执行…") + "，请稍候</span>";
  } else if (batchId) {
    html += '<button class="btn sm ' + (destructive ? "danger" : "primary") + '"'
      + ' data-ai="batch-confirm" data-bid="' + escapeHtml(batchId) + '">'
      + (multi ? "确认执行这 " + items.length + " 步" : "确认并执行") + "</button>"
      + '<button class="btn sm" data-ai="batch-cancel" data-bid="' + escapeHtml(batchId) + '">'
      + (multi ? "全部取消" : "取消") + "</button>";
  } else {
    // 升级前建的卡没有批次号：保留单卡确认，不硬塞进批次逻辑
    html += '<button class="btn sm ' + (billable ? "primary" : "danger") + '"'
      + ' data-ai="confirm" data-id="' + p.id + '">确认执行</button>'
      + '<button class="btn sm" data-ai="cancel" data-id="' + p.id + '">取消</button>';
  }
  // 多步时工具名不止一个，写一个反而误导；单步也**不露英文工具名** ——
  // 卖家要判断的是"要不要执行"，不是"哪个函数"（system prompt 也要求不暴露内部工具名）。
  if (!multi && !commit) {
    html += '<span class="hint">由 AI 助手发起 · 点确认后才会执行</span>';
  }
  html += "</div></div>";
  return html;
}

async function sendMessage() {
  var input = $("chatInput");
  var text = ((input && input.value) || "").trim();
  if (!text || CHAT.busy) { return; }
  input.value = "";
  CHAT.messages.push({ role: "seller", content: text });
  CHAT.busy = true;
  renderChat();

  var r = await api("/api/seller/assistant/chat", {
    method: "POST", body: { message: text, context: aiContext() }
  });
  CHAT.busy = false;

  if (!r.ok) {
    CHAT.messages.push({ role: "assistant", content: "没能完成这次请求：" + r.error });
    renderChat();
    toast(r.error, true);
    return;
  }
  CHAT.messages.push({ role: "assistant", content: r.data.reply || "（没有返回内容）" });
  // 待确认列表**以服务端为准重新拉取**，而不是直接用本轮返回值：
  // 接口只返回"本轮新建的卡"，直接赋值会让上一轮还没处理的卡从界面上消失（实测踩到）。
  var p = await api("/api/seller/assistant/actions");
  CHAT.pending = p.ok ? (p.data.items || []) : (r.data.pending || []);
  pruneSkipped();
  renderChat();          // renderChat 里会同步角标
  if ((r.data.trace || []).length) {          // 有工具执行过 → 数据可能已变，刷新界面
    await refreshAll();
    render();
  }
}

async function confirmAction(id) {
  // 一次只处理一张卡（串行，避免并发污染）；但**必须给反馈**——
  // 静默 return 会让人以为"这张卡点不动"（实测踩到：两张卡时只能点其中一张，另一张像坏了）
  if (CHAT.busy) { toast("上一项还在处理中，请等它完成后再点这张", true); return; }
  var key = "solo-" + id;
  CHAT.pendingBusy[key] = "confirm";     // 点下去立刻撤掉这张卡的按钮
  CHAT.busy = true;
  renderChat();
  var r = await api("/api/seller/assistant/actions/" + id + "/confirm", { method: "POST" });
  CHAT.busy = false;
  // 无论成败都解除"已提交"标记：成功时卡会被 loadChat 移除，
  // 失败时卡**还在清单里**，必须恢复可点，否则这张卡就再也点不动了。
  delete CHAT.pendingBusy[key];
  if (!r.ok) {
    toast(r.error, true);
    // 失败也会在对话里留一条说明（例如「服务刚更新过，这一步没有执行」）——
    // 不重拉的话卖家只看得到一句 toast，气泡里什么都没有（实测踩到：卡无声消失）
    await loadChat();
    renderChat();
    return;
  }
  toast(r.data.ok === false ? "执行未成功，请查看说明" : "已执行");
  await loadChat();
  await refreshAll();
  render();
}

async function cancelAction(id) {
  if (CHAT.busy) { return; }
  var key = "solo-" + id;
  CHAT.pendingBusy[key] = "cancel";
  CHAT.busy = true;
  renderChat();
  var r = await api("/api/seller/assistant/actions/" + id + "/cancel", { method: "POST" });
  CHAT.busy = false;
  delete CHAT.pendingBusy[key];
  if (!r.ok) { toast(r.error, true); renderChat(); return; }
  toast("已取消，未做改动");
  await loadChat();
}

/** 一批里「划掉 / 恢复」某一步。**只改那一行 DOM**，不整块重绘 ——
 *  重绘会把卡片滚走、也会丢按钮焦点（同一个坑踩过一次，不再踩）。 */
function toggleSkip(btn) {
  var id = Number(btn.getAttribute("data-id"));
  CHAT.skipped[id] = !CHAT.skipped[id];
  var li = btn.closest(".pstep");
  if (li) { li.classList.toggle("skip", !!CHAT.skipped[id]); }
  btn.textContent = CHAT.skipped[id] ? "恢复" : "跳过";
  updateBatchAction(li && li.closest(".pend"));
}

/** 按"还有几步真的会执行"更新主按钮文案；全被划掉就禁用（否则点了什么都不会发生）。 */
function updateBatchAction(card) {
  if (!card) { return; }
  var steps = card.querySelectorAll(".pstep");
  var skipped = card.querySelectorAll(".pstep.skip").length;
  var btn = card.querySelector('[data-ai="batch-confirm"]');
  if (!btn) { return; }
  var left = steps.length - skipped;
  btn.textContent = "确认执行这 " + left + " 步";
  btn.disabled = left === 0;
  if (left === 0) { btn.textContent = "全部已划掉"; }
}

/** 一次确认执行整批。**这是多任务场景的正确入口** —— 不再逐张点。 */
async function confirmBatch(batchId) {
  if (CHAT.busy) { toast("上一项还在处理中，请等它完成后再点", true); return; }
  var group = null;
  pendingBatches().forEach(function (g) { if (g.id === batchId) { group = g; } });
  if (!group) { toast("这批操作已经处理过了", true); await loadChat(); return; }

  var skipIds = group.items
    .filter(function (p) { return CHAT.skipped[p.id]; })
    .map(function (p) { return p.id; });
  if (skipIds.length === group.items.length) {
    toast("所有步骤都被划掉了，没有可执行的", true);
    return;
  }

  CHAT.pendingBusy[batchId] = "confirm";   // 点下去立刻撤掉整张卡的按钮（含「跳过」）
  CHAT.busy = true;
  renderChat();
  var r = await api(
    "/api/seller/assistant/actions/batch/" + encodeURIComponent(batchId) + "/confirm",
    { method: "POST", body: { skip_ids: skipIds } }
  );
  CHAT.busy = false;
  delete CHAT.pendingBusy[batchId];
  if (!r.ok) {
    toast(r.error, true);
    // 可能已被别的标签页处理过 → 以服务端为准重拉，别留一张点不动的卡
    await loadChat();
    return;
  }
  var d = r.data || {};
  toast(batchResultText(d), (d.fail_count || 0) > 0);
  // 有失败 → 留一个「重试失败项」入口；无失败 → 清掉旧入口
  CHAT.failedBatch = (d.fail_count || 0) > 0 ? { batchId: batchId, count: d.fail_count } : null;
  group.items.forEach(function (p) { delete CHAT.skipped[p.id]; });
  await loadChat();
  await refreshAll();      // 任务中心 / 操作日志 / 商品列表都要跟着变
  render();
}

async function cancelBatch(batchId) {
  if (CHAT.busy) { toast("上一项还在处理中，请稍后再点", true); return; }
  CHAT.pendingBusy[batchId] = "cancel";
  CHAT.busy = true;
  renderChat();
  var r = await api(
    "/api/seller/assistant/actions/batch/" + encodeURIComponent(batchId) + "/cancel",
    { method: "POST" }
  );
  CHAT.busy = false;
  delete CHAT.pendingBusy[batchId];
  if (!r.ok) { toast(r.error, true); await loadChat(); return; }
  toast("已整批取消，未做任何改动");
  CHAT.skipped = {};
  await loadChat();
}

/** 把失败项重置回待确认，让卖家重新点确认（计费项重试仍需再点头）。 */
async function retryFailedBatch(batchId) {
  if (CHAT.busy) { toast("上一项还在处理中，请稍后再点", true); return; }
  CHAT.busy = true;
  renderChat();
  var r = await api(
    "/api/seller/assistant/actions/batch/" + encodeURIComponent(batchId) + "/retry",
    { method: "POST" }
  );
  CHAT.busy = false;
  if (!r.ok) { toast(r.error, true); renderChat(); return; }
  var d = r.data || {};
  toast("已把 " + d.reset + " 步失败项放回待确认，请再确认一次");
  CHAT.failedBatch = null;
  await loadChat();
}

/** 执行结果的汇总话术（成功 / 跳过 / 失败分开说，不合并成一句"已完成"）。 */
function batchResultText(d) {
  var parts = [];
  if (d.ok_count) { parts.push(d.ok_count + " 步成功"); }
  if (d.skipped_count) { parts.push(d.skipped_count + " 步已跳过"); }
  if (d.fail_count) { parts.push(d.fail_count + " 步失败"); }
  return parts.length ? parts.join(" · ") : "已处理";
}

async function toggleCaps() {
  var el = $("caps");
  if (el.classList.contains("on")) { el.classList.remove("on"); return; }
  if (!CHAT.capsLoaded) {
    var r = await api("/api/seller/assistant/capabilities");
    CHAT.caps = r.ok ? (r.data.groups || []) : [];
    CHAT.capsLoaded = true;
  }
  el.innerHTML = CHAT.caps.length
    ? CHAT.caps.map(function (g) {
        return '<div class="cap-g">' + escapeHtml(g.label) + '</div><div class="cap-l">'
          + g.tools.map(function (t) {
              return '<span class="' + (t.destructive ? "d" : "") + '">' + escapeHtml(t.label)
                + (t.destructive ? "（需确认）" : "") + "</span>";
            }).join("")
          + "</div>";
      }).join("")
    : '<div class="hint">暂时读不到能力清单（后端不可达？）</div>';
  el.classList.add("on");
}

async function newChat() {
  if (CHAT.busy) { return; }
  var r = await api("/api/seller/assistant/reset", { method: "POST" });
  if (!r.ok) { toast(r.error, true); return; }
  CHAT.messages = [];
  CHAT.pending = [];
  CHAT.skipped = {};
  CHAT.pendingBusy = {};   // 新对话：旧的"已提交"标记不能留到下一批（id 可能被复用）
  $("fabDot").hidden = true;
  renderChat();
  toast("已开始新对话");
}

function bindDrawer() {
  $("btnCloseDrawer").onclick = function () { toggleDrawer(false); };
  $("btnNewChat").onclick = newChat;
  $("btnCaps").onclick = toggleCaps;
  $("btnSend").onclick = sendMessage;

  // 回形针：在对话里直接把资料递给知识库。
  // 为什么保留在对话里：卖家多数是"边聊边想起还有份规则没传"，
  // 让他切到知识库视图再上传，回来就找不到刚才聊到哪了。
  var attach = $("btnKbUpload");
  var attachInput = $("kbUploadInput");
  if (attach && attachInput) {
    attach.onclick = function () { attachInput.click(); };
    attachInput.addEventListener("change", async function () {
      var file = attachInput.files && attachInput.files[0];
      attachInput.value = "";        // 清空：同一份文件连传两次也要能再触发 change
      if (!file) { return; }
      if (state.knowledge.busy) { toast("上一份还在入库，稍等一下", true); return; }

      CHAT.messages.push({ role: "seller", content: "（上传资料）" + file.name });
      renderChat();
      var out = await uploadKnowledge(file, { render: false });
      if (!out) {
        // toast 一闪就没了，对话里留一条才看得见（具体原因已在 toast/横幅里给出）
        CHAT.messages.push({
          role: "assistant",
          content: "这份资料没能入库。原因见右下角提示；"
            + "常见的是格式不支持、文件超过 10 MB，或图片识别未启用。"
        });
      } else {
        CHAT.messages.push({
          role: "assistant",
          content: "已入库：" + out.filename + "（「" + (out.kb || "默认知识库") + "」· "
            + out.chunks + " 个片段）。\n"
            + "现在可以直接问我这份资料里的事，我会按原文回答并说明出自哪份资料。"
        });
      }
      renderChat();
      render();          // 侧栏「知识库」角标与中栏清单要跟着变
    });
  }

  var input = $("chatInput");
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMessage(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "42px";
    input.style.height = Math.min(120, input.scrollHeight) + "px";
  });

  $("drawer").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-ai]");
    if (!btn) { return; }
    var act = btn.getAttribute("data-ai");
    if (act === "skip") { toggleSkip(btn); return; }
    if (act === "batch-confirm") { confirmBatch(btn.getAttribute("data-bid")); return; }
    if (act === "batch-cancel") { cancelBatch(btn.getAttribute("data-bid")); return; }
    if (act === "batch-retry") { retryFailedBatch(btn.getAttribute("data-bid")); return; }
    var id = Number(btn.getAttribute("data-id"));
    if (act === "confirm") { confirmAction(id); }
    else { cancelAction(id); }
  });

  // 浮动图标：可拖动；**未拖动的单击**才展开抽屉（否则拖完手一松就弹出来）
  var fab = $("fab");
  var dragging = false, moved = false, ox = 0, oy = 0, startX = 0, startY = 0;
  fab.addEventListener("pointerdown", function (e) {
    dragging = true; moved = false;
    startX = e.clientX; startY = e.clientY;
    ox = e.clientX - fab.getBoundingClientRect().left;
    oy = e.clientY - fab.getBoundingClientRect().top;
    try { fab.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  });
  fab.addEventListener("pointermove", function (e) {
    if (!dragging) { return; }
    // 点击时的细小指针抖动不是拖动；达到阈值后才改变按钮位置。
    if (!moved && Math.hypot(e.clientX - startX, e.clientY - startY) < 6) { return; }
    moved = true;
    var x = Math.max(8, Math.min(window.innerWidth - fab.offsetWidth - 8, e.clientX - ox));
    var y = Math.max(8, Math.min(window.innerHeight - fab.offsetHeight - 8, e.clientY - oy));
    fab.style.left = x + "px"; fab.style.top = y + "px";
    fab.style.right = "auto"; fab.style.bottom = "auto";
    if (CHAT.open) { positionDrawer(); }   // 浮窗跟着按钮走（实时）
  });
  fab.addEventListener("pointerup", function (e) {
    dragging = false;
    try { fab.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    if (!moved) { toggleDrawer(); }   // 切换：开着就关、关着就开（与 × 按钮等价）
  });

  // 视口变化时重新摆位，避免浮窗被挤出屏幕
  window.addEventListener("resize", function () {
    if (CHAT.open) { positionDrawer(); }
  });
}

/* ---------- 跨模块跳转（商品 ↔ 售后） ---------- */

/** 听指挥台的求助：它被内嵌在 iframe 里，切换不了外层模块，只能请外层代办。 */
function bindHostMessages() {
  window.addEventListener("message", function (e) {
    var data = e.data || {};
    if (data.type !== "seller-open-goods" || !data.goods_no) { return; }
    openGoodsByNo(String(data.goods_no));
  });
}

/** 按商品编号切到「商品管理」并选中它。 */
async function openGoodsByNo(goodsNo) {
  state.view = "admin";
  state.sel = null;
  state.log = null;
  state._aiCopy = null;
  state._aiPrice = null;
  await refreshAll();
  render();
  var target = state.goods.filter(function (g) { return g.goods_no === goodsNo; })[0];
  if (target) {
    await selectGoods(target.id);
    toast("已定位到商品 " + goodsNo);
  } else {
    toast("未在当前商品列表中找到 " + goodsNo, true);
  }
}

/** 反向：从商品跳去售后。
 *
 *  ⚠️ 只切模块 + 提示，**不去 iframe 里选中具体工单** —— 跨文档操作内嵌页很脆，
 *  收益也不大（收件箱本来就能一眼看到）。这是刻意的边界，不是没做完。
 */
function openCaseInAftersale(caseId) {
  state.view = "aftersale";
  state.sel = null;
  state.log = null;
  render();
  toast("已切到售后：请在收件箱中选择 " + caseId);
}

/* ---------- 渲染入口 ---------- */

function render() {
  renderSide();
  // 未登录就没有数据可渲染 —— 直接给占位，不要显示"加载中"（那会永远转下去）
  if (AUTH.seller) { renderMid(); } else { renderLoggedOutMid(); }
  renderRight();
  updateAiCtx();          // 抽屉里的"当前上下文"跟着界面走
  updateCrumb();          // 顶栏面包屑跟着模块走
}

/** 未登录时中栏的占位。
 *
 *  ⚠️ 不能沿用各视图的"空态/加载中"：那些文案的前提是"数据正在来"，
 *     而未登录时数据**永远不会来** —— 卖家会以为界面卡住了。
 */
function renderLoggedOutMid() {
  $("mid").innerHTML =
    '<div class="hd"><h2>请先登录</h2><span class="sub">登录后这里会显示你的店铺数据</span></div>'
    + '<div class="body"><div class="panel"><div class="pb" '
    + 'style="padding:44px 20px;text-align:center;color:var(--muted);font-size:13px;line-height:1.8">'
    + "当前未登录。<br />在登录窗口输入账号密码后即可进入控制台。</div></div></div>";
  _midScroll.view = "";
  _midScroll.top = 0;
}

/** 顶栏面包屑：显示当前模块名（与左栏高亮一致）。 */
function updateCrumb() {
  var el = $("crumb");
  if (el) { el.textContent = VIEW_LABELS[state.view] || state.view || "概览"; }
}

/* ---------- 事件委托 ---------- */

function bindGlobal() {
  $("side").addEventListener("click", function (e) {
    if (e.target.closest("#btnLogout")) { doLogout(); return; }
    var a = e.target.closest("a[data-view]");
    if (!a) { return; }
    state.view = a.getAttribute("data-view");
    state.sel = null; state.log = null; state.checked = {}; state.arrange = false;
    state._galleryCat = ""; state._galleryQ = "";   // 进「商品陈列」默认看全部
    state._adminQ = "";                             // 管理页搜索同理：别让人回到一个忘了的筛选态
    clearTimeout(state._searchTm);                  // 别让上一页的搜索防抖在新页面里触发重绘
    document.body.classList.remove("arrange");
    // **先给反馈再去取数**：数据没回来之前不重绘中栏（否则会闪一下"暂无商品"，
    // 那是假信息），但左栏高亮与面包屑是纯本地计算 —— 立刻变，用户就知道点中了。
    renderSide();
    updateCrumb();
    refreshAll().then(render);
  });

  $("mid").addEventListener("dblclick", function (e) {
    if (state.view === "orders") { handleCommerceMidDoubleClick(e, state.view); }
  });

  $("mid").addEventListener("click", function (e) {
    if (state.view === "orders" || state.view === "service") {
      if (handleCommerceMidClick(e, state.view)) { return; }
    }
    var act = e.target.closest("[data-act]");
    if (act) {
      var name = act.getAttribute("data-act");
      if (name === "goto-audit") { state.view = "audit"; state.sel = null; state.log = null; renderSide(); updateCrumb(); refreshAll().then(render); return; }
      if (name === "new-goods") { newGoods(); return; }
      // 陈列页分类胶囊：换的是**这一屏显示的商品**，回到顶部（原滚动位置对新列表没意义）
      if (name === "gallery-cat") {
        state._galleryCat = act.getAttribute("data-cat") || "";
        state.sel = null;
        renderMid({ reset: true });
        return;
      }
      if (name === "toggle-filter") {
        state._filterOpen = !state._filterOpen;
        renderMid();
        return;
      }
      if (name === "reset-filter") {
        state._filter = {};
        renderMid({ reset: true });
        return;
      }
      if (name === "clear-filter") {
        var fkey = act.getAttribute("data-key");
        if (fkey === "cat") { state._filter.cat = ""; }
        if (fkey === "price") { state._filter.pmin = ""; state._filter.pmax = ""; }
        if (fkey === "stock") { state._filter.smin = ""; state._filter.smax = ""; }
        if (fkey === "date") { state._filter.dmin = ""; state._filter.dmax = ""; }
        renderMid({ reset: true });
        return;
      }
      if (name === "sort") {
        var sk = act.getAttribute("data-key");
        if (state._sort.key === sk) { state._sort.dir = -state._sort.dir; }
        else { state._sort = { key: sk, dir: 1 }; }
        renderMid({ reset: true });   // 排序后行序全变，回到顶部
        return;
      }
      if (name === "restore-goods") { restoreGoods(Number(act.getAttribute("data-id"))); return; }
      if (name === "purge-goods") { purgeGoods(Number(act.getAttribute("data-id"))); return; }
      if (name === "retry-task") { retryTask(Number(act.getAttribute("data-id"))); return; }
      if (name === "task-refresh") { refreshAll().then(render); toast("已刷新"); return; }
      // 子功能栏切换（tab 在中栏里，所以必须在**中栏**的委托里处理）
      if (name === "admin-tab") {
        state._adminTab = act.getAttribute("data-tab") || "all";
        state.checked = {};          // 切视图清空勾选，避免"看不见的行仍被批量操作"
        renderMid({ reset: true });  // 子功能栏 = 另一批数据，回到顶部
        return;
      }
      if (name === "status") {
        var row = act.closest("tr");
        var g = findGoods(row && row.getAttribute("data-goods"));
        if (g) { setStatus(g, act.getAttribute("data-status")); }
        return;
      }
      if (name === "del") {
        var row2 = act.closest("tr");
        var g2 = findGoods(row2 && row2.getAttribute("data-goods"));
        if (g2) { confirmDeleteGoods(g2); }
        return;
      }
      if (name === "bulk") { bulkAction(act.getAttribute("data-mode")); return; }
      if (name === "bulk-price") { bulkAction("price_delta"); return; }
      if (name === "undo") { doUndo(Number(act.getAttribute("data-log"))); return; }
    }

    // ---- 知识库（data-kb 与 data-act 互斥，单独一组动作名）----
    var kbEl = e.target.closest("[data-kb]");
    if (kbEl) {
      var kbAct = kbEl.getAttribute("data-kb");
      if (kbAct === "pick") { pickKnowledgeFile(); return; }
      if (kbAct === "search") { searchKnowledge(); return; }
      if (kbAct === "del") { deleteKnowledgeDoc(kbEl.getAttribute("data-id")); return; }
      if (kbAct === "move") { beginMoveKnowledgeDoc(kbEl.getAttribute("data-id")); return; }
      if (kbAct === "moveSave") { saveKnowledgeDocKb(kbEl.getAttribute("data-id")); return; }
      if (kbAct === "moveCancel") { cancelMoveKnowledgeDoc(); return; }
      return;
    }

    var todo = e.target.closest("[data-goods]");
    if (todo && todo.classList.contains("todo")) {
      var gid = Number(todo.getAttribute("data-goods"));
      state.view = "admin";
      state.sel = null;
      refreshAll().then(function () { render(); selectGoods(gid); });
      return;
    }

    var card = e.target.closest(".gcard");
    if (card) { selectGoods(Number(card.getAttribute("data-goods"))); return; }

    // 勾选框（批量选择）不算"查看详情"：打个勾不该顺带打开右栏、多发一次请求
    var row3 = e.target.closest("tr[data-goods]");
    if (row3 && !e.target.closest(".ckRow")) { selectGoods(Number(row3.getAttribute("data-goods"))); return; }

    var logRow = e.target.closest("tr[data-log]");
    if (logRow) { selectLog(Number(logRow.getAttribute("data-log"))); return; }
  });

  $("mid").addEventListener("change", function (e) {
    if (state.view === "orders" && e.target && e.target.id === "orderCsv") {
      importOrderCsv(e.target.files && e.target.files[0]);
      e.target.value = "";
      return;
    }
    if (e.target.id === "arrangeToggle") {
      state.arrange = e.target.checked;
      document.body.classList.toggle("arrange", state.arrange);
      renderMid();
      return;
    }
    if (e.target.id === "ckAll") {
      var on = e.target.checked;
      state.goods.forEach(function (g) { state.checked[g.id] = on; });
      renderMid();
      return;
    }
    if (e.target.classList && e.target.classList.contains("ckRow")) {
      var id = Number(e.target.getAttribute("data-id"));
      state.checked[id] = e.target.checked;
      renderMid();
      return;
    }
    if (e.target.id === "adminStatus") { state._status = e.target.value; renderMid({ reset: true }); return; }
    if (e.target.id === "auditActor") { filterLogs(e.target.value); return; }
    // 知识库：文件选择框在中栏里，会被重绘替换 → 靠**委托**监听，避免漏绑（表现为"点了没反应"）
    if (e.target.id === "kbFile") {
      var kbFile = e.target.files && e.target.files[0];
      e.target.value = "";          // 清空 value：同一份文件连传两次也要能触发 change
      uploadKnowledge(kbFile);
      return;
    }
    // 文档清单按库筛选：换的是另一批数据 → 回到顶部（不是"重绘"）
    if (e.target.id === "kbFilter") {
      state.knowledge.kbFilter = e.target.value || "";
      state.knowledge.moving = "";
      loadKnowledge().then(function () { renderMid({ reset: true }); });
      return;
    }
    // 检索范围只是下次检索的参数，不必立刻重绘（重绘会让输入框失焦）
    if (e.target.id === "kbScope") { state.knowledge.searchKb = e.target.value || ""; return; }
  });

  $("mid").addEventListener("keydown", function (e) {
    // 知识库检索框：回车即搜（与助手输入框 Ctrl+Enter 发送的习惯不冲突）
    if (e.key === "Enter" && e.target && e.target.id === "kbQ") {
      e.preventDefault();
      searchKnowledge();
      return;
    }
    // 改归属的输入框：回车保存、Esc 取消（不点按钮也能收尾，少一次鼠标）
    if (e.target && e.target.id === "kbMoveTo") {
      if (e.key === "Enter") {
        e.preventDefault();
        saveKnowledgeDocKb(state.knowledge.moving);
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelMoveKnowledgeDoc();
      }
    }
  });

  $("mid").addEventListener("input", function (e) {
    // ⚠️ **输入法组字期间什么都不做** —— 中文/日文输入法在"还没上屏"时会连续触发 input
    // （拼音字母每个都触发一次）。此时重绘中栏会把输入框销毁重建，组合被打断，
    // 拼音永远上不了屏（现象：中文根本打不进去）。等 compositionend 再用最终文本过滤。
    if (e.isComposing || state._composing) { return; }
    // 筛选面板（类目 / 价格区间 / 库存区间）、陈列搜索、管理搜索：统一分发
    if (e.target.id && (e.target.id.indexOf("flt") === 0
        || e.target.id === "gallerySearch" || e.target.id === "adminSearch")) {
      applySearchLike(e.target);
      return;
    }
    // 知识库检索词实时记进 state：万一本栏被重绘，输入内容还在
    if (e.target.id === "kbQ") { state.knowledge.q = e.target.value; }
    // 上传卡的两个输入同理：入库后要重绘清单，不能把刚填的库名/标签冲掉
    if (e.target.id === "kbTarget") { state.knowledge.uploadKb = e.target.value; }
    if (e.target.id === "kbTags") { state.knowledge.tags = e.target.value; }
  });

  // 组字开始 → 立标记；组字结束 → 用**最终文本**过滤一次。
  // 浏览器在 compositionend 之后还会补一个 input（isComposing=false），
  // 两条路都会走到 applySearchLike；`applyGallerySearch`/`applyAdminSearch` 里判等后
  // 第二次是空操作，不会重复重绘。
  $("mid").addEventListener("compositionstart", function () {
    state._composing = true;
  });
  $("mid").addEventListener("compositionend", function (e) {
    state._composing = false;
    applySearchLike(e.target);
  });

  $("right").addEventListener("click", function (e) {
    if ((state.view === "orders" || state.view === "service") && handleCommerceRightClick(e, state.view)) { return; }
    var act = e.target.closest("[data-act]");
    if (!act) { return; }
    var name = act.getAttribute("data-act");
    if (name === "collapse-right") { state.rightOpen = false; persistRight(); render(); return; }
    if (name === "clear-sel") { clearSel(); return; }
    if (name === "del-media") { deleteMedia(Number(act.getAttribute("data-media"))); return; }
    if (name === "ai-copy") { aiCopy(); return; }
    if (name === "copy-apply") { applyCopy(); return; }
    if (name === "copy-drop") { state._aiCopy = null; renderRight(); return; }
    if (name === "ai-price") { aiPrice(); return; }
    if (name === "ai-cover") { aiCover(); return; }
    if (name === "price-apply") { applyPrice(); return; }
    if (name === "price-drop") { state._aiPrice = null; renderRight(); return; }
    if (name === "open-case") {
      openCaseInAftersale(act.getAttribute("data-case") || "");
      return;
    }
  });

  // 文案语气：改了就先记进 state —— 即便中途发生一次重绘（例如点了「AI 定价」），
  // 也不会把卖家的选择悄悄丢掉。不做重绘，避免干扰正在看的界面。
  $("right").addEventListener("change", function (e) {
    if (state.view === "service" && e.target && e.target.id === "serviceReply") {
      var replyId = Number(e.target.value || 0);
      var reply = commerceState.replies.filter(function (r) { return r.id === replyId; })[0];
      var box = $("serviceText");
      if (box && reply) { box.value = reply.content; }
      return;
    }
    if (e.target && e.target.id === "copyStyle") { state._aiStyle = e.target.value; }
  });

  $("dlgCancel").onclick = closeConfirm;
  $("dlgOk").onclick = function () { if (state.onOk) { state.onOk(); } else { closeConfirm(); } };
  $("mask").onclick = function (e) { if (e.target.id === "mask") { closeConfirm(); } };
  $("btnRefresh").onclick = function () { refreshAll().then(render); toast("已刷新"); };
  $("btnShowRight").onclick = function () { state.rightOpen = true; persistRight(); render(); };
  bindDrawer();
  bindHostMessages();
}

function findGoods(id) {
  if (!id) { return null; }
  var gid = Number(id);
  return state.goods.filter(function (g) { return g.id === gid; })[0] || null;
}

function selectLog(logId) {
  state.log = state.logs.filter(function (l) { return l.id === logId; })[0] || null;
  render();
}

function filterLogs(actor) {
  var r = api("/api/seller/logs?limit=100" + (actor ? "&actor=" + actor : ""));
  r.then(function (res) {
    if (res.ok) { state.logs = res.data.items || []; renderMid({ reset: true }); }
  });
}

/* ---------- 陈列拖拽排序 ---------- */

function bindGalleryDrag() {
  var grid = $("galleryGrid");
  if (!grid) { return; }
  var dragEl = null;

  grid.addEventListener("dragstart", function (e) {
    if (!state.arrange) { e.preventDefault(); return; }
    // ⚠️ 有筛选/搜索时**不许拖**：重排提交的是"可见的这些 id 的顺序"，
    //    被隐藏的商品不会出现在提交里，后端只改这些 id 的 sort_no —— 顺序会被打乱。
    if (state._galleryCat || state._galleryQ) {
      e.preventDefault();
      toast("筛选状态下不能拖拽排序，请先点「全部」并清空搜索", true);
      return;
    }
    dragEl = e.target.closest(".gcard");
    if (dragEl) { dragEl.classList.add("dragging"); }
  });
  grid.addEventListener("dragend", function () {
    if (!dragEl) { return; }
    dragEl.classList.remove("dragging");
    dragEl = null;
    var ids = Array.prototype.slice.call(grid.querySelectorAll(".gcard"))
      .map(function (c) { return Number(c.getAttribute("data-goods")); });
    reorder(ids);
  });
  grid.addEventListener("dragover", function (e) {
    if (!dragEl) { return; }
    e.preventDefault();
    var over = e.target.closest(".gcard");
    if (!over || over === dragEl) { return; }
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".gcard"));
    if (cards.indexOf(dragEl) < cards.indexOf(over)) { grid.insertBefore(dragEl, over.nextSibling); }
    else { grid.insertBefore(dragEl, over); }
    Array.prototype.slice.call(grid.querySelectorAll(".ordnum")).forEach(function (n, i) { n.textContent = i + 1; });
  });
}

/* ---------- 启动 ---------- */

async function init() {
  // 版本号上屏：不用开 DevTools 就能确认浏览器跑的是不是最新代码
  var vc = $("verChip");
  if (vc) { vc.textContent = "v" + FRONTEND_VERSION; }
  // 直达参数：?view=... 指定初始视图；?sel=<商品id> 直接选中某件商品；?ai=1 展开助手抽屉
  var q = new URLSearchParams(location.search);
  var v = q.get("view");
  if (v) { state.view = v; }
  if (q.get("filter") === "1") { state._filterOpen = true; }   // 演示/截图用：直接展开筛选面板
  var tab = q.get("tab");
  if (tab) { state._adminTab = tab; }                          // ?tab=trash 直达回收站
  bindGlobal();
  bindAuth();
  // 先确认登录态：没登录就**只开登录浮窗**，不去挨个请求那些必然 401 的接口
  // （否则一进页面就是满屏"登录已失效"提示，看不出真正要做什么）。
  var me = await api("/api/seller/auth/me");
  await loadHealth();                 // 顶栏连接状态与登录无关，照常显示
  if (!me.ok) {
    AUTH.seller = null;
    AUTH.threadId = "";
    render();                         // 左栏显示「未登录」，中栏是空态；登录后原地接管
    return;
  }
  AUTH.seller = me.data.seller;
  AUTH.threadId = me.data.thread_id;
  await refreshAll();
  render();
  var sel = q.get("sel");
  if (sel) { await selectGoods(Number(sel)); }
  if (q.get("ai") === "1") { toggleDrawer(true); }
}

/** 登录表单：回车提交也走这里（`<form>` 的 submit 事件）。 */
function bindAuth() {
  var form = $("authForm");
  if (form) { form.onsubmit = doLogin; }
}

init();
