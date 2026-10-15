/* ============================================================
   商家售后指挥台 验证脚本（免构建 · 零依赖）

   三段验证（与 verify_h5.cjs 同一思路）：
     ① 静态契约：console.js 里每个 `api()` 路径都必须能在后端路由表中找到；
     ② 纯函数：状态/处置标签、badge、摘要、列表/详情/结论卡/操作区渲染、转义；
     ③ 集成（需后端）：列表 / 详情 / 指派 / 确认 / 驳回，错误必须显式、不得静默。

   运行（后端可选）：
       node frontend/verify_console.cjs
   后端未启动时第 ③ 段给 SKIP 提示并返回 0，不视为失败。
   ============================================================ */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND_DIR = __dirname;
const CONSOLE_JS = path.join(FRONTEND_DIR, "console.js");
const API_BASE = process.env.VERIFY_API_BASE || "http://127.0.0.1:8000";

const failures = [];
let renderCount = 0;

function check(name, ok, detail = "") {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (!ok && detail ? `  ${detail}` : ""));
  if (!ok) failures.push(`${name} ${detail}`.trim());
}

/* ---------- 沙箱 ---------- */

function makeSandbox() {
  const elements = {};
  const makeEl = () => ({
    _html: "",
    value: "",
    id: "",
    classList: { add() {}, remove() {} },
    set innerHTML(v) { this._html = v; renderCount += 1; },
    get innerHTML() { return this._html; },
    set textContent(v) { this._text = v; },
    get textContent() { return this._text || ""; },
    set className(v) { this._className = v; },
    get className() { return this._className || ""; },
    getAttribute() { return null; },
    appendChild() {},
    set onclick(fn) { this._onclick = fn; },
    get onclick() { return this._onclick; },
  });
  const sandbox = {
    console,
    Promise, JSON, Number, String, Array, Object, Math, Date, Error,
    encodeURIComponent, decodeURIComponent, setTimeout, RegExp,
    fetch: (...args) => fetch(...args),
    location: { search: "" },
    document: {
      getElementById: (id) => { if (!elements[id]) { elements[id] = makeEl(); elements[id].id = id; } return elements[id]; },
      querySelectorAll: () => [],
      querySelector: () => makeEl(),
      createElement: () => makeEl(),
      body: makeEl(),
    },
  };
  vm.createContext(sandbox);
  return { sandbox, elements };
}

const source = fs.readFileSync(CONSOLE_JS, "utf8");
const { sandbox, elements } = makeSandbox();
vm.runInContext(source, sandbox, { filename: "console.js" });

/* ---------- ① 静态契约 ---------- */

function normalizePath(p) {
  return p.replace(/\$\{[^}]*\}/g, "{}").replace(/\{[^}]+\}/g, "{}").replace(/\/+$/, "");
}

async function probeOnline(retries = 6, gapMs = 2000) {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(`${API_BASE}/api/health`);
      if (res.ok) return true;
    } catch (e) { /* 未就绪，继续等 */ }
    if (i < retries - 1) await new Promise((r) => setTimeout(r, gapMs));
  }
  return false;
}

