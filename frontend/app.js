/* ============================================================
   电商售后智能体 · 复核工作台（免构建 · 零依赖）

   设计约定（便于后续迁移 Vue3）：
   - **数据与渲染分离**：所有远端数据先落到 `state`，再由纯函数渲染 HTML；
   - **单一入口**：任何数据变更后调用 `render()`，与 Vue 的响应式心智一致；
   - **接口层统一**：所有请求走 `api()`，集中处理错误与后端不可达。

   后端默认地址 http://127.0.0.1:8000，可在顶栏修改（存 localStorage）。
   ============================================================ */

// ============================================================
// 状态
// ============================================================
const state = {
  apiBase: localStorage.getItem("refund.apiBase") || "http://127.0.0.1:8000",
  reviewer: localStorage.getItem("refund.reviewer") || "qa-zhang",
  tab: "pending",
  online: null, // null=未知 / true=在线 / false=离线
  busy: false,
  error: "",
  notice: "",

  pending: [],
  audits: { items: [], total: 0, limit: 20, offset: 0, conclusion: "", orderId: "" },
  stats: null,
  statsRange: { since: "", until: "" },
  scenarios: [],
  // 表单字段全部纳入 state —— 渲染是"数据驱动"的，重渲染不能丢失用户输入
  submit: {
    scenario: "",
    orderId: "",
    payAmount: "39",
    claimAmount: "39",
    reason: "not_received",
    imageCount: "0",
    note: "",
    lastResult: null,
  },
  // 口径问答：messages 为对话记录（user/bot），knowledge 为可选提问清单
  qa: { question: "", orderId: "", messages: [], knowledge: [] },
};

// 结论 → 徽章样式与中文名
const CONCLUSION_META = {
  auto_pass: { text: "自动通过", cls: "ok" },
  auto_reject: { text: "自动驳回", cls: "danger" },
  request_evidence: { text: "要求补证", cls: "warn" },
  to_human: { text: "转人工", cls: "primary" },
};

const REASON_LABELS = {
  not_received: "未收到货",
  quality: "质量问题",
  wrong_item: "错发/漏发",
  no_reason: "无理由",
  other: "其他",
};

// 人工复核可选决策 → 按钮文案与样式。
// 具体提供哪些选项由**后端**在 interrupt 载荷的 options 里给出：
// 规则判定转人工时只有「维持 / 改判」；处置未落地时才多出「重试处置」。
const DECISION_META = {
  maintain: { text: "维持", cls: "" },
  override: { text: "改判", cls: "danger" },
  retry: { text: "重试处置", cls: "primary" },
};

// ============================================================
// 基础工具
// ============================================================
const $ = (sel) => document.querySelector(sel);

