/* ============================================================
   卖家经营控制台 —— 前端逻辑（免构建）
   ============================================================
   结构：state → api → 视图渲染 → 事件委托 → 数据加载 → init

   两条约定：
   1. **右栏是上下文面板**，内容随「当前模块 + 选中对象」变化（不是固定的编辑商品）；
      概览与售后不显示右栏。
   2. **删除一律经弹窗确认**，且请求带 `confirm=true` —— 后端也强制，
      所以"确认"不是装饰，漏传会被后端 400。
   ============================================================ */

var DEFAULT_API_BASE = "http://127.0.0.1:8000";

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
  _aiCopy: null,        // 文案生成结果（候选，未采纳）
  _aiPrice: null,       // 定价建议结果（候选，未采纳）
  arrange: false,       // 陈列编排模式
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
      return { ok: false, status: res.status, error: String(detail || ("HTTP " + res.status)) };
    }
    return { ok: true, status: res.status, data: data };
  } catch (err) {
    return { ok: false, status: 0, error: "无法连接后端：" + (err && err.message ? err.message : err) };
  }
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
  var items = [
    { grp: "总览" },
    { view: "overview", name: "概览" },
    { grp: "经营" },
    { view: "gallery", name: "商品陈列", n: counts.on_shelf || 0 },
    { view: "admin", name: "商品管理", n: counts.total || 0 },
    { grp: "服务" },
    { view: "aftersale", name: "售后", n: state.aftersaleCount > 0 ? state.aftersaleCount : "" },
    { view: "audit", name: "操作日志", n: (ov.recent_operations || []).length || "" }
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
    html += '<a class="' + on.trim() + '" data-view="' + it.view + '">' + escapeHtml(it.name) + badge + "</a>";
  });
  html += '<div class="who">当前卖家：<b>默认卖家</b><div class="tag">登录接口已预留 · 本轮不做</div></div>';
  $("side").innerHTML = html;
}

/* ---------- 渲染：中栏 ---------- */