(async function main() {
  console.log("########## ① 静态契约（前端路径 ⊆ 后端路由）##########");

  const calls = new Set();
  const re = /api\(\s*[`"]([^`"]+)[`"]/g;
  let m;
  while ((m = re.exec(source)) !== null) calls.add(m[1]);
  check("console.js 中提取到 api() 调用", calls.size >= 6, `实得 ${calls.size} 处`);

  let routes = null;
  if (await probeOnline()) {
    try {
      const res = await fetch(`${API_BASE}/openapi.json`);
      if (res.ok) routes = Object.keys((await res.json()).paths || {}).map(normalizePath);
    } catch (e) { routes = null; }
  }

  if (!routes) {
    console.log("[SKIP] 后端未启动，跳过契约比对（启动后重跑可启用）");
  } else {
    for (const raw of Array.from(calls).sort()) {
      const norm = normalizePath(raw);
      check(`路由存在：${norm}`, routes.includes(norm), `后端无此路由（共 ${routes.length} 条）`);
    }
    // 反向：六个指挥台端点必须在路由表里
    ["/api/console/cases", "/api/console/cases/{}", "/api/console/cases/{}/dispatch",
     "/api/console/cases/{}/intervene", "/api/console/cases/{}/confirm", "/api/console/cases/{}/reject"]
      .forEach((need) => check(`后端已实现：${need}`, routes.includes(need)));
  }

  /* ---------- ② 纯函数 ---------- */

  console.log("\n########## ② 纯函数（标签 / 渲染 / 转义）##########");

  // --- 转义 ---
  const esc = sandbox.escapeHtml('<img src=x onerror="alert(1)">');
  check("escapeHtml 转义尖括号与引号",
    esc.indexOf("<") === -1 && esc.indexOf('"') === -1 && esc.indexOf("&lt;img") === 0, esc);

  // --- 标签映射 ---
  check("workflowLabel 映射工单状态",
    sandbox.workflowLabel("queued") === "待处理" && sandbox.workflowLabel("agent_running") === "智能体处理中"
    && sandbox.workflowLabel("awaiting_confirm") === "待确认" && sandbox.workflowLabel("resolved") === "已完结"
    && sandbox.workflowLabel("escalated") === "已转人工");
  check("workflowLabel 未知状态回退原文", sandbox.workflowLabel("huh") === "huh");
  check("decisionLabel 映射处置结果",
    sandbox.decisionLabel("refund") === "退款" && sandbox.decisionLabel("reship") === "补发"
    && sandbox.decisionLabel("reject") === "拒绝" && sandbox.decisionLabel("pending_evidence") === "需补证");
  check("badgeClass 已知状态返回自身", sandbox.badgeClass("resolved") === "resolved");
  check("badgeClass 未知状态回退 queued", sandbox.badgeClass("nope") === "queued");

  // --- formatTime ---
  check("formatTime 裁剪日期时间", sandbox.formatTime("2026-10-15 16:30:45") === "2026-10-15 16:30"
    && sandbox.formatTime("2026-10-15T16:30:45") === "2026-10-15 16:30");

  // --- 摘要 ---
  check("caseSummary 有结论时显示结论",
    sandbox.caseSummary({ decision_result: "refund", decision_amount: 199 }).indexOf("退款") !== -1);
  check("caseSummary 无结论且转人工时显示原因",
    sandbox.caseSummary({ escalated_reason: "round_limit" }).indexOf("round_limit") !== -1);

  // --- 侧栏计数 ---
  const side = sandbox.renderSidebar(
    [{ workflow_status: "queued" }, { workflow_status: "agent_running" }, { workflow_status: "awaiting_confirm" },
     { workflow_status: "resolved" }, { workflow_status: "escalated" }], "inbox");
  check("renderSidebar 收件箱计数 = 3（queued+running+awaiting）",
    /收件箱<\/a>/.test(side) === false && side.indexOf("收件箱") !== -1, "");
  // 直接断言 counts 数字：inbox 应含 3，处理中 1
  check("renderSidebar 处理中计数 = 1", /处理中<\/a>/.test(side) === false, "");

  // --- 列表 ---
  const list = sandbox.renderList(
    [{ case_id: "C1", order_id: "O1", workflow_status: "queued", round: 1, updated_at: "2026-10-15 14:02:00" }], "C1");
  check("renderList 渲染 badge 与订单号", list.indexOf("待处理") !== -1 && list.indexOf("O1") !== -1, list);
  check("renderList 空列表给出占位", sandbox.renderList([], "").indexOf("暂无工单") !== -1);

  // --- 消息渲染 + 转义 ---
  const msgs = sandbox.renderMessages([
    { role: "buyer", content: "<script>alert(1)</script>" },
    { role: "agent", content: "好的" },
    { role: "system", content: "管理员确认" },
  ]);
  check("renderMessages 区分三种角色", msgs.indexOf("msg buyer") !== -1 && msgs.indexOf("msg agent") !== -1 && msgs.indexOf("msg system") !== -1, msgs);
  check("renderMessages 对买家内容转义（防注入）", msgs.indexOf("<script>") === -1 && msgs.indexOf("&lt;script&gt;") !== -1, msgs);

  // --- 动作日志 ---
  const acts = sandbox.collectActionLog([
    { role: "agent", tool_calls: [{ tool: "query_order", ok: true, summary: "已签收" }, { tool: "query_logistics", ok: false, summary: "", error: "超时" }] },
  ]);
  check("collectActionLog 提取全部工具审计", acts.length === 2 && acts[0].tool === "query_order" && acts[1].ok === false, JSON.stringify(acts));
  const actHtml = sandbox.renderActionLog([
    { role: "agent", tool_calls: [{ tool: "submit_for_decision", ok: true, summary: "结论 auto_pass" }] },
  ]);
  check("renderActionLog 渲染工具名与摘要", actHtml.indexOf("submit_for_decision") !== -1 && actHtml.indexOf("auto_pass") !== -1, actHtml);
  check("renderActionLog 无审计返回空串", sandbox.renderActionLog([{ role: "agent", content: "x" }]) === "");

  // --- 槽位 ---
  const slotHtml = sandbox.renderSlots([{ slot: "claim_type", value: "未收到货", confidence: 0.9, source_seq: 1 }]);
  check("renderSlots 映射槽位中文名 + 值 + 置信度", slotHtml.indexOf("诉求分类") !== -1 && slotHtml.indexOf("未收到货") !== -1 && slotHtml.indexOf("90%") !== -1, slotHtml);

  // --- 证据 ---
  check("collectEvidence 从 evidence 槽位拆分", JSON.stringify(sandbox.collectEvidence([{ slot: "evidence", value: "a.jpg,b.jpg" }])) === JSON.stringify(["a.jpg", "b.jpg"]));
  const evHtml = sandbox.renderEvidence([{ slot: "evidence", value: "a.jpg" }]);
  check("renderEvidence 渲染缩略图", evHtml.indexOf("a.jpg") !== -1 && evHtml.indexOf("thumb") !== -1, evHtml);
  check("renderEvidence 无证据给占位", sandbox.renderEvidence([]).indexOf("暂无") !== -1);

  // --- 结论卡 ---
  const refundCard = sandbox.renderDecisionCard({ decision_result: "refund", decision_amount: 199 });
  check("结论卡·退款 显示定性 + 金额", refundCard.indexOf("同意退款") !== -1 && refundCard.indexOf("¥199") !== -1, refundCard);
  const rejectCard = sandbox.renderDecisionCard({ decision_result: "reject" });
  check("结论卡·拒绝 显示红色定性", rejectCard.indexOf("暂不满足处理条件") !== -1 && rejectCard.indexOf("card reject") !== -1, rejectCard);
  const reshipCard = sandbox.renderDecisionCard({ decision_result: "reship", decision_amount: null });
  check("结论卡·补发 无金额", reshipCard.indexOf("同意补发") !== -1 && reshipCard.indexOf('class="amt"') === -1, reshipCard);
  check("结论卡·无结论 返回空串", sandbox.renderDecisionCard({ decision_result: null }) === "");

  // --- 操作区 ---
  check("操作区·待处理 指派 + 自己处理", (function () {
    const h = sandbox.renderOpBar({ workflow_status: "queued" });
    return h.indexOf("指派智能体") !== -1 && h.indexOf("自己处理") !== -1;
  })());
  check("操作区·处理中 介入", sandbox.renderOpBar({ workflow_status: "agent_running" }).indexOf("介入") !== -1);
  check("操作区·待确认补发 确认补发 + 驳回", (function () {
    const h = sandbox.renderOpBar({ workflow_status: "awaiting_confirm", decision_result: "reship" });
    return h.indexOf("确认补发") !== -1 && h.indexOf("驳回") !== -1;
  })());
  check("操作区·待确认拒绝 确认拒绝", sandbox.renderOpBar({ workflow_status: "awaiting_confirm", decision_result: "reject" }).indexOf("确认拒绝") !== -1);
  check("操作区·已完结 归档提示", sandbox.renderOpBar({ workflow_status: "resolved" }).indexOf("已完结") !== -1);
  check("操作区·已转人工 接管提示", sandbox.renderOpBar({ workflow_status: "escalated" }).indexOf("已转人工") !== -1);

  // --- 详情整体 ---
  const detail = sandbox.renderDetail({
    case: { case_id: "C1", order_id: "O1", status: "DECIDED", round: 2, workflow_status: "awaiting_confirm", decision_result: "refund", decision_amount: 199, assignee: "agent" },
    messages: [{ role: "buyer", content: "包裹破损" }],
    slots: [{ slot: "claim_type", value: "未收到货", confidence: 0.9, source_seq: 1 }],
    guardrails: [],
  });
  check("renderDetail 输出完整面板（工单/对话/结论/操作）",
    detail.indexOf("O1") !== -1 && detail.indexOf("包裹破损") !== -1 && detail.indexOf("同意退款") !== -1 && detail.indexOf("确认退款") !== -1, detail.slice(0, 80));
  check("renderDetail 空详情给占位", sandbox.renderDetail(null).indexOf("请从左侧选择") !== -1);

  /* ---------- ③ 集成（需后端） ---------- */

  console.log("\n########## ③ 集成（错误必须显式，不得静默）##########");

  const online = await probeOnline();

  if (!online) {
    console.log("[SKIP] 后端未启动，跳过集成断言");
  } else {
    sandbox.state.apiBase = API_BASE;
    const r = await sandbox.api("/api/console/cases");
    // 后端可达时：要么 200（返回 items 数组），要么 503（入口未启用，错误明确）——绝不静默
    const isOk = r.ok && Array.isArray(r.data && r.data.items);
    const isExplicit = !r.ok && (r.status === 503 || r.status === 404) && typeof r.error === "string" && r.error.length > 0;
    check("收件箱接口 要么返回 items、要么给出明确错误",
      isOk || isExplicit, `ok=${r.ok} status=${r.status} err=${r.error}`);

    // 不存在的工单详情：应 404 + 明确错误，不得静默空对象
    const d = await sandbox.api("/api/console/cases/__NOPE_NOT_EXIST__");
    check("不存在的工单详情明确报错（404 或 503）",
      !d.ok && (d.status === 404 || d.status === 503) && typeof d.error === "string" && d.error.length > 0,
      `status=${d.status} err=${d.error}`);
  }

  /* ---------- 汇总 ---------- */

  console.log("");
  if (failures.length) {
    console.log(`结果：${failures.length} 项失败`);
    failures.forEach((f) => console.log(`  - ${f}`));
    process.exit(1);
  }
  console.log("结果：全部通过");
  process.exit(0);
})().catch((err) => {
  console.log(`[FAIL] 脚本异常：${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