/** HTML 转义：所有来自后端的字符串都必须经此进入模板，避免 XSS */
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtAmount(v) {
  if (v === null || v === undefined || v === "") return "—";
  return "¥" + Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtRate(v) {
  if (v === null || v === undefined) return "—";
  return (Number(v) * 100).toFixed(1) + "%";
}

function conclusionBadge(code) {
  const meta = CONCLUSION_META[code] || { text: code || "—", cls: "" };
  return `<span class="badge ${meta.cls}">${esc(meta.text)}</span>`;
}

function todayStr(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

/**
 * 轻量文本渲染：**先转义**，再支持 `**加粗**` 与换行。
 * 不引入 Markdown 依赖（本项目前端免构建、零依赖），但必须保证转义在前，
 * 否则后端返回的文本可注入 HTML。
 */
function fmtText(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\n/g, "<br />");
}

// ============================================================
// 接口层
// ============================================================
/**
 * 统一请求封装。
 * 后端不可达与业务错误都转成 Error，由调用方统一展示——
 * **绝不静默失败**（否则使用者会误以为"没有数据"）。
 */
async function api(path, { method = "GET", body } = {}) {
  const url = state.apiBase.replace(/\/$/, "") + path;
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }

  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    state.online = false;
    throw new Error(`无法连接后端（${url}）。请确认已启动服务：uvicorn app.main:app --port 8000`);
  }

  state.online = true;
  let payload = null;
  try {
    payload = await resp.json();
  } catch (err) {
    payload = null;
  }

  if (!resp.ok) {
    const detail = payload && payload.detail ? payload.detail : `HTTP ${resp.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

/** 统一包裹动作：置忙 → 清错 → 执行 → 渲染 */
async function run(action) {
  state.busy = true;
  state.error = "";
  state.notice = "";
  render();
  try {
    await action();
  } catch (err) {
    state.error = err.message || String(err);
  } finally {
    state.busy = false;
    render();
  }
}

// ============================================================
// 业务动作
// ============================================================
const loadPending = () =>
  run(async () => {
    const data = await api("/api/reviews/pending?limit=50");
    state.pending = data.items || [];
  });

const loadAudits = (offset = null) =>
  run(async () => {
    const a = state.audits;
    if (offset !== null) a.offset = Math.max(0, offset);
    const params = new URLSearchParams({ limit: a.limit, offset: a.offset });
    if (a.conclusion) params.set("conclusion", a.conclusion);
    if (a.orderId) params.set("order_id", a.orderId);
    const data = await api("/api/audits?" + params.toString());
    a.items = data.items || [];
    a.total = data.total || 0;
  });

const loadStats = () =>
  run(async () => {
    const params = new URLSearchParams();
    if (state.statsRange.since) params.set("since", state.statsRange.since);
    if (state.statsRange.until) params.set("until", state.statsRange.until);
    const qs = params.toString();
    state.stats = await api("/api/audits/stats" + (qs ? "?" + qs : ""));
  });

const loadScenarios = () =>
  run(async () => {
    const data = await api("/api/dev/scenarios");
    state.scenarios = data.items || [];
    if (!state.submit.scenario && state.scenarios.length) {
      state.submit.scenario = state.scenarios[0].key;
    }
    if (!data.available) {
      state.notice = `演示场景不可用：${data.reason}（已接入真实业务 API）`;
    }
  });

const loadKnowledge = () =>
  run(async () => {
    const data = await api("/api/qa/knowledge");
    state.qa.knowledge = data.items || [];
  });

const doAsk = () =>
  run(async () => {
    const qa = state.qa;
    const question = (qa.question || "").trim();
    if (!question) throw new Error("请输入问题");
    const orderId = (qa.orderId || "").trim();

    // 先把提问入列（让使用者立刻看到自己的问题），再等待回答
    qa.messages.push({ role: "user", text: orderId ? `${question}（受理号 ${orderId}）` : question });

    const data = await api("/api/qa", {
      method: "POST",
      body: { question, order_id: orderId || null },
    });
    qa.messages.push({
      role: "bot",
      text: data.answer || "（后端返回了空回答）",
      sources: data.sources || [],
      modelUsed: !!data.model_used,
    });
    qa.question = "";

    if (!qa.knowledge.length) {
      const kb = await api("/api/qa/knowledge");
      qa.knowledge = kb.items || [];
    }
  });

const doReview = (orderId, decision) =>
  run(async () => {
    await api(`/api/review/${encodeURIComponent(orderId)}`, {
      method: "POST",
      body: { decision, reviewer: state.reviewer },
    });
    const label = (DECISION_META[decision] || {}).text || decision;
    state.notice = `${orderId} 已提交复核：${label}`;
    const data = await api("/api/reviews/pending?limit=50");
    state.pending = data.items || [];
  });

const doSubmit = () =>
  run(async () => {
    const f = state.submit;
    const orderId = (f.orderId || "").trim();
    if (!orderId) throw new Error("请填写订单号");

    const payAmount = Number(f.payAmount);
    const claimAmount = Number(f.claimAmount);
    if (!(payAmount > 0) || !(claimAmount > 0)) throw new Error("金额必须大于 0");
    if (claimAmount > payAmount) throw new Error("申请退款金额不得超过实付金额");

    // 1) 注入演示取数（仅 mock 模式可用）
    if (f.scenario) {
      await api("/api/dev/seed", {
        method: "POST",
        body: { order_id: orderId, scenario: f.scenario, pay_amount: payAmount },
      });
    }

    // 2) 提交申请并执行判定
    const result = await api("/api/requests", {
      method: "POST",
      body: {
        order_id: orderId,
        pay_amount: payAmount,
        claim_amount: claimAmount,
        refund_reason_code: f.reason,
        // 非本地路径只计数、不下载校验（见节点实现），故演示值可安全使用
        claim_images: Array.from(
          { length: Number(f.imageCount) || 0 },
          (_, i) => `demo://evidence/${i + 1}`
        ),
        buyer_note: f.note,
      },
    });

    f.lastResult = result;
    // 后端对同一受理号做了幂等保护：已受理过则返回既有结论，不会重复判定/处置
    state.notice = result.idempotent_replay
      ? `${orderId} 此前已受理，返回既有结论（未重复判定）`
      : `${orderId} 判定完成：${(CONCLUSION_META[result.conclusion] || {}).text || result.conclusion}`;
  });

