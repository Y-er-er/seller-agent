/* ============================================================
   买家侧对话页（H5）逻辑 —— 免构建 · 零依赖

   设计要点：
   1) **单一出口**：所有后端调用都经 `api()`，便于契约校验与统一错误处理。
   2) **纯函数渲染**：`renderMessages` / `renderOrderCard` / `renderTips` 只吃数据、
      吐 HTML 字符串 —— 可脱离浏览器直接单测（见 verify_h5.cjs）。
   3) **不静默失败**：后端不可达 / 未启用 / 会话不存在，一律显示明确提示，
      绝不让用户面对"点了没反应"。
   4) **护栏前置**：200 字在输入框实时提示（首次只提示，不拦发送——与后端裁决一致）。

   ⚠️ 流式说明：`/messages/stream` 是 SSE，但 P1 的模型调用为**非流式**，
      后端做的是**确定性分段推送**（meta.stream_mode === "segmented"）。
      本页据此渲染"打字效果"，不宣称 token 级流式。
   ============================================================ */

/* ---------- 配置 ---------- */

// 后端地址：file:// 打开时用本机默认端口；也允许通过 ?api= 覆盖
var DEFAULT_API_BASE = "http://127.0.0.1:8000";

var state = {
  apiBase: DEFAULT_API_BASE,
  caseId: "",
  orderId: "",
  messages: [],        // [{role:'buyer'|'agent'|'system', content, seq}]
  status: "",
  round: 0,
  maxRounds: 20,
  gate: null,
  escalated: false,
  ended: false,
  sending: false,
  overLengthWarned: false,   // 首次超长只提示（不计轮次）；记本地标记避免重复唠叨
  attachments: [],           // 本次会话已上传的材料名
  quota: 0,                  // 会话允许的轮数（来自后端 max_rounds）
  lastError: "",
};

var MAX_MSG_CHARS = 200;

/* ---------- 工具 ---------- */

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 统一的后端调用出口。返回 {ok, status, data, error}，**不抛异常**。 */
async function api(path, options) {
  var opt = options || {};
  var url = state.apiBase + path;
  var init = { method: opt.method || "GET", headers: {} };
  if (opt.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opt.body);
  }
  if (opt.form) {
    init.body = opt.form;              // FormData：让浏览器自行设置 multipart 边界
  }
  try {
    var res = await fetch(url, init);
    if (opt.raw) return { ok: res.ok, status: res.status, response: res };
    var data = null;
    var text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
    if (!res.ok) {
      return { ok: false, status: res.status, data: data, error: (data && data.detail) || ("HTTP " + res.status) };
    }
    return { ok: true, status: res.status, data: data, error: "" };
  } catch (err) {
    // 网络层失败（后端没起 / 跨域被拦）：给出可操作的文案，而不是一句"失败"
    return { ok: false, status: 0, data: null, error: "无法连接后端（" + state.apiBase + "）：" + (err && err.message ? err.message : err) };
  }
}

/* ---------- SSE 解析（纯函数，可单测） ---------- */

/**
 * 解析缓冲区里**已完整**的 SSE 帧，返回 {events, rest}。
 * `rest` 是尚未收到空行结尾的残留内容，需与后续数据拼接后再解析。
 */
function parseSSEChunk(buffer) {
  var text = String(buffer == null ? "" : buffer);
  var events = [];
  var idx;
  while ((idx = text.indexOf("\n\n")) !== -1) {
    var block = text.slice(0, idx);
    text = text.slice(idx + 2);
    // 空块（例如正文已以 \n\n 结尾、调用方又补了一个）必须跳过，
    // 否则会凭空多出一个 `message` 事件（曾因此多派发一帧）。
    if (!block.trim()) continue;
    var ev = { event: "message", data: null };
    var lines = block.split("\n");
    var payload = "";
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("event:") === 0) ev.event = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) payload += (payload ? "\n" : "") + line.slice(5).trim();
    }
    if (payload) {
      try { ev.data = JSON.parse(payload); } catch (e) { ev.data = { raw: payload }; }
    }
    events.push(ev);
  }
  return { events: events, rest: text };
}

