/* 订单与客服视图。只管理本地快照与演示咨询，不向外部平台写订单。 */

var commerceState = {
  orders: [], order: null, orderQuery: "", orderError: "",
  conversations: [], conversation: null, replies: [], serviceError: ""
};

function commerceError(result) {
  return result.error || "请求失败";
}

async function loadCommerce(view) {
  if (view === "orders") {
    var q = commerceState.orderQuery ? "?query=" + encodeURIComponent(commerceState.orderQuery) : "";
    var r = await api("/api/seller/orders" + q);
    commerceState.orderError = r.ok ? "" : commerceError(r);
    commerceState.orders = r.ok ? (r.data.items || []) : [];
  } else {
    var parts = await Promise.all([
      api("/api/seller/service/conversations"),
      api("/api/seller/service/quick-replies")
    ]);
    commerceState.serviceError = parts[0].ok ? "" : commerceError(parts[0]);
    commerceState.conversations = parts[0].ok ? (parts[0].data.items || []) : [];
    commerceState.replies = parts[1].ok ? (parts[1].data.items || []) : [];
  }
}

function commerceHeading(view) {
  if (view === "orders") {
    return '<h2>订单</h2><span class="sub">本地订单快照 · 不会修改外部平台</span>';
  }
  return '<h2>客服</h2><span class="sub">本地演示咨询 · 人工接待与快捷回复</span>';
}

function commerceBody(view) {
  return view === "orders" ? orderBody() : serviceBody();
}

/** 终态订单采用文字和颜色双重提示，避免只靠颜色辨认。 */
function orderStatusTag(status) {
  var kinds = { "已完成": "done", completed: "done", "已退款": "refunded", refunded: "refunded",
    "已取消": "cancelled", cancelled: "cancelled", canceled: "cancelled" };
  var kind = Object.prototype.hasOwnProperty.call(kinds, status) ? kinds[status] : "normal";
  return '<strong class="commerce-status ' + kind + '">' + escapeHtml(status) + '</strong>';
}

function orderBody() {
  var html = '<div class="commerce-note">演示和导入订单会明确标记来源。导入只保存快照，不会在原平台发货、退款或改价。</div>';
  if (commerceState.orderError) { html += '<div class="commerce-error">' + escapeHtml(commerceState.orderError) + '</div>'; }
  html += '<div class="commerce-tools"><input id="orderSearch" placeholder="按订单号搜索" value="'
    + escapeHtml(commerceState.orderQuery) + '"><button class="btn" data-act="search-orders">搜索</button>'
    + '<label class="btn primary">导入 CSV<input id="orderCsv" type="file" accept=".csv,text/csv" hidden></label>'
    + '<button class="btn" data-act="create-demo-order">生成演示订单</button>'
    + '<a class="btn" href="orders_import_template.csv" download>下载模板</a></div>';
  if (!commerceState.orders.length) {
    return html + '<div class="commerce-empty">暂无订单。可先导入 CSV；订单表尚未迁移时会显示明确错误。</div>';
  }
  html += '<div class="commerce-note">双击订单查看商品、金额、原始导入信息和时间线；键盘选中后按 Enter 也可打开。</div>';
  html += '<div class="commerce-list">';
  commerceState.orders.forEach(function (o) {
    html += '<button class="commerce-row" data-act="select-order" data-id="' + Number(o.id)
      + '" title="双击查看详情" aria-label="双击查看订单 ' + escapeHtml(o.external_order_id) + ' 的详情">'
      + '<span><b>' + escapeHtml(o.external_order_id) + '</b>' + orderStatusTag(o.status)
      + '<small>' + escapeHtml(o.placed_at || o.imported_at || '') + '</small>'
      + '<small class="commerce-tracking-hint">' + (o.has_tracking ? '有物流' : '暂无运单') + '</small></span>'
      + '<span><em>' + (o.source === "demo" ? "演示" : "导入") + '</em><b>¥' + (Number(o.order_amount_cent ?? o.paid_cent ?? 0) / 100).toFixed(2) + '</b><small>双击查看详情 ›</small></span></button>';
  });
  return html + '</div>';
}