// ============================================================
// 渲染
// ============================================================
function renderHeader() {
  const cls = state.online === null ? "" : state.online ? "online" : "offline";
  const text = state.online === null ? "未连接" : state.online ? "后端在线" : "后端离线";
  return `
    <div class="topbar">
      <h1>电商售后智能体 · 复核工作台</h1>
      <span class="status ${cls}"><span class="dot"></span>${text}</span>
      <div class="spacer"></div>
      <label for="api-base">后端地址</label>
      <input type="text" id="api-base" value="${esc(state.apiBase)}" size="34" />
      <button data-action="apply-api-base">应用</button>
      <label for="reviewer-input">复核人</label>
      <input type="text" id="reviewer-input" value="${esc(state.reviewer)}" size="12" />
    </div>`;
}

function renderTabs() {
  const tabs = [
    ["pending", "待复核队列"],
    ["audits", "审计查询"],
    ["stats", "统计看板"],
    ["submit", "提交申请"],
    ["qa", "口径问答"],
  ];
  return `<div class="tabs">${tabs
    .map(
      ([key, label]) =>
        `<button data-action="tab" data-tab="${key}" class="${state.tab === key ? "active" : ""}">${label}</button>`
    )
    .join("")}</div>`;
}

function renderAlerts() {
  let html = "";
  if (state.error) html += `<div class="alert error">${esc(state.error)}</div>`;
  if (state.notice) html += `<div class="alert info">${esc(state.notice)}</div>`;
  return html;
}

// 命中明细的可读性映射：规则表里用的是字段代码与运算符代码，
// 直接呈现给复核人（业务岗）无法理解，故统一转成中文与数学符号。
const OP_LABELS = {
  gte: "≥", lte: "≤", gt: ">", lt: "<", eq: "=", ne: "≠",
  in: "属于", is_true: "为真", is_false: "为假",
};

const FIELD_LABELS = {
  claim_amount: "申请退款金额",
  pay_amount: "实付金额",
  refund_reason_code: "退款原因",
  logistics_status: "物流状态",
  image_count: "有效举证图",
  buyer_credit_score: "买家信用分",
  buyer_refund_rate_90d: "90天退款率",
  refund_count_90d: "90天退款笔数",
  has_order: "订单数据可取到",
  has_logistics: "物流数据可取到",
};

const LOGISTICS_LABELS = {
  not_shipped: "未发货",
  in_transit: "运输中",
  not_signed: "未签收",
  signed: "已签收",
};

/** 把字段值转成人能读的形式（枚举转中文、布尔转是否、比例转百分比） */
function fmtFieldValue(field, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map((v) => fmtFieldValue(field, v)).join(" / ");
  if (typeof value === "boolean") return value ? "是" : "否";
  if (field === "refund_reason_code") return REASON_LABELS[value] || value;
  if (field === "logistics_status") return LOGISTICS_LABELS[value] || value;
  // 退款率是 0~1 的比例，业务上习惯看百分比
  if (field === "buyer_refund_rate_90d" && typeof value === "number") {
    return `${(value * 100).toFixed(1)}%`;
  }
  return String(value);
}