/** 消费 SSE 响应，逐个回调事件。兼容无 `body.getReader` 的环境（测试桩）。 */
async function consumeSSE(res, onEvent) {
  if (res && res.body && typeof res.body.getReader === "function") {
    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    for (;;) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += decoder.decode(chunk.value, { stream: true });
      var parsed = parseSSEChunk(buf);
      buf = parsed.rest;
      for (var i = 0; i < parsed.events.length; i++) onEvent(parsed.events[i]);
    }
    if (buf.trim()) {
      var tail = parseSSEChunk(buf + "\n\n");
      for (var j = 0; j < tail.events.length; j++) onEvent(tail.events[j]);
    }
    return;
  }
  var body = await res.text();
  var all = parseSSEChunk(body + "\n\n");
  for (var k = 0; k < all.events.length; k++) onEvent(all.events[k]);
}

/* ---------- 渲染（纯函数） ---------- */

function renderOrderCard(order) {
  if (!order) return "";
  var rows = [
    ["订单号", order.order_id],
    ["商品", order.item_title || order.title],
    ["实付", order.pay_amount != null ? ("¥" + order.pay_amount) : ""],
    ["状态", order.status_text || order.status],
  ].filter(function (r) { return r[1] !== undefined && r[1] !== null && r[1] !== ""; });
  if (!rows.length) return "";
  var body = rows.map(function (r) {
    return '<div class="row"><span class="k">' + escapeHtml(r[0]) +
           '</span><span class="v">' + escapeHtml(r[1]) + "</span></div>";
  }).join("");
  return '<div class="order-card"><div class="title">关联订单</div>' + body + "</div>";
}

function renderAmountCard(amount) {
  if (amount == null || amount === "") return "";
  var label = typeof amount === "object" ? (amount.label || "涉及金额") : "涉及金额";
  var value = typeof amount === "object" ? amount.value : amount;
  return '<div class="amount-card"><div class="label">' + escapeHtml(label) +
         '</div><div class="value">¥' + escapeHtml(value) + "</div></div>";
}

function renderBasis(basis) {
  if (!basis || !basis.length) return "";
  var items = basis.map(function (b) { return "<li>" + escapeHtml(b) + "</li>"; }).join("");
  return '<details class="basis"><summary>判定依据（点击展开）</summary><ol>' + items + "</ol></details>";
}

function renderMessage(msg) {
  var role = msg.role === "buyer" ? "buyer" : (msg.role === "system" ? "system" : "agent");
  var extra = "";
  if (msg.amount != null) extra += renderAmountCard(msg.amount);
  if (msg.basis && msg.basis.length) extra += renderBasis(msg.basis);
  if (msg.quick && msg.quick.length) {
    extra += '<div class="quick">' + msg.quick.map(function (q) {
      return '<button type="button" data-quick="' + escapeHtml(q) + '">' + escapeHtml(q) + "</button>";
    }).join("") + "</div>";
  }
  var cls = "bubble" + (msg.streaming ? " caret" : "");
  return '<div class="msg ' + role + '"><div class="' + cls + '">' +
         escapeHtml(msg.content) + extra + "</div></div>";
}

function renderMessages(messages) {
  if (!messages || !messages.length) return "";
  return messages.map(renderMessage).join("");
}

function renderRoundBar(round, maxRounds) {
  var max = Number(maxRounds) || 20;
  var used = Number(round) || 0;
  var pct = Math.max(0, Math.min(100, Math.round((used / max) * 100)));
  var cls = "round-bar";
  if (used >= max) cls += " is-full";
  else if (used >= max - 3) cls += " is-warn";
  return '<div class="' + cls + '"><i style="width:' + pct + '%"></i></div>';
}

