/* ============================================================
   商家售后指挥台 —— 逻辑（免构建 · 零依赖）

   设计要点（与 h5.js 同一套约定）：
   1) **单一出口**：所有后端调用都经 `api()`，便于契约校验与统一错误处理。
   2) **纯函数渲染**：workflowLabel / renderList / renderDetail 等只吃数据、吐 HTML
      字符串 —— 可脱离浏览器直接单测（见 verify_console.cjs）。
   3) **不静默失败**：后端不可达 / 未启用 / 工单不存在，一律显示明确提示。
   4) **人始终在环**：指派 / 介入 / 确认 / 驳回四个动作都打到后端，由后端裁决
      状态合法性（非法状态返回 409），前端不自行改状态。

   工单状态（workflow_status）：queued / agent_running / awaiting_confirm / resolved / escalated
   判定处置（decision_result）：refund / reship / reject / manual / pending_evidence
   ============================================================ */

/* ---------- 配置 ---------- */
var DEFAULT_API_BASE = "http://127.0.0.1:8000";

var state = {
  apiBase: DEFAULT_API_BASE,
  filter: "inbox",          // inbox / agent_running / awaiting_confirm / resolved / escalated
  cases: [],                // 收件箱列表（case 字典）
  selectedId: "",
  detail: null,             // 当前详情 {case, messages, slots, guardrails}
  busy: false,
  lastError: "",
};

/* ---------- 常量（供渲染与单测共用） ---------- */
var WF_LABEL = {
  queued: "待处理",
  agent_running: "智能体处理中",
  awaiting_confirm: "待确认",
  resolved: "已完结",
  escalated: "已转人工",
};

var DECISION_LABEL = {
  refund: "退款",
  reship: "补发",
  reject: "拒绝",
  manual: "转人工",
  pending_evidence: "需补证",
};

var SLOT_LABEL = {
  claim_type: "诉求分类",
  issue_object: "问题对象",
  expectation: "期望方案",
  symptom: "问题现象",
  affected_qty: "影响数量",
  signed_days: "签收后时长",
  evidence: "举证材料",
  note: "补充说明",
};

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
  try {
    var res = await fetch(url, init);
    var data = null;
    var text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
    if (!res.ok) {
      return { ok: false, status: res.status, data: data, error: (data && data.detail) || ("HTTP " + res.status) };
    }
    return { ok: true, status: res.status, data: data, error: "" };
  } catch (err) {
    return { ok: false, status: 0, data: null, error: "无法连接后端：" + err.message };
  }
}

/* ---------- 纯函数（渲染，可单测） ---------- */

function workflowLabel(ws) { return WF_LABEL[ws] || ws || "未知"; }
function decisionLabel(dr) { return DECISION_LABEL[dr] || ""; }

function badgeClass(ws) {
  if (WF_LABEL[ws]) return ws;
  return "queued";
}

/** 把 ISO/datetime 字符串裁成短时间（MM-DD HH:MM）。 */
function formatTime(v) {
  if (!v) return "";
  var s = String(v).replace("T", " ").slice(0, 16);
  // 已是 "YYYY-MM-DD HH:MM"
  return s;
}

/** 收件箱侧栏：过滤项 + 计数。 */
function renderSidebar(cases, filter) {
  var counts = { inbox: 0, agent_running: 0, awaiting_confirm: 0, resolved: 0, escalated: 0 };
  (cases || []).forEach(function (c) {
    var ws = c.workflow_status || "";
    if (counts[ws] !== undefined) counts[ws]++;
    if (ws === "queued" || ws === "agent_running" || ws === "awaiting_confirm") counts.inbox++;
  });
  var items = [
    ["inbox", "收件箱", counts.inbox],
    ["agent_running", "处理中", counts.agent_running],
    ["awaiting_confirm", "待确认", counts.awaiting_confirm],
    ["resolved", "已完结", counts.resolved],
    ["escalated", "已转人工", counts.escalated],
  ];
  var html = "";
  items.forEach(function (it) {
    html += '<a class="' + (it[0] === filter ? "on" : "") + '" data-filter="' + it[0] + '">' +
      it[1] + '<span class="n">' + it[2] + "</span></a>";
  });
  return html;
}

/** 列表项摘要：无 messages 时从 case 字段推导。 */
function caseSummary(c) {
  if (c.decision_result && DECISION_LABEL[c.decision_result]) {
    return "结论：" + DECISION_LABEL[c.decision_result] + (c.decision_amount != null ? " ¥" + c.decision_amount : "");
  }
  if (c.assignee === "manual") return "管理员处理中";
  if (c.escalated_reason) return "转人工：" + c.escalated_reason;
  return "待处理";
}