/** 命中明细：把每条规则的叶子条件展开，供复核人追溯（已中文化） */
function renderHitDetails(item) {
  const details = item.hit_details || [];
  if (!details.length) return "";
  const body = details
    .map((d) => {
      const leaves = (d.leaves || [])
        .map((l) => {
          const field = FIELD_LABELS[l.field] || l.field;
          const op = OP_LABELS[l.op] || l.op;
          const thresholdText = fmtFieldValue(l.field, l.threshold);
          const actualText = fmtFieldValue(l.field, l.actual);
          // 等值/不等值比较时，阈值与实际值必然一致（如"退款原因 = 质量问题"），
          // 再缀一句"（实际 质量问题）"纯属冗余，故省略。
          const isEquality = l.op === "eq" || l.op === "ne";
          const tail = isEquality && thresholdText === actualText
            ? ""
            : `（实际 ${esc(actualText)}）`;
          return `${esc(field)} ${esc(op)} ${esc(thresholdText)}${tail}`;
        })
        .join("；");
      return `<div><b>${esc(d.rule_id)}</b> ${esc(d.description || "")}<br />${leaves}</div>`;
    })
    .join("<hr style='border:none;border-top:1px solid #e4e6ea;margin:6px 0' />");
  return `<details><summary>命中明细（${details.length} 条）</summary><div class="detail-box">${body}</div></details>`;
}

/** 复核操作按钮：由后端给出的 options 决定，避免前端硬编码业务可选项 */
function renderReviewButtons(item) {
  const value = (item.interrupt && item.interrupt.value) || {};
  const options = value.options && value.options.length ? value.options : ["maintain", "override"];
  return options
    .map((key) => {
      const meta = DECISION_META[key] || { text: key, cls: "" };
      return `<button class="${meta.cls}" data-action="review" data-order="${esc(item.order_id)}" data-decision="${esc(key)}">${esc(meta.text)}</button>`;
    })
    .join(" ");
}

/** 挂起原因提示：区分"规则判定转人工"与"处置未落地" */
function renderPendingReason(item) {
  const value = (item.interrupt && item.interrupt.value) || {};
  if (value.reason !== "disposal_failed") return "";
  const attempt = value.attempt ? `（已重试 ${value.attempt} 次）` : "";
  return `<div class="alert warn" style="margin:6px 0 0">处置未落地${attempt}：${esc(value.message || "")}</div>`;
}

function renderPending() {
  const rows = state.pending
    .map(
      (item) => `
      <tr>
        <td class="mono">${esc(item.order_id)}</td>
        <td>${fmtAmount(item.claim_amount)}</td>
        <td>${esc((REASON_LABELS[item.refund_reason_code] || item.refund_reason_code) || "—")}</td>
        <td>${conclusionBadge(item.conclusion)}</td>
        <td>${(item.hit_rules || []).map((r) => `<span class="badge">${esc(r)}</span>`).join(" ") || "—"}</td>
        <td>${esc(item.risk_level || "—")}</td>
        <td>
          ${renderReviewButtons(item)}
          ${renderPendingReason(item)}
          ${renderHitDetails(item)}
        </td>
      </tr>`
    )
    .join("");

  return `
    <div class="panel">
      <h2>待复核队列 <span class="sub">共 ${state.pending.length} 条（最近活动优先）</span></h2>
      <div class="row">
        <button data-action="load-pending" ${state.busy ? "disabled" : ""}>刷新</button>
        <span class="hint">以当前“复核人”身份提交。队列数据来自检查点：命中转人工时即入队，复核完成后自动出队。</span>
      </div>
      ${
        state.pending.length
          ? `<table>
              <thead><tr><th>订单号</th><th>申请金额</th><th>原因</th><th>结论</th><th>命中规则</th><th>风险</th><th>复核操作</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>`
          : `<div class="empty">队列为空 —— 没有待人工复核的申请</div>`
      }
    </div>`;
}