function renderTips(st) {
  var tips = [];
  if (st.lastError) {
    tips.push({ level: "danger", text: st.lastError });
  }
  if (st.overLengthWarned) {
    tips.push({ level: "warn", text: "消息超过 " + MAX_MSG_CHARS + " 字，本次已按超长提示处理；再次超长将转人工客服。" });
  }
  if (st.gate && st.gate.paused) {
    tips.push({ level: "warn", text: st.gate.message || "对话入口已暂停。" });
  }
  if (st.escalated) {
    tips.push({ level: "info", text: "本会话已转人工客服，后续回复以人工为准。" });
  }
  if (st.ended && !st.escalated) {
    tips.push({ level: "info", text: "本会话已结束。如需继续，请重新发起咨询。" });
  }
  if (!tips.length) return "";
  return tips.map(function (t) {
    return '<div class="tip ' + t.level + '">' + escapeHtml(t.text) + "</div>";
  }).join("");
}

function renderComposer(st) {
  var locked = st.sending || st.escalated || st.ended;
  var disabled = locked ? " disabled" : "";
  var hint = st.escalated ? "已转人工，输入已关闭"
           : (st.ended ? "会话已结束" : (st.sending ? "正在发送…" : ""));
  var chips = st.attachments.length
    ? '<div class="toolbar"><span>' + st.attachments.map(function (n) {
        return '<span class="file-chip">📎 ' + escapeHtml(n) + "</span>";
      }).join(" ") + "</span></div>"
    : "";
  return '' +
    '<div class="composer">' +
      chips +
      '<div class="row">' +
        '<textarea id="h5-input" rows="1" placeholder="' + escapeHtml(hint || "描述你遇到的问题…") + '"' + disabled + "></textarea>" +
        '<button class="btn primary" id="h5-send"' + disabled + ">发送</button>" +
      "</div>" +
      '<div class="toolbar">' +
        '<span class="counter" id="h5-counter">0 / ' + MAX_MSG_CHARS + "</span>" +
        '<span>' +
          '<input type="file" id="h5-file" class="hidden" accept=".jpg,.jpeg,.png,.webp,.pdf" />' +
          '<button class="btn ghost" id="h5-upload"' + disabled + ">上传举证</button> " +
          '<button class="btn ghost" id="h5-escalate"' + (st.escalated ? " disabled" : "") + ">转人工</button>" +
        "</span>" +
      "</div>" +
    "</div>";
}

/** 全量渲染：本页状态不多，整块重绘比手工 diff 更不容易出错。 */
function render() {
  var root = document.getElementById("h5");
  if (!root) return "";
  var header = '' +
    '<div class="h5-header">' +
      "<h1>售后助手</h1>" +
      '<div class="sub">' +
        (state.caseId ? ("会话 " + escapeHtml(state.caseId) + " · 第 " + state.round + "/" + state.maxRounds + " 轮")
                      : "请先输入订单号开始咨询") +
      "</div>" +
      renderRoundBar(state.round, state.maxRounds) +
    "</div>";
  var html = header +
    renderOrderCard(state.order) +
    '<div class="messages" id="h5-messages">' + renderMessages(state.messages) + "</div>" +
    '<div class="tips">' + renderTips(state) + "</div>" +
    renderComposer(state);
  root.innerHTML = html;
  return html;
}

/* ---------- 状态与视图辅助 ---------- */

function setState(patch) {
  var key;
  for (key in patch) {
    if (Object.prototype.hasOwnProperty.call(patch, key)) state[key] = patch[key];
  }
  render();
}

function pushMessage(msg) {
  state.messages.push(msg);
  render();
}

function applyServerSnapshot(payload) {
  var s = (payload && payload.session) || {};
  if (s.status) state.status = s.status;
  if (s.round != null) state.round = s.round;
  if (s.status === "ESCALATED" || s.status === "CLOSED") state.escalated = s.status === "ESCALATED";
  if (payload && payload.messages) {
    state.messages = payload.messages.map(function (m) {
      return { role: m.role, content: m.content, seq: m.seq };
    });
  }
  render();
}

/* ---------- 业务动作 ---------- */