function serviceBody() {
  var html = '<div class="commerce-note">咨询进线目前仅来自本地演示入口。人工接管后由客服发言；不会自动向买家发送 AI 回复。</div>';
  if (commerceState.serviceError) { html += '<div class="commerce-error">' + escapeHtml(commerceState.serviceError) + '</div>'; }
  var sellerId = AUTH.seller ? AUTH.seller.seller_id : "default";
  html += '<div class="commerce-tools"><a class="btn" href="consultation.html?seller_id='
    + encodeURIComponent(sellerId) + '" target="_blank" rel="noopener">打开买家咨询页</a>'
    + '<button class="btn" data-act="refresh-service">刷新收件箱</button></div>';
  if (!commerceState.conversations.length) {
    return html + '<div class="commerce-empty">暂无咨询。可打开买家咨询页发起一段演示会话。</div>';
  }
  html += '<div class="commerce-list">';
  commerceState.conversations.forEach(function (c) {
    html += '<button class="commerce-row" data-act="select-service" data-id="' + escapeHtml(c.public_id) + '">'
      + '<span><b>咨询 ' + escapeHtml(c.public_id.slice(0, 8)) + '</b><small>'
      + escapeHtml(c.last_message || "暂无消息") + '</small></span><em>'
      + (c.status === "open" ? "进行中" : "已结束") + '</em></button>';
  });
  return html + '</div>';
}

function renderCommerceRight(pane, view) {
  var item = view === "orders" ? commerceState.order : commerceState.conversation;
  if (!item || !state.rightOpen) {
    pane.style.display = "none";
    updateRightToggle(!!item && !state.rightOpen);
    return;
  }
  pane.style.display = "flex";
  pane.setAttribute("data-mode", view);
  updateRightToggle(false);
  pane.innerHTML = view === "orders" ? orderDetail(item) : serviceDetail(item);
}

function orderDetail(o) {
  var html = '<div class="hd"><h3>订单详情</h3><button class="btn sm" data-act="collapse-right">收起详情</button></div><div class="commerce-detail">'
    + '<div class="commerce-note">' + (o.source === "demo" ? "演示订单" : "商家导入订单") + ' · 仅本地记录</div>'
    + '<h3>' + escapeHtml(o.external_order_id) + '</h3>'
    + '<p>状态：' + orderStatusTag(o.status) + '</p><p>订单金额：¥' + (Number(o.order_amount_cent ?? o.paid_cent ?? 0) / 100).toFixed(2) + '</p>'
    + '<p>当前已收：¥' + (Number(o.paid_cent || 0) / 100).toFixed(2) + '</p>'
    + '<h4>订单商品</h4>';
  (o.lines || []).forEach(function (line) {
    html += '<div class="commerce-line"><b>' + escapeHtml(line.title) + '</b><span>'
      + escapeHtml(line.goods_no || line.sku_id || line.line_id) + ' × ' + Number(line.quantity)
      + ' · 单价 ¥' + (Number(line.unit_price_cent || 0) / 100).toFixed(2)
      + ' · 已收 ¥' + (Number(line.paid_cent || 0) / 100).toFixed(2) + '</span></div>';
  });
  html += orderLogistics(o);
  (o.import_payload || []).forEach(function (sourceRow) {
    html += '<h4>导入原始信息</h4>';
    Object.keys(sourceRow).forEach(function (key) {
      if (sourceRow[key]) {
        html += '<p>' + escapeHtml(key) + '：' + escapeHtml(sourceRow[key]) + '</p>';
      }
    });
  });
  html += '<h4>时间线</h4>';
  (o.events || []).forEach(function (ev) {
    html += '<div class="commerce-line"><b>' + escapeHtml(ev.detail) + '</b><span>'
      + escapeHtml(ev.created_at || '') + (ev.simulated ? ' · 模拟' : '') + '</span></div>';
  });
  return html + '</div>';
}

/** 物流字段来自导入快照；状态只用于阶段提示，不伪造真实扫描轨迹。 */
function orderLogistics(order) {
  var source = (order.import_payload || [])[0] || {};
  var company = source["物流公司"] || "";
  var number = source["物流单号"] || "";
  var html = '<section class="commerce-logistics"><h4>物流追踪</h4>';
  if (!company || !number) {
    return html + '<p>尚无运单信息</p><small>当前订单快照未提供物流公司和运单号。</small></section>';
  }
  var stages = { "已发货": "已发货", "运输中": "运输中", "已签收": "已签收", "已完成": "订单已完成" };
  var stage = Object.prototype.hasOwnProperty.call(stages, order.status)
    ? stages[order.status] : "物流阶段未知";
  html += '<p><b>' + escapeHtml(company) + '</b> · 运单号 ' + escapeHtml(number) + '</p>'
    + '<p>当前阶段：' + escapeHtml(stage) + '</p>'
    + '<p>订单下单时间：' + escapeHtml(order.placed_at || "未提供") + '</p>'
    + '<p>快照导入时间：' + escapeHtml(order.imported_at || "未提供") + '</p>'
    + '<small>根据导入订单状态展示，非实时轨迹；无揽收、运输节点及签收时间数据。</small>';
  return html + '</section>';
}