function renderAudits() {
  const a = state.audits;
  const rows = a.items
    .map(
      (r) => `
      <tr>
        <td class="mono">${esc(r.request_id)}</td>
        <td class="mono">${esc(r.order_id)}</td>
        <td>${conclusionBadge(r.conclusion)}</td>
        <td>${(r.hit_rules || []).map((x) => `<span class="badge">${esc(x)}</span>`).join(" ") || "—"}</td>
        <td>${esc(r.risk_level || "—")}</td>
        <td>${esc(r.action || "—")}</td>
        <td>${r.executed ? '<span class="badge danger">已执行</span>' : '<span class="badge">未执行</span>'}</td>
        <td>${esc(r.review_decision || "—")}${r.reviewer ? `<br /><span class="hint">${esc(r.reviewer)}</span>` : ""}</td>
        <td class="mono">${esc(r.created_at || "—")}</td>
      </tr>`
    )
    .join("");

  const from = a.total === 0 ? 0 : a.offset + 1;
  const to = Math.min(a.offset + a.limit, a.total);

  return `
    <div class="panel">
      <h2>审计记录 <span class="sub">共 ${a.total} 条，当前显示 ${from}-${to}</span></h2>
      <div class="row">
        <div class="field">
          <label>结论</label>
          <select id="f-audit-conclusion">
            <option value="">全部</option>
            ${Object.keys(CONCLUSION_META)
              .map(
                (k) =>
                  `<option value="${k}" ${a.conclusion === k ? "selected" : ""}>${CONCLUSION_META[k].text}</option>`
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label>订单号</label>
          <input type="text" id="f-audit-order" value="${esc(a.orderId)}" placeholder="精确匹配" />
        </div>
        <div class="field">
          <label>每页</label>
          <select id="f-audit-limit">
            ${[10, 20, 50].map((n) => `<option value="${n}" ${a.limit === n ? "selected" : ""}>${n}</option>`).join("")}
          </select>
        </div>
        <button class="primary" data-action="load-audits" ${state.busy ? "disabled" : ""}>查询</button>
        <div class="spacer" style="flex:1"></div>
        <button data-action="page" data-offset="${Math.max(0, a.offset - a.limit)}" ${a.offset <= 0 ? "disabled" : ""}>上一页</button>
        <button data-action="page" data-offset="${a.offset + a.limit}" ${a.offset + a.limit >= a.total ? "disabled" : ""}>下一页</button>
      </div>
      ${
        a.items.length
          ? `<table>
              <thead><tr><th>受理号</th><th>订单号</th><th>结论</th><th>命中规则</th><th>风险</th><th>处置动作</th><th>执行</th><th>人工复核</th><th>写入时间</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>`
          : `<div class="empty">没有符合条件的审计记录</div>`
      }
    </div>`;
}

function renderBars(entries, colorize) {
  const max = Math.max(1, ...entries.map((e) => e.value));
  return `<div class="bars">${entries
    .map(
      (e) => `
      <div class="bar-row">
        <div>${esc(e.label)}</div>
        <div class="bar-track"><div class="bar-fill ${colorize ? colorize(e) : ""}" style="width:${(e.value / max) * 100}%"></div></div>
        <div class="bar-num">${e.value}</div>
      </div>`
    )
    .join("")}</div>`;
}