/** 创建（或幂等恢复）会话。 */
async function createCase(orderId) {
  if (!orderId) {
    setState({ lastError: "请填写订单号" });
    return false;
  }
  setState({ orderId: orderId, lastError: "" });
  var res = await api("/api/h5/cases", { method: "POST", body: { order_id: orderId } });
  if (!res.ok) {
    // 503 = 对话入口未启用 / 闸门暂停；后端给了具体原因，直接透出
    setState({ lastError: res.status === 503 ? ("对话入口暂不可用：" + res.error) : ("创建会话失败：" + res.error) });
    return false;
  }
  state.caseId = res.data.case_id;
  if (res.data.gate) state.gate = res.data.gate;
  await loadCase();
  return true;
}

async function loadCase() {
  if (!state.caseId) return false;
  var res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}`);
  if (!res.ok) {
    setState({ lastError: "读取会话失败：" + res.error });
    return false;
  }
  applyServerSnapshot(res.data);
  return true;
}

/** 发送消息。`stream=true` 走 SSE 分段推送。 */
async function sendMessage(text, options) {
  var opt = options || {};
  var body = String(text == null ? "" : text).trim();
  if (!body || state.sending || state.escalated || state.ended) return false;

  pushMessage({ role: "buyer", content: body });
  setState({ sending: true, lastError: "" });

  // 本地即时提示超长（与后端裁决一致：首次只提示、再犯转人工）
  if (body.length > MAX_MSG_CHARS && !state.overLengthWarned) {
    state.overLengthWarned = true;
  }

  if (opt.stream === false) {
    var plain = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/messages`, {
      method: "POST", body: { text: body },
    });
    setState({ sending: false });
    if (!plain.ok) {
      pushMessage({ role: "system", content: "发送失败：" + plain.error });
      setState({ lastError: plain.error });
      return false;
    }
    applySendResult(plain.data);
    return true;
  }

  var streamRes = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/messages/stream`, {
    method: "POST", body: { text: body }, raw: true,
  });
  if (!streamRes.ok) {
    var detail = "";
    try { detail = await streamRes.response.text(); } catch (e) { detail = ""; }
    setState({ sending: false, lastError: "发送失败（HTTP " + streamRes.status + "）" + (detail ? "：" + detail : "") });
    return false;
  }

  // 分段渲染：先插入一个空的 agent 气泡，逐段 append
  var bubbleIndex = state.messages.length;
  state.messages.push({ role: "agent", content: "", streaming: true });
  render();

  var meta = null;
  var decision = null;
  var finished = false;
  await consumeSSE(streamRes.response, function (ev) {
    if (ev.event === "status") {
      state.messages[bubbleIndex].content = (ev.data && ev.data.message) || "处理中…";
      render();
    } else if (ev.event === "meta") {
      meta = ev.data || {};
      if (meta.max_rounds) state.maxRounds = meta.max_rounds;
      if (meta.round != null) state.round = meta.round;
      if (meta.status) state.status = meta.status;
      if (meta.gate) state.gate = meta.gate;
      state.messages[bubbleIndex].content = "";
    } else if (ev.event === "delta") {
      state.messages[bubbleIndex].content += (ev.data && ev.data.text) || "";
      render();
    } else if (ev.event === "done") {
      finished = true;
      if (ev.data) decision = ev.data.decision || null;
    } else if (ev.event === "error") {
      state.messages[bubbleIndex].content = "（本次未能完成）" +
        ((ev.data && ev.data.message) ? ev.data.message : "未知错误");
      if (ev.data && ev.data.code) setState({ lastError: ev.data.message || ev.data.code });
    }
  });

  state.messages[bubbleIndex].streaming = false;
  if (!finished && !state.messages[bubbleIndex].content) {
    state.messages[bubbleIndex].content = "（未收到完整回复，请重试或转人工）";
  }
  if (meta) {
    if (meta.escalated) state.escalated = true;
    if (meta.status === "CLOSED") state.ended = true;
  }
  if (decision) {
    var amount = decision.refund_amount != null ? decision.refund_amount : null;
    state.messages[bubbleIndex].amount = amount != null ? { label: "建议退款金额", value: amount } : null;
    var basis = [];
    if (decision.hit_rules && decision.hit_rules.length) basis.push("命中规则：" + decision.hit_rules.join("、"));
    if (decision.conclusion) basis.push("结论： " + decision.conclusion);
    if (basis.length) state.messages[bubbleIndex].basis = basis;
  }
  setState({ sending: false });
  return true;
}

/** 非流式响应 → 视图（保留给降级路径与测试）。 */
function applySendResult(data) {
  var d = data || {};
  if (d.max_rounds) state.maxRounds = d.max_rounds;
  if (d.round != null) state.round = d.round;
  if (d.status) state.status = d.status;
  if (d.gate) state.gate = d.gate;
  var msg = { role: "agent", content: d.reply || "（无回复）" };
  if (d.decision && d.decision.refund_amount != null) {
    msg.amount = { label: "建议退款金额", value: d.decision.refund_amount };
  }
  pushMessage(msg);
  if (d.escalated) state.escalated = true;
  if (d.status === "CLOSED") state.ended = true;
  render();
}

/** 上传举证材料。 */
async function uploadAttachment(file) {
  if (!file) return false;
  var form = new FormData();
  form.append("file", file, file.name);
  var res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/attachments`, {
    method: "POST", form: form,
  });
  if (!res.ok) {
    setState({ lastError: "上传失败：" + res.error });
    return false;
  }
  state.attachments.push(res.data.filename || file.name);
  pushMessage({ role: "system", content: "已上传材料：" + (res.data.filename || file.name) });
  return true;
}