function serviceDetail(c) {
  var html = '<div class="hd"><h3>咨询详情</h3></div><div class="commerce-detail">'
    + '<div class="commerce-note">' + (c.status === "open" ? "进行中" : "已结束")
    + ' · 当前接待：' + (c.assignee === "manual" ? "人工" : "AI") + '</div>'
    + '<div class="commerce-messages">';
  (c.messages || []).forEach(function (m) {
    html += '<div class="commerce-message ' + escapeHtml(m.role) + '"><b>'
      + (m.role === "buyer" ? "买家" : "客服") + '</b><p>' + escapeHtml(m.content) + '</p></div>';
  });
  html += '</div>';
  if (c.status === "open") {
    html += '<div class="commerce-actions"><button class="btn" data-act="takeover-service">人工接管</button>'
      + '<button class="btn" data-act="close-service">结束咨询</button></div>'
      + '<select id="serviceReply"><option value="">选择快捷回复</option>';
    commerceState.replies.forEach(function (r) {
      html += '<option value="' + Number(r.id) + '">' + escapeHtml(r.title) + '</option>';
    });
    html += '</select><textarea id="serviceText" placeholder="输入发给买家的消息"></textarea>'
      + '<button class="btn primary" data-act="send-service">发送回复</button>';
  }
  html += '<h4>新建快捷回复</h4><input id="replyTitle" placeholder="标题">'
    + '<textarea id="replyContent" placeholder="回复正文"></textarea>'
    + '<button class="btn" data-act="add-reply">保存快捷回复</button></div>';
  return html;
}

async function selectCommerceOrder(id) {
  var r = await api("/api/seller/orders/" + id);
  if (!r.ok) { toast(commerceError(r), true); return; }
  commerceState.order = r.data;
  // 右栏可能被用户在其他模块收起；主动选订单应始终展示详情。
  state.rightOpen = true;
  persistRight();
  renderRight();
}

async function selectCommerceService(id) {
  var r = await api("/api/seller/service/conversations/" + encodeURIComponent(id));
  if (!r.ok) { toast(commerceError(r), true); return; }
  commerceState.conversation = r.data;
  renderRight();
}

function handleCommerceMidClick(event, view) {
  var act = event.target.closest("[data-act]");
  if (!act) { return false; }
  var name = act.getAttribute("data-act");
  if (name === "select-order") {
    // 鼠标单击只选中按钮；键盘 Enter/Space 产生的 click.detail=0 仍可打开。
    if (event.detail === 0) { selectCommerceOrder(Number(act.getAttribute("data-id"))); }
    return true;
  }
  if (name === "create-demo-order") {
    api("/api/seller/orders/demo", { method: "POST" }).then(function (r) {
      if (!r.ok) { toast(commerceError(r), true); return; }
      loadCommerce("orders").then(function () { renderMid(); selectCommerceOrder(r.data.order_id); });
    });
    return true;
  }
  if (name === "select-service") { selectCommerceService(act.getAttribute("data-id")); return true; }
  if (name === "search-orders") {
    commerceState.orderQuery = ($("orderSearch") || {}).value || "";
    loadCommerce("orders").then(function () { renderMid({ reset: true }); });
    return true;
  }
  if (name === "refresh-service") {
    loadCommerce("service").then(function () { renderMid(); });
    return true;
  }
  return false;
}

function handleCommerceMidDoubleClick(event, view) {
  if (view !== "orders") { return; }
  var row = event.target.closest('[data-act="select-order"]');
  if (row) { selectCommerceOrder(Number(row.getAttribute("data-id"))); }
}