function renderStats() {
  const s = state.stats;
  if (!s) {
    return `<div class="panel"><h2>统计看板</h2><div class="empty">点击“查询”加载统计数据</div></div>`;
  }

  const dict = s.conclusion_distribution || {};
  const review = s.review_distribution || {};
  const exec = s.execution_stats || {};

  const conclusionBars = renderBars(
    Object.keys(CONCLUSION_META).map((k) => ({
      label: CONCLUSION_META[k].text,
      value: dict[k] || 0,
      key: k,
    })),
    (e) => (e.key === "auto_pass" ? "ok" : e.key === "to_human" ? "" : e.key === "request_evidence" ? "warn" : "danger")
  );

  const ruleBars = (s.rule_hits || []).length
    ? renderBars(s.rule_hits.slice(0, 10).map((r) => ({ label: r.rule_id, value: r.count })))
    : `<div class="empty">暂无命中记录</div>`;

  const reviewBars = renderBars([
    { label: "维持", value: review.maintain || 0, key: "maintain" },
    { label: "改判", value: review.override || 0, key: "override" },
    { label: "未记录", value: review.unrecorded || 0, key: "unrecorded" },
  ], (e) => (e.key === "maintain" ? "ok" : e.key === "override" ? "danger" : ""));

  return `
    <div class="panel">
      <h2>时间范围</h2>
      <div class="row">
        <div class="field"><label>起始（含）</label><input type="date" id="f-since" value="${esc(state.statsRange.since)}" /></div>
        <div class="field"><label>结束（含当天）</label><input type="date" id="f-until" value="${esc(state.statsRange.until)}" /></div>
        <button class="primary" data-action="load-stats" ${state.busy ? "disabled" : ""}>查询</button>
        <button data-action="quick-range" data-days="0">今天</button>
        <button data-action="quick-range" data-days="7">近 7 天</button>
        <button data-action="quick-range" data-days="30">近 30 天</button>
        <button data-action="quick-range" data-days="">全部</button>
      </div>
    </div>

    <div class="panel">
      <h2>核心指标 <span class="sub">共 ${s.total} 条判定 · 时间范围 ${esc((s.time_range || {}).since || "不限")} ~ ${esc((s.time_range || {}).until || "不限")}</span></h2>
      <div class="cards">
        <div class="card"><div class="k">判定总数</div><div class="v">${s.total}</div></div>
        <div class="card"><div class="k">与人工一致率</div><div class="v">${fmtRate(s.agreement_rate)}<small>维持占比</small></div></div>
        <div class="card"><div class="k">复核参与率</div><div class="v">${fmtRate(s.review_coverage)}<small>复核/转人工</small></div></div>
        <div class="card"><div class="k">资金动作已落地</div><div class="v">${exec.executed || 0}<small>影子模式应为 0</small></div></div>
      </div>
      <div class="hint" style="margin-top:10px">
        一致率无样本时显示“—”。未落地 ${exec.not_executed || 0} 条。
      </div>
    </div>

    <div class="panel"><h2>判定结论分布</h2>${conclusionBars}</div>
    <div class="panel"><h2>规则命中排行 <span class="sub">前 10</span></h2>${ruleBars}</div>
    <div class="panel"><h2>人工复核分布 <span class="sub">仅统计转人工的申请</span></h2>${reviewBars}</div>`;
}

function renderSubmit() {
  const options = state.scenarios
    .map(
      (s) =>
        `<option value="${esc(s.key)}" ${state.submit.scenario === s.key ? "selected" : ""}>${esc(s.label)}</option>`
    )
    .join("");

  const r = state.submit.lastResult;
  const resultBox = r
    ? `<div class="panel">
         <h2>判定结果</h2>
         <div class="row">
           ${conclusionBadge(r.conclusion)}
           <span>命中规则：${(r.hit_rules || []).map((x) => `<span class="badge">${esc(x)}</span>`).join(" ") || "—"}</span>
           <span>状态：${esc(r.status === "pending_review" ? "已挂起待复核" : "已完成")}</span>
         </div>
         <div class="detail-box" style="margin-top:10px">
           <pre>${esc(JSON.stringify(r, null, 2))}</pre>
         </div>
       </div>`
    : "";

  return `
    <div class="panel">
      <h2>提交申请 <span class="sub">用于本地演示与验收</span></h2>
      <div class="alert warn">
        本页依赖“演示场景”注入取数数据（内存 mock）。已接入真实业务 API 时该功能不可用，
        提交后将走“数据不足 → 转人工”的兜底路径。
      </div>
      <div class="row">
        <div class="field" style="flex:1;min-width:180px"><label>订单号</label><input type="text" id="f-order-id" value="${esc(state.submit.orderId)}" /></div>
        <div class="field" style="flex:1;min-width:180px"><label>演示场景</label><select id="f-scenario">${options || '<option value="">（无可用场景）</option>'}</select></div>
      </div>
      <div class="row">
        <div class="field"><label>实付金额</label><input type="number" id="f-pay-amount" value="${esc(state.submit.payAmount)}" step="0.01" /></div>
        <div class="field"><label>申请退款金额</label><input type="number" id="f-claim-amount" value="${esc(state.submit.claimAmount)}" step="0.01" /></div>
        <div class="field">
          <label>退款原因</label>
          <select id="f-reason">
            ${Object.keys(REASON_LABELS)
              .map(
                (k) =>
                  `<option value="${k}" ${state.submit.reason === k ? "selected" : ""}>${REASON_LABELS[k]}</option>`
              )
              .join("")}
          </select>
        </div>
        <div class="field"><label>举证图片数</label><input type="number" id="f-image-count" value="${esc(state.submit.imageCount)}" min="0" /></div>
      </div>
      <div class="row">
        <div class="field" style="flex:1"><label>买家留言（不参与判定，仅供模型理解）</label><textarea id="f-note" rows="2" style="width:100%">${esc(state.submit.note)}</textarea></div>
      </div>
      <div class="row">
        <button data-action="fill-scenario">按场景预填</button>
        <button class="primary" data-action="submit-request" ${state.busy ? "disabled" : ""}>注入数据并提交判定</button>
        <span class="hint">提交后若结论为“转人工”，可在“待复核队列”中看到它。</span>
      </div>
    </div>
    ${resultBox}`;
}