function renderList(cases, selectedId) {
  var html = '<div class="hd"><span>售后问题（实时收件箱）</span><span>' + (cases || []).length + " 单</span></div>";
  html += '<div class="items">';
  if (!cases || !cases.length) {
    html += '<div class="empty">暂无工单</div>';
  } else {
    cases.forEach(function (c) {
      var id = c.case_id || c.order_id || "";
      var ws = c.workflow_status || "queued";
      var on = id === selectedId ? " on" : "";
      html += '<div class="case' + on + '" data-id="' + escapeHtml(id) + '">' +
        '<div class="r1"><span class="oid">' + escapeHtml(c.order_id || id) + "</span>" +
        '<span class="badge ' + badgeClass(ws) + '">' + workflowLabel(ws) + "</span></div>" +
        '<div class="meta"><span>' + escapeHtml(caseSummary(c)) + "</span>" +
        "<span>轮 " + (c.round || 0) + "</span><span>" + formatTime(c.updated_at || c.created_at) + "</span></div></div>";
    });
  }
  return html + "</div>";
}

function renderMessages(messages) {
  var html = "";
  (messages || []).forEach(function (m) {
    var role = m.role === "buyer" ? "buyer" : (m.role === "system" ? "system" : "agent");
    var label = role === "buyer" ? "买家" : (role === "system" ? "" : "智能体");
    html += '<div class="msg ' + role + '"><div><div class="bubble">' + escapeHtml(m.content || "") + "</div>" +
      '<div class="meta">' + (label ? label + " · " : "") + formatTime(m.created_at) + "</div></div></div>";
  });
  return html;
}

/** 动作日志：从 agent 消息的 tool_calls（审计）提取。 */
function collectActionLog(messages) {
  var acts = [];
  (messages || []).forEach(function (m) {
    var calls = m.tool_calls || [];
    if (!Array.isArray(calls)) return;
    calls.forEach(function (a) {
      acts.push({
        tool: a.tool || "",
        ok: a.ok !== false,
        elapsed_ms: a.elapsed_ms,
        summary: a.summary || a.error || "",
        error: a.error || "",
      });
    });
  });
  return acts;
}

function renderActionLog(messages) {
  var acts = collectActionLog(messages);
  if (!acts.length) return "";
  var html = '<div class="actionlog">';
  acts.forEach(function (a) {
    var label = escapeHtml(a.tool || "工具") + (a.ok ? "" : "（失败）");
    var res = a.summary ? escapeHtml(a.summary) : "";
    html += '<div class="act' + (a.ok ? "" : " fail") + '"><i></i><span>' + label + "</span>" +
      (res ? '<span class="res">' + res + "</span>" : "") + "</div>";
  });
  return html + "</div>";
}

/** 槽位结构化视图（只读展示，模型推断）。 */
function renderSlots(slots) {
  var html = '<div class="box"><div class="bt">智能体抽取的槽位（推断，不参与判定）</div><div class="slotgrid">';
  if (!slots || !slots.length) {
    html += '<div class="hint">暂无</div>';
  } else {
    slots.forEach(function (s) {
      var key = s.slot || "";
      var label = SLOT_LABEL[key] || key;
      var conf = s.confidence != null ? (" · 置信度 " + Math.round(Number(s.confidence) * 100) + "%") : "";
      html += '<div class="slot"><div class="k">' + escapeHtml(label) + "</div>" +
        '<div class="v">' + escapeHtml(s.value || "—") + "</div>" +
        '<div class="c">' + (s.source_seq != null ? "来源轮 " + s.source_seq : "") + conf + "</div></div>";
    });
  }
  return html + "</div></div>";
}

/** 订单事实（只展示 case 上真实存在的字段，不编造）。 */
function renderFacts(c) {
  var rows = [
    ["订单号", c.order_id || c.case_id || ""],
    ["会话状态", c.status || ""],
    ["已用轮次", c.round != null ? String(c.round) : "0"],
    ["处理者", c.assignee === "agent" ? "智能体" : (c.assignee === "manual" ? "管理员" : "—")],
  ];
  var html = '<div class="box"><div class="bt">工单事实</div>';
  rows.forEach(function (r) {
    html += '<div class="row"><span class="k">' + escapeHtml(r[0]) + '</span><span class="v">' + escapeHtml(r[1]) + "</span></div>";
  });
  return html + "</div>";
}