async function handleCommerceRightAction(name) {
  var c = commerceState.conversation;
  if (!c) { return; }
  var base = "/api/seller/service/conversations/" + encodeURIComponent(c.public_id);
  var r;
  if (name === "takeover-service") { r = await api(base + "/takeover", { method: "POST" }); }
  else if (name === "close-service") { r = await api(base + "/close", { method: "POST" }); }
  else if (name === "send-service") {
    var text = ($("serviceText") || {}).value || "";
    var replyId = Number(($("serviceReply") || {}).value || 0);
    if (!text.trim()) { toast("请输入回复内容", true); return; }
    r = await api(base + "/messages", { method: "POST", body: { text: text, quick_reply_id: replyId || null } });
  } else if (name === "add-reply") {
    var title = ($("replyTitle") || {}).value || "";
    var content = ($("replyContent") || {}).value || "";
    if (!title.trim() || !content.trim()) { toast("请填写标题和回复正文", true); return; }
    r = await api("/api/seller/service/quick-replies", { method: "POST", body: { title: title, content: content } });
    if (r.ok) { await loadCommerce("service"); }
  }
  if (!r) { return; }
  if (!r.ok) { toast(commerceError(r), true); return; }
  toast("操作已完成");
  await selectCommerceService(c.public_id);
  if (name === "close-service") { await loadCommerce("service"); renderMid(); }
}

function handleCommerceRightClick(event, view) {
  if (view !== "service") { return false; }
  var act = event.target.closest("[data-act]");
  if (!act) { return false; }
  handleCommerceRightAction(act.getAttribute("data-act"));
  return true;
}