function renderQa() {
  const qa = state.qa;

  const suggestions = qa.knowledge.length
    ? qa.knowledge
        .slice(0, 6)
        .map(
          (e) =>
            `<button data-action="qa-suggest" data-q="${esc(e.title)}">${esc(e.title)}</button>`
        )
        .join(" ")
    : '<span class="hint">（口径库尚未加载）</span>';

  const bubbles = qa.messages.length
    ? qa.messages
        .map((m) => {
          if (m.role === "user") {
            return `<div class="bubble user">${esc(m.text)}</div>`;
          }
          const sources = (m.sources || []).length
            ? `<div class="row" style="margin-top:8px;flex-wrap:wrap">${m.sources
                .map((s) => `<span class="badge">${esc(s.title)}</span>`)
                .join(" ")}</div>`
            : "";
          // 明确标注是否发生计费调用，便于核对成本
          const cost = m.modelUsed
            ? "本条由模型润色措辞（事实仍来自规则）"
            : "本条为确定性回答（零模型调用）";
          return `<div class="bubble bot">${fmtText(m.text)}${sources}<div class="hint" style="margin-top:6px">${cost}</div></div>`;
        })
        .join("")
    : '<div class="empty">还没有问答记录 —— 可以直接提问，或点下方任一口径标题</div>';

  return `
    <div class="panel">
      <h2>口径问答 <span class="sub">解释判定依据与规则口径 · 不参与资格判定</span></h2>
      <div class="alert info">
        本入口只做解释与释疑，<b>不会改变任何判定结论</b>。
        可只问规则口径；若填了受理号，会一并解释"这笔为什么这么判"。
      </div>
      <div class="row">
        <div class="field" style="flex:0 0 240px">
          <label>受理号（可选）</label>
          <input type="text" id="f-qa-order" value="${esc(qa.orderId)}" placeholder="如 WB-DEMO-A4" />
        </div>
        <div class="field" style="flex:1;min-width:260px">
          <label>问题</label>
          <input type="text" id="f-qa-question" value="${esc(qa.question)}" placeholder="例如：质量问题为什么不自动判定？" />
        </div>
        <button class="primary" data-action="ask-qa" ${state.busy ? "disabled" : ""}>提问</button>
      </div>
      <div class="row" style="flex-wrap:wrap">${suggestions}</div>
      <div class="chat">${bubbles}</div>
    </div>`;
}

function render() {
  const body = {
    pending: renderPending,
    audits: renderAudits,
    stats: renderStats,
    submit: renderSubmit,
    qa: renderQa,
  }[state.tab]();

  document.getElementById("app").innerHTML = renderHeader() + renderTabs() + `<main>${renderAlerts()}${body}</main>`;
}