/** 举证材料：从槽位 evidence（逗号分隔引用）提取。 */
function collectEvidence(slots) {
  var names = [];
  (slots || []).forEach(function (s) {
    if (s.slot === "evidence" && s.value) {
      String(s.value).split(",").forEach(function (n) {
        var t = n.trim();
        if (t) names.push(t);
      });
    }
  });
  return names;
}

function renderEvidence(slots) {
  var names = collectEvidence(slots);
  var html = '<div class="box"><div class="bt">举证材料（点击查看）</div>';
  if (!names.length) {
    html += '<div class="hint">暂无</div>';
  } else {
    html += '<div class="thumbs">';
    names.forEach(function (name) {
      html += '<div class="thumb" data-ev="' + escapeHtml(name) + '" onclick="showEvidence(this)">🖼' +
        '<span class="nm">' + escapeHtml(name) + "</span></div>";
    });
    html += "</div>";
  }
  return html + "</div>";
}

/** 结论卡：由 decision_result + decision_amount 确定性渲染。 */
function renderDecisionCard(c) {
  var dr = c.decision_result;
  if (!dr || !DECISION_LABEL[dr]) return "";
  var cls = "card " + (dr === "reject" ? "reject" : (dr === "manual" || dr === "pending_evidence" ? dr : (dr === "reship" ? "reship" : "")));
  var icon = { refund: "✅", reship: "📦", reject: "⛔", manual: "🙋", pending_evidence: "📎" }[dr] || "ℹ️";
  var title = { refund: "同意退款", reship: "同意补发", reject: "暂不满足处理条件", manual: "转人工核实", pending_evidence: "需补充举证" }[dr] || DECISION_LABEL[dr];
  var html = '<div class="' + cls + '"><div class="ct">' + icon + " " + escapeHtml(title) + "</div>";
  if (dr === "refund" && c.decision_amount != null) {
    html += '<div class="amt">¥' + escapeHtml(c.decision_amount) + "</div>";
  } else if (c.decision_amount != null) {
    html += '<div class="line"><span class="k">金额：</span>¥' + escapeHtml(c.decision_amount) + "</div>";
  }
  if (c.confirmed_by) {
    html += '<div class="line"><span class="k">确认人：</span>' + escapeHtml(c.confirmed_by) +
      (c.confirmed_at ? " · " + formatTime(c.confirmed_at) : "") + "</div>";
  }
  return html + "</div>";
}

/** 操作区：按工单状态给出合法动作。 */
function renderOpBar(c) {
  var ws = c.workflow_status || "queued";
  var html = '<div class="actions">';
  if (ws === "queued") {
    html += '<button class="btn primary" data-op="dispatch">指派智能体</button>' +
      '<button class="btn ghost" data-op="intervene">自己处理</button>' +
      '<span class="hint">指派后智能体将与买家沟通、取证、判定</span>';
  } else if (ws === "agent_running") {
    html += '<span class="hint">智能体处理中…</span>' +
      '<button class="btn danger" data-op="intervene">介入（接管）</button>';
  } else if (ws === "awaiting_confirm") {
    var dr = c.decision_result;
    var confirmLabel = dr === "reship" ? "确认补发" : (dr === "reject" ? "确认拒绝" : "确认退款");
    html += '<button class="btn ok" data-op="confirm">' + confirmLabel + "</button>" +
      '<button class="btn danger" data-op="reject">驳回</button>' +
      '<span class="hint">确认后资金/库存动作按红线分级执行</span>';
  } else if (ws === "resolved") {
    html += '<span class="hint">已完结，结论已归档。</span>';
  } else if (ws === "escalated") {
    html += '<span class="hint">已转人工，由管理员接管处理。</span>';
  }
  return html + "</div>";
}

/** 详情面板（整体）。 */
function renderDetail(detail) {
  if (!detail || !detail.case) {
    return '<div class="empty-detail">请从左侧选择一笔工单</div>';
  }
  var c = detail.case;
  var ws = c.workflow_status || "queued";
  var sub = [];
  if (c.decision_result && DECISION_LABEL[c.decision_result]) sub.push(DECISION_LABEL[c.decision_result]);
  sub.push("轮 " + (c.round || 0));
  var html = '<div class="top"><div class="r1">' +
    "<h3>" + escapeHtml(c.order_id || c.case_id || "") + "</h3>" +
    '<span class="badge ' + badgeClass(ws) + '">' + workflowLabel(ws) + "</span>" +
    '<span class="sub">' + escapeHtml(sub.join(" · ")) + "</span></div>" +
    '<div class="sub" style="margin-top:4px">工单号：' + escapeHtml(c.case_id || "—") +
    (c.updated_at ? " · 更新于 " + formatTime(c.updated_at) : "") + "</div></div>";

  html += '<div class="body">';
  html += '<div class="grid">' + renderFacts(c) + renderEvidence(detail.slots) + "</div>";
  html += renderSlots(detail.slots);
  html += '<div class="box"><div class="bt">完整对话历史（管理员可见）</div>' +
    (detail.messages && detail.messages.length ? renderMessages(detail.messages) : '<div class="hint">暂无消息</div>') +
    "</div>";
  html += renderActionLog(detail.messages);
  html += renderDecisionCard(c);
  html += renderOpBar(c);
  return html + "</div></div>";
}