function parseCsv(text) {
  var rows = [], row = [], cell = "", quoted = false;
  text = String(text || "").replace(/^\uFEFF/, "");
  for (var i = 0; i < text.length; i++) {
    var ch = text[i];
    if (quoted && ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
    else if (ch === '"') { quoted = !quoted; }
    else if (ch === "," && !quoted) { row.push(cell); cell = ""; }
    else if ((ch === "\n" || ch === "\r") && !quoted) {
      if (ch === "\r" && text[i + 1] === "\n") { i++; }
      row.push(cell); cell = "";
      if (row.some(function (x) { return x !== ""; })) { rows.push(row); }
      row = [];
    } else { cell += ch; }
  }
  if (quoted) { throw new Error("CSV 引号未闭合"); }
  row.push(cell);
  if (row.some(function (x) { return x !== ""; })) { rows.push(row); }
  return rows;
}

function ordersFromCsv(text) {
  var rows = parseCsv(text);
  if (rows.length < 2) { throw new Error("CSV 至少需要表头和一行订单"); }
  var header = rows.shift().map(function (x) { return x.trim(); });
  if (header.indexOf("订单号") >= 0) { return chineseOrdersFromRows(header, rows); }
  var required = ["order_id", "line_id", "title", "quantity", "unit_price_cent", "paid_cent", "status"];
  required.forEach(function (name) { if (header.indexOf(name) < 0) { throw new Error("CSV 缺少列：" + name); } });
  var map = {};
  rows.forEach(function (row, i) {
    if (row.length !== header.length) { throw new Error("第 " + (i + 2) + " 行列数不一致"); }
    var x = {};
    header.forEach(function (h, j) { x[h] = row[j].trim(); });
    if (!x.order_id) { throw new Error("第 " + (i + 2) + " 行缺少订单号"); }
    if (!map[x.order_id]) {
      map[x.order_id] = { external_order_id: x.order_id, status: x.status, paid_cent: 0,
        placed_at: x.placed_at || null, lines: [] };
    }
    var o = map[x.order_id];
    if (o.status !== x.status || (o.placed_at || "") !== (x.placed_at || "")) {
      throw new Error("同一订单的状态或下单时间不一致：" + x.order_id);
    }
    ["quantity", "unit_price_cent", "paid_cent"].forEach(function (key) {
      if (!/^\d+$/.test(x[key] || "")) { throw new Error("第 " + (i + 2) + " 行 " + key + " 必须是非负整数"); }
    });
    o.lines.push({ line_id: x.line_id, goods_no: x.goods_no || "", sku_id: x.sku_id || "",
      title: x.title, quantity: Number(x.quantity), unit_price_cent: Number(x.unit_price_cent), paid_cent: Number(x.paid_cent) });
    o.paid_cent += Number(x.paid_cent);
  });
  var orders = Object.keys(map).map(function (id) { return map[id]; });
  if (orders.length > 100) { throw new Error("一次最多导入 100 笔订单"); }
  return orders;
}

/** 中文电商导出列转成内部订单模型。保留原行，避免地址、物流等信息静默丢失。 */
function chineseOrdersFromRows(header, rows) {
  var required = ["订单号", "下单时间", "商品名称", "数量", "单价(元)", "商品金额(元)",
    "运费(元)", "优惠金额(元)", "实付金额(元)", "订单状态"];
  required.forEach(function (name) {
    if (header.indexOf(name) < 0) { throw new Error("中文 CSV 缺少列：" + name); }
  });
  var knownStatus = ["待付款", "已付款待发货", "已发货", "运输中", "已签收", "已完成", "退款中", "已取消", "已退款"];
  var unpaidStatus = ["待付款", "已取消", "已退款"];
  var seen = {};
  var orders = rows.map(function (row, index) {
    var rowNumber = index + 2;
    if (row.length !== header.length) { throw new Error("第 " + rowNumber + " 行列数不一致"); }
    var source = {};
    header.forEach(function (name, i) { source[name] = row[i].trim(); });
    if (!source["订单号"] || !source["商品名称"]) { throw new Error("第 " + rowNumber + " 行订单号或商品名称为空"); }
    if (seen[source["订单号"]]) { throw new Error("第 " + rowNumber + " 行订单号重复；中文格式每行应为一笔订单"); }
    seen[source["订单号"]] = true;
    if (knownStatus.indexOf(source["订单状态"]) < 0) { throw new Error("第 " + rowNumber + " 行订单状态无法识别"); }
    var quantity = Number(source["数量"]);
    if (!Number.isSafeInteger(quantity) || quantity < 1) { throw new Error("第 " + rowNumber + " 行数量必须是正整数"); }
    function cents(name) {
      var value = source[name];
      if (!/^\d+(?:\.\d{1,2})?$/.test(value)) { throw new Error("第 " + rowNumber + " 行 " + name + " 必须是非负金额，最多两位小数"); }
      var parts = value.split(".");
      var result = Number(parts[0]) * 100 + Number((parts[1] || "").padEnd(2, "0"));
      if (!Number.isSafeInteger(result)) { throw new Error("第 " + rowNumber + " 行金额过大"); }
      return result;
    }
    var unit = cents("单价(元)"), goods = cents("商品金额(元)");
    var freight = cents("运费(元)"), discount = cents("优惠金额(元)");
    var amount = cents("实付金额(元)");
    if (unit * quantity !== goods || goods + freight - discount !== amount) {
      throw new Error("第 " + rowNumber + " 行金额不平：商品金额＋运费－优惠应等于订单金额");
    }
    var paid = unpaidStatus.indexOf(source["订单状态"]) >= 0 ? 0 : amount;
    return {
      external_order_id: source["订单号"], status: source["订单状态"],
      placed_at: source["下单时间"] || null, order_amount_cent: amount, paid_cent: paid,
      lines: [{ line_id: "1", goods_no: "", sku_id: "", title: source["商品名称"],
        quantity: quantity, unit_price_cent: unit, paid_cent: paid }],
      import_payload: [source]
    };
  });
  if (orders.length > 100) { throw new Error("一次最多导入 100 笔订单"); }
  return orders;
}

async function importOrderCsv(file) {
  if (!file) { return; }
  if (file.size > 1024 * 1024) { toast("CSV 文件不能超过 1 MB", true); return; }
  var orders;
  try { orders = ordersFromCsv(await file.text()); }
  catch (err) { toast(err.message, true); return; }
  if (orders.some(function (order) { return order.import_payload; })) {
    if (!window.confirm("识别到 " + orders.length + " 笔中文订单。原始字段将保存在本地订单详情；待付款、已取消、已退款的当前已收金额记为 0。确认导入？")) { return; }
  }
  var created = 0, reused = 0, failure = "", lastOrderId = null;
  for (var i = 0; i < orders.length; i++) {
    var r = await api("/api/seller/orders/import", { method: "POST", body: orders[i] });
    if (!r.ok) { failure = "第 " + (i + 1) + " 笔导入失败：" + commerceError(r); break; }
    lastOrderId = r.data.order_id;
    if (r.data.created) { created++; } else { reused++; }
  }
  await loadCommerce("orders");
  renderMid();
  if (lastOrderId) { await selectCommerceOrder(lastOrderId); }
  toast(failure || ("新增 " + created + " 笔，已存在 " + reused + " 笔"), !!failure);
}