// ============================================================
// 事件
// ============================================================
/** 输入类控件只更新状态、不重渲染（避免打断输入与丢焦点） */
function bindInputEvents() {
  document.addEventListener("input", (e) => {
    const id = e.target.id;
    const v = e.target.value;
    if (id === "reviewer-input") {
      state.reviewer = v;
      localStorage.setItem("refund.reviewer", state.reviewer);
    } else if (id === "f-audit-order") state.audits.orderId = v.trim();
    else if (id === "f-order-id") state.submit.orderId = v;
    else if (id === "f-pay-amount") state.submit.payAmount = v;
    else if (id === "f-claim-amount") state.submit.claimAmount = v;
    else if (id === "f-image-count") state.submit.imageCount = v;
    else if (id === "f-note") state.submit.note = v;
    else if (id === "f-qa-question") state.qa.question = v;
    else if (id === "f-qa-order") state.qa.orderId = v;
  });

  // 下拉选中一律走 change（语义清晰，且避免与 input 重复触发）
  document.addEventListener("change", (e) => {
    const id = e.target.id;
    const v = e.target.value;
    if (id === "f-audit-conclusion") state.audits.conclusion = v;
    else if (id === "f-audit-limit") state.audits.limit = Number(v);
    else if (id === "f-since") state.statsRange.since = v;
    else if (id === "f-until") state.statsRange.until = v;
    else if (id === "f-scenario") state.submit.scenario = v;
    else if (id === "f-reason") state.submit.reason = v;
  });
}

function bindClickEvents() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;

    if (action === "tab") {
      state.tab = btn.dataset.tab;
      state.error = "";
      state.notice = "";
      render();
      // 切换即按需拉取，避免每次都请求全部接口
      if (state.tab === "pending") loadPending();
      else if (state.tab === "audits") loadAudits(0);
      else if (state.tab === "stats" && !state.stats) loadStats();
      else if (state.tab === "submit" && !state.scenarios.length) loadScenarios();
      else if (state.tab === "qa" && !state.qa.knowledge.length) loadKnowledge();
      return;
    }

    if (action === "apply-api-base") {
      state.apiBase = $("#api-base").value.trim() || state.apiBase;
      localStorage.setItem("refund.apiBase", state.apiBase);
      loadPending();
      return;
    }

    if (action === "load-pending") return loadPending();
    if (action === "review") return doReview(btn.dataset.order, btn.dataset.decision);
    if (action === "load-audits") return loadAudits(0);
    if (action === "page") return loadAudits(Number(btn.dataset.offset));
    if (action === "load-stats") return loadStats();
    if (action === "submit-request") return doSubmit();
    if (action === "ask-qa") return doAsk();

    if (action === "qa-suggest") {
      // 点击口径标题 → 填进问题框（不直接提问，使用者可先改措辞）
      state.qa.question = btn.dataset.q;
      render();
      return;
    }

    if (action === "quick-range") {
      const days = btn.dataset.days;
      if (days === "") {
        state.statsRange.since = "";
        state.statsRange.until = "";
      } else {
        // 纯日期即可：后端会把结束日期补足到 23:59:59（含当天）
        state.statsRange.since = todayStr(-Number(days));
        state.statsRange.until = todayStr(0);
      }
      return loadStats();
    }

    if (action === "fill-scenario") {
      const spec = state.scenarios.find((s) => s.key === state.submit.scenario);
      if (!spec) {
        state.error = "请先选择演示场景";
        render();
        return;
      }
      state.submit.payAmount = String(spec.suggest_amount);
      state.submit.claimAmount = String(spec.suggest_amount);
      state.submit.reason = spec.suggest_reason;
      state.notice = `已按「${spec.label}」预填金额与原因`;
      render();
      return;
    }
  });
}

async function init() {
  // 订单号生成一次即固定，避免每次重渲染都变（否则用户改到一半会被重置）
  if (!state.submit.orderId) {
    state.submit.orderId = "WB-" + Date.now().toString().slice(-6);
  }
  bindInputEvents();
  bindClickEvents();
  render();
  // 首屏拉取待复核队列；失败时会在顶部明确提示后端不可达
  await loadPending();
}

document.addEventListener("DOMContentLoaded", init);