function renderMid() {
  var hd, body;
  if (state.view === "overview") { hd = hdOverview(); body = bodyOverview(); }
  else if (state.view === "gallery") { hd = hdGallery(); body = bodyGallery(); }
  else if (state.view === "admin") { hd = hdAdmin(); body = bodyAdmin(); }
  else if (state.view === "audit") { hd = hdAudit(); body = bodyAudit(); }
  else { hd = hdAftersale(); body = bodyAftersale(); }
  $("mid").innerHTML = '<div class="hd">' + hd + '</div><div class="body">' + body + "</div>";
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

function hdGallery() {
  return '<h2>商品陈列</h2><span class="sub">已上架商品 · 对外展示的样子</span><div class="spacer"></div>'
    + '<input class="field" id="gallerySearch" placeholder="搜索商品…" style="width:170px" />'
    + '<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">'
    + '<input type="checkbox" id="arrangeToggle"' + (state.arrange ? " checked" : "") + " /> 编排模式</label>"
    + '<button class="btn primary" data-act="new-goods">＋ 新增商品</button>';
}

function bodyGallery() {
  var html = errBanner();
  if (state.arrange) {
    html += '<div class="hint" style="margin-bottom:12px">编排模式：<b>拖动卡片</b>调整陈列顺序；置顶的商品固定在最前。</div>';
  }
  if (!state.goods.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center" class="hint">'
      + '<div class="hint">还没有已上架的商品。去「商品管理」新建并上架，或直接点右上角「＋ 新增商品」。</div></div></div>';
  }
  html += '<div class="grid" id="galleryGrid">';
  state.goods.forEach(function (g, i) {
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
  var low = g.stock_alert > 0 && g.stock <= g.stock_alert;
  return '<span class="stk' + (low ? " low" : "") + '">库存 ' + g.stock + "</span>";
}

function hdAdmin() {
  return '<h2>商品管理</h2><span class="sub">全部商品 · 与「商品陈列」同一份数据</span><div class="spacer"></div>'
    + '<select class="field" id="adminStatus" style="width:118px">'
    + ["", "on_shelf", "off_shelf", "draft"].map(function (s) {
      return '<option value="' + s + '"' + (state._status === s ? " selected" : "") + ">"
        + (s === "" ? "全部状态" : s === "on_shelf" ? "已上架" : s === "off_shelf" ? "已下架" : "草稿") + "</option>";
    }).join("")
    + "</select>"
    + '<input class="field" id="adminSearch" placeholder="搜索…" style="width:150px" />'
    + '<button class="btn primary" data-act="new-goods">＋ 新增商品</button>';
}

/** 是否低于预警阈值。与后端 `low_stock_only` **同口径**：阈值 0 表示不预警。 */
function isLowStock(g) {
  var alert = Number(g.stock_alert || 0);
  return alert > 0 && Number(g.stock || 0) <= alert;
}

function bodyAdmin() {
  var tab = state._adminTab || "all";
  var lowCount = state.goods.filter(isLowStock).length;
  var rows = state.goods.filter(function (g) {
    if (state._status && g.status !== state._status) { return false; }
    if (tab === "low" && !isLowStock(g)) { return false; }
    return true;
  });
  var checked = Object.keys(state.checked).filter(function (k) { return state.checked[k]; });

  var html = errBanner();
  // 子功能栏：低库存是「商品管理」的一个视图，**不再是左栏的并列项**
  html += '<div class="tabs">'
    + '<button class="tab' + (tab === "all" ? " on" : "") + '" data-act="admin-tab" data-tab="all">'
    + '全部商品<span class="cnt">' + state.goods.length + "</span></button>"
    + '<button class="tab' + (tab === "low" ? " on" : "") + '" data-act="admin-tab" data-tab="low">'
    + '低库存<span class="cnt warn">' + lowCount + "</span></button>"
    + "</div>";
  html += '<div class="bulkbar' + (checked.length ? " on" : "") + '" id="bulkbar">'
    + "已选 <b>" + checked.length + "</b> 项"
    + '<button class="btn sm" data-act="bulk" data-mode="on_shelf">批量上架</button>'
    + '<button class="btn sm" data-act="bulk" data-mode="off_shelf">批量下架</button>'
    + '<button class="btn sm" data-act="bulk-price">批量改价</button>'
    + '<button class="btn sm danger" data-act="bulk" data-mode="delete">批量删除</button>'
    + '<span class="hint" style="margin-left:auto">批量删除需二次确认</span></div>';

  if (!rows.length) {
    return html + '<div class="panel"><div class="pb" style="padding:40px;text-align:center">'
      + '<div class="hint">没有符合条件的商品</div></div></div>';
  }

  html += '<table class="tbl"><thead><tr>'
    + '<th style="width:32px"><input type="checkbox" id="ckAll" /></th>'
    + "<th>编号</th><th>宣传图</th><th>标题</th><th>类目</th><th>价格</th><th>库存</th><th>状态</th>"
    + '<th style="width:190px">操作</th></tr></thead><tbody>';

  rows.forEach(function (g) {
    var thumb = g.cover_url
      ? '<img class="mini" src="' + escapeHtml(resolveUrl(g.cover_url)) + '" alt="" />'
      : '<span class="mini"></span>';
    var low = g.stock_alert > 0 && g.stock <= g.stock_alert;
    html += '<tr data-goods="' + g.id + '"' + (state.sel && state.sel.id === g.id ? ' class="on"' : "") + ">"
      + '<td><input type="checkbox" class="ckRow" data-id="' + g.id + '"'
      + (state.checked[g.id] ? " checked" : "") + " /></td>"
      + "<td>" + escapeHtml(g.goods_no) + "</td><td>" + thumb + "</td>"
      + "<td>" + escapeHtml(g.title) + "</td><td>" + escapeHtml(g.category || "—") + "</td>"
      + '<td class="price" style="font-size:13px">¥' + money(g.price) + "</td>"
      + "<td>" + (low ? '<span class="stk low">' + g.stock + "</span>" : g.stock) + "</td>"
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
  return "<h2>售后</h2><span class=\"sub\">内嵌现有指挥台 · 逻辑不改</span>";
}

function bodyAftersale() {
  return '<div style="height:100%">'
    + '<iframe class="embed-frame" src="console.html?embed=1" title="售后指挥台"></iframe></div>';
}

/* ---------- 渲染：右栏（上下文面板） ---------- */

function renderRight() {
  var pane = $("right");
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
    audit: "从左侧选一条日志，查看这次改了什么"
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
    + '<select class="field" id="copyStyle" style="max-width:104px">'
    + ["口语", "专业", "促销"].map(function (s) {
        return "<option>" + s + "</option>";
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
  state._aiBusy = true;
  toast("正在生成文案…");
  renderRight();
  var styleEl = $("copyStyle");
  var r = await api("/api/seller/goods/" + state.sel.id + "/ai/copy", {
    method: "POST", body: { style: (styleEl && styleEl.value) || "口语" }
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

async function bulkAction(mode, priceDelta) {
  var ids = Object.keys(state.checked).filter(function (k) { return state.checked[k]; }).map(Number);
  if (!ids.length) { toast("未选择任何商品", true); return; }

  var run = async function () {
    var body = { goods_ids: ids, action: mode, confirm: true };
    if (mode === "price_delta") { body.price_delta = priceDelta; }
    var r = await api("/api/seller/goods/bulk", { method: "POST", body: body });
    if (!r.ok) { toast(r.error, true); return; }
    var d = r.data || {};
    toast("成功 " + (d.done || []).length + " 件" + ((d.failed || []).length ? "，失败 " + d.failed.length + " 件" : ""));
    state.checked = {};
    await refreshAll();
    render();
  };

  if (mode === "delete") {
    askConfirm({
      title: "确认批量删除？",
      text: "此操作不可撤销。所选商品及其图片会被一并删除。",
      box: "共 <b>" + ids.length + "</b> 件商品",
      ico: "!"
    }, function () { closeConfirm(); run(); });
    return;
  }
  if (mode === "price_delta") {
    var raw = window.prompt("输入价格调整金额（元，可为负）：", "-10");
    if (raw === null) { return; }
    var delta = Number(raw);
    if (!isFinite(delta)) { toast("金额非法", true); return; }
    await run.call(null);
    return;
  }
  await run();
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
  await loadHealth();
  await loadAftersaleCount();
  if (state.view === "overview") { await loadOverview(); await loadGoods(); }
  else if (state.view === "audit") { await loadLogs(); }
  else if (state.view !== "aftersale") { await loadGoods(); }
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
  aftersale: "售后", audit: "操作日志"
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
  open: false, messages: [], caps: [], capsLoaded: false, pending: [], busy: false
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
  var r = await api("/api/seller/assistant/messages");
  CHAT.messages = r.ok ? (r.data.items || []) : [];
  var p = await api("/api/seller/assistant/actions");
  CHAT.pending = p.ok ? (p.data.items || []) : [];
  renderChat();
}

function renderChat() {
  var box = $("chat");
  if (!box) { return; }
  var html = "";
  if (!CHAT.messages.length && !CHAT.pending.length) {
    html += '<div class="msg bot">我是你的经营助手，可以帮你管商品、排陈列、看售后。\n'
      + '比如：「看看哪些商品快没货了」「把 G-0001 的价格改成 89」「这件商品还有多少库存」。</div>';
  }
  CHAT.messages.forEach(function (m) {
    html += '<div class="msg ' + (m.role === "seller" ? "me" : "bot") + '">'
      + escapeHtml(m.content) + "</div>";
  });
  CHAT.pending.forEach(function (p) { html += pendingCard(p); });
  if (CHAT.busy) { html += '<div class="msg bot busy">正在处理…</div>'; }
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
}

function pendingCard(p) {
  // 确认理由分两类：不可撤销 vs 会产生费用 —— 措辞与配色都不同，卖家的判断依据也不同
  var billable = p.kind === "billable";
  return '<div class="pend' + (billable ? " billable" : "") + '">'
    + '<div class="ttl">' + (billable ? "这一步会产生费用，确认后执行" : "需要你确认后才能执行") + "</div>"
    + escapeHtml(p.summary || "（未命名操作）")
    + (p.detail ? '<div class="det">' + escapeHtml(p.detail) + "</div>" : "")
    + '<div class="acts">'
    + '<button class="btn sm ' + (billable ? "primary" : "danger") + '" data-ai="confirm" data-id="' + p.id + '">'
    + (billable ? "确认并执行" : "确认执行") + "</button>"
    + '<button class="btn sm" data-ai="cancel" data-id="' + p.id + '">取消</button>'
    + '<span class="hint">来自 ' + escapeHtml(p.tool_name || "") + "</span>"
    + "</div></div>";
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
  CHAT.pending = r.data.pending || [];
  renderChat();
  $("fabDot").hidden = CHAT.pending.length === 0;

  if ((r.data.trace || []).length) {          // 有工具执行过 → 数据可能已变，刷新界面
    await refreshAll();
    render();
  }
}

async function confirmAction(id) {
  if (CHAT.busy) { return; }
  CHAT.busy = true;
  renderChat();
  var r = await api("/api/seller/assistant/actions/" + id + "/confirm", { method: "POST" });
  CHAT.busy = false;
  if (!r.ok) { toast(r.error, true); renderChat(); return; }
  toast(r.data.ok === false ? "执行未成功，请查看说明" : "已执行");
  await loadChat();
  await refreshAll();
  render();
}

async function cancelAction(id) {
  if (CHAT.busy) { return; }
  CHAT.busy = true;
  renderChat();
  var r = await api("/api/seller/assistant/actions/" + id + "/cancel", { method: "POST" });
  CHAT.busy = false;
  if (!r.ok) { toast(r.error, true); renderChat(); return; }
  toast("已取消，未做改动");
  await loadChat();
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
  $("fabDot").hidden = true;
  renderChat();
  toast("已开始新对话");
}

function bindDrawer() {
  $("btnCloseDrawer").onclick = function () { toggleDrawer(false); };
  $("btnNewChat").onclick = newChat;
  $("btnCaps").onclick = toggleCaps;
  $("btnSend").onclick = sendMessage;

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
    var id = Number(btn.getAttribute("data-id"));
    if (btn.getAttribute("data-ai") === "confirm") { confirmAction(id); }
    else { cancelAction(id); }
  });

  // 浮动图标：可拖动；**未拖动的单击**才展开抽屉（否则拖完手一松就弹出来）
  var fab = $("fab");
  var dragging = false, moved = false, ox = 0, oy = 0;
  fab.addEventListener("pointerdown", function (e) {
    dragging = true; moved = false;
    ox = e.clientX - fab.getBoundingClientRect().left;
    oy = e.clientY - fab.getBoundingClientRect().top;
    try { fab.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  });
  fab.addEventListener("pointermove", function (e) {
    if (!dragging) { return; }
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
    if (!moved) { toggleDrawer(true); }
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
  renderMid();
  renderRight();
  updateAiCtx();          // 抽屉里的"当前上下文"跟着界面走
  if (state.view === "gallery") { bindGalleryDrag(); }
}

/* ---------- 事件委托 ---------- */

function bindGlobal() {
  $("side").addEventListener("click", function (e) {
    var a = e.target.closest("a[data-view]");
    if (!a) { return; }
    state.view = a.getAttribute("data-view");
    state.sel = null; state.log = null; state.checked = {}; state.arrange = false;
    document.body.classList.remove("arrange");
    refreshAll().then(render);
  });

  $("mid").addEventListener("click", function (e) {
    var act = e.target.closest("[data-act]");
    if (act) {
      var name = act.getAttribute("data-act");
      if (name === "goto-audit") { state.view = "audit"; state.sel = null; state.log = null; refreshAll().then(render); return; }
      if (name === "new-goods") { newGoods(); return; }
      // 子功能栏切换（tab 在中栏里，所以必须在**中栏**的委托里处理）
      if (name === "admin-tab") {
        state._adminTab = act.getAttribute("data-tab") || "all";
        state.checked = {};          // 切视图清空勾选，避免"看不见的行仍被批量操作"
        renderMid();
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

    var row3 = e.target.closest("tr[data-goods]");
    if (row3) { selectGoods(Number(row3.getAttribute("data-goods"))); return; }

    var logRow = e.target.closest("tr[data-log]");
    if (logRow) { selectLog(Number(logRow.getAttribute("data-log"))); return; }
  });

  $("mid").addEventListener("change", function (e) {
    if (e.target.id === "arrangeToggle") {
      state.arrange = e.target.checked;
      document.body.classList.toggle("arrange", state.arrange);
      renderMid();
      bindGalleryDrag();
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
    if (e.target.id === "adminStatus") { state._status = e.target.value; renderMid(); return; }
    if (e.target.id === "auditActor") { filterLogs(e.target.value); return; }
  });

  $("mid").addEventListener("input", function (e) {
    if (e.target.id === "gallerySearch" || e.target.id === "adminSearch") {
      var kw = e.target.value.trim();
      clearTimeout(state._searchTm);
      state._searchTm = setTimeout(function () { localSearch(kw); }, 220);
    }
  });

  $("right").addEventListener("click", function (e) {
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
    if (res.ok) { state.logs = res.data.items || []; renderMid(); }
  });
}

/** 本地搜索（不重新请求，直接过滤已加载列表）。 */
function localSearch(keyword) {
  if (!keyword) { refreshAll().then(render); return; }
  var r = api("/api/seller/goods?scope=" + (state.view === "gallery" ? "shelf" : "all")
    + "&limit=200&q=" + encodeURIComponent(keyword));
  r.then(function (res) {
    if (res.ok) { state.goods = res.data.items || []; renderMid(); }
  });
}

/* ---------- 陈列拖拽排序 ---------- */

function bindGalleryDrag() {
  var grid = $("galleryGrid");
  if (!grid) { return; }
  var dragEl = null;

  grid.addEventListener("dragstart", function (e) {
    if (!state.arrange) { e.preventDefault(); return; }
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
  // 直达参数：?view=... 指定初始视图；?sel=<商品id> 直接选中某件商品；?ai=1 展开助手抽屉
  var q = new URLSearchParams(location.search);
  var v = q.get("view");
  if (v) { state.view = v; }
  bindGlobal();
  await refreshAll();
  render();
  var sel = q.get("sel");
  if (sel) { await selectGoods(Number(sel)); }
  if (q.get("ai") === "1") { toggleDrawer(true); }
}

init();