/* ---------- 动作 ---------- */

function setConn(status) {
  var el = document.getElementById("conn");
  if (!el) return;
  el.className = "conn " + status;
  var txt = document.getElementById("connText");
  if (txt) txt.textContent = status === "online" ? "已连接" : (status === "offline" ? "后端不可达" : "连接中…");
}

function setError(msg) {
  state.lastError = msg || "";
}

async function loadList() {
  var r = await api("/api/console/cases");
  if (!r.ok) {
    setConn("offline");
    setError(r.error);
    render();
    return;
  }
  setConn("online");
  setError("");
  state.cases = (r.data && r.data.items) || [];
  render();
}

function filteredCases() {
  if (state.filter === "inbox") {
    return state.cases.filter(function (c) {
      var ws = c.workflow_status;
      return ws === "queued" || ws === "agent_running" || ws === "awaiting_confirm";
    });
  }
  return state.cases.filter(function (c) { return c.workflow_status === state.filter; });
}

async function loadDetail(caseId) {
  state.selectedId = caseId;
  state.detail = null;
  render();
  var r = await api(`/api/console/cases/${encodeURIComponent(caseId)}`);
  if (!r.ok) {
    setError(r.error);
    render();
    return;
  }
  setError("");
  state.detail = r.data;
  render();
}

async function doAction(op) {
  if (!state.selectedId || state.busy) return;
  state.busy = true;
  var caseId = state.selectedId;
  var r;
  if (op === "dispatch") r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/dispatch`, { method: "POST" });
  else if (op === "intervene") r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/intervene`, { method: "POST" });
  else if (op === "confirm") r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/confirm`, { method: "POST", body: { reviewer: "admin" } });
  else if (op === "reject") r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/reject`, { method: "POST", body: { reason: "管理员驳回判定" } });
  else { state.busy = false; return; }
  state.busy = false;
  if (!r.ok) {
    setError(r.error);
    render();
    return;
  }
  setError("");
  await loadList();
  await loadDetail(caseId);
}

/* ---------- 渲染 ---------- */

function render() {
  var side = document.getElementById("side");
  var listPane = document.getElementById("listPane");
  var detailPane = document.getElementById("detailPane");
  if (side) side.innerHTML = renderSidebar(state.cases, state.filter);
  if (listPane) listPane.innerHTML = renderList(filteredCases(), state.selectedId);
  if (detailPane) detailPane.innerHTML = renderDetail(state.detail);

  // 侧栏过滤点击
  document.querySelectorAll(".side a").forEach(function (a) {
    a.onclick = function () {
      state.filter = a.getAttribute("data-filter");
      render();
    };
  });
  // 列表项点击
  document.querySelectorAll(".case").forEach(function (el) {
    el.onclick = function () { loadDetail(el.getAttribute("data-id")); };
  });
  // 操作按钮
  document.querySelectorAll("[data-op]").forEach(function (b) {
    b.onclick = function () { doAction(b.getAttribute("data-op")); };
  });
}

function showEvidence(el) {
  var name = el.getAttribute("data-ev");
  var lb = document.createElement("div");
  lb.className = "lightbox";
  lb.innerHTML = '<div class="lb-inner"><div class="lb-img">🖼 证据图（图片回显未接入）</div><div class="lb-name">' + escapeHtml(name) + "</div></div>";
  lb.onclick = function () { lb.remove(); };
  document.body.appendChild(lb);
}

function init() {
  var qs = (location.search || "").replace(/^\?/, "").split("&");
  qs.forEach(function (p) {
    var kv = p.split("=");
    if (kv[0] === "api" && kv[1]) state.apiBase = decodeURIComponent(kv[1]);
  });
  var btn = document.getElementById("btnRefresh");
  if (btn) btn.onclick = loadList;
  loadList();
}

init();