/** 转人工（终态、幂等）。 */
async function escalate() {
  if (!state.caseId || state.escalated) return false;
  var res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/escalate`, {
    method: "POST", body: { note: "" },
  });
  if (!res.ok) {
    setState({ lastError: "转人工失败：" + res.error });
    return false;
  }
  pushMessage({ role: "agent", content: res.data.reply || "已为你转接人工客服。" });
  setState({ escalated: true });
  return true;
}

/* ---------- 初始化 ---------- */

function readQuery() {
  var out = { order_id: "", api: "" };
  var search = "";
  try {
    search = (typeof window !== "undefined" && window.location && window.location.search) || "";
  } catch (e) { search = ""; }
  if (!search) return out;
  var params = new URLSearchParams(search);
  out.order_id = params.get("order_id") || "";
  out.api = params.get("api") || "";
  return out;
}

function bindEvents() {
  document.addEventListener("click", function (e) {
    var el = e.target;
    if (!el || !el.id && !el.getAttribute) return;
    var id = el.id || "";
    if (id === "h5-send") {
      var input = document.getElementById("h5-input");
      if (input) sendMessage(input.value).then(function () { input.value = ""; });
    } else if (id === "h5-upload") {
      var picker = document.getElementById("h5-file");
      if (picker) picker.click();
    } else if (id === "h5-escalate") {
      escalate();
    } else if (el.getAttribute && el.getAttribute("data-quick")) {
      sendMessage(el.getAttribute("data-quick"));
    }
  });

  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el || el.id !== "h5-input") return;
    var counter = document.getElementById("h5-counter");
    if (!counter) return;
    var len = (el.value || "").length;
    counter.innerHTML = len + " / " + MAX_MSG_CHARS;
    if (len > MAX_MSG_CHARS) counter.classList.add("over");
    else counter.classList.remove("over");
  });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || el.id !== "h5-file") return;
    var f = el.files && el.files[0];
    if (f) uploadAttachment(f);
  });
}

async function init() {
  var q = readQuery();
  if (q.api) state.apiBase = q.api;
  state.quota = state.maxRounds;
  bindEvents();
  render();
  if (q.order_id) {
    await createCase(q.order_id);
  }
  return state;
}

// 浏览器中自动启动；Node 验证脚本会显式调用 init()/createCase() 等
if (typeof window !== "undefined" && window.document) {
  init();
}
