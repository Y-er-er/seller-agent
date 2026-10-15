/* ============================================================
   H5 对话页验证脚本（免构建 · 零依赖）

   三段验证（与 verify.cjs 同一思路）：
     ① 静态契约：h5.js 里每个 `api()` 路径都必须能在后端路由表中找到；
     ② 纯函数：SSE 解析 / 渲染 / 转义 —— 不需要浏览器也不需要后端；
     ③ 集成（需后端）：后端不可达、入口未启用时必须**明确报错**，不得静默。

   运行（后端可选）：
       node frontend/verify_h5.cjs
   后端未启动时第 ③ 段给 SKIP 提示并返回 0，不视为失败。
   ============================================================ */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND_DIR = __dirname;
const H5_JS = path.join(FRONTEND_DIR, "h5.js");
const API_BASE = process.env.VERIFY_API_BASE || "http://127.0.0.1:8000";

const failures = [];
let renderCount = 0;

function check(name, ok, detail = "") {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (!ok && detail ? `  ${detail}` : ""));
  if (!ok) failures.push(`${name} ${detail}`.trim());
}

/* ---------- 沙箱（不定义 window，避免脚本自动 init） ---------- */

function makeSandbox() {
  const elements = {};
  const handlers = {};
  const makeEl = () => ({
    _html: "",
    value: "",
    files: [],
    id: "",
    classList: { add() {}, remove() {} },
    set innerHTML(v) { this._html = v; renderCount += 1; },
    get innerHTML() { return this._html; },
    click() { this._clicked = true; },
    getAttribute() { return null; },
  });
  const sandbox = {
    console,
    Promise, JSON, Number, String, Array, Object, Math, Date, Error,
    encodeURIComponent, URLSearchParams, TextDecoder, FormData, setTimeout,
    fetch: (...args) => fetch(...args),
    document: {
      addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
      getElementById: (id) => {
        if (!elements[id]) { elements[id] = makeEl(); elements[id].id = id; }
        return elements[id];
      },
      querySelector: () => makeEl(),
    },
  };
  vm.createContext(sandbox);
  return { sandbox, elements, handlers };
}

const source = fs.readFileSync(H5_JS, "utf8");
const { sandbox, elements } = makeSandbox();
vm.runInContext(source, sandbox, { filename: "h5.js" });

/* ---------- ① 静态契约 ---------- */

function normalizePath(p) {
  // `/api/h5/cases/${encodeURIComponent(state.caseId)}/messages` → `/api/h5/cases/{}` + `/messages`
  return p.replace(/\$\{[^}]*\}/g, "{}").replace(/\{[^}]+\}/g, "{}").replace(/\/+$/, "");
}

/* ---------- 后端就绪探测（带重试：服务刚启动时健康检查可能尚未就绪） ---------- */

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
  check("h5.js 中提取到 api() 调用", calls.size >= 5, `实得 ${calls.size} 处`);

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
    // 反向：这三个 P1.3 端点必须在路由表里（防止后端漏掉实现）
    ["/api/h5/cases/{}/attachments", "/api/h5/cases/{}/escalate", "/api/h5/cases/{}/messages/stream"]
      .forEach((need) => check(`后端已实现：${need}`, routes.includes(need)));
  }

  /* ---------- ② 纯函数 ---------- */

  console.log("\n########## ② 纯函数（SSE 解析 / 渲染 / 转义）##########");

  // --- 转义（XSS 防护：买家输入直接进 HTML）---
  const esc = sandbox.escapeHtml('<img src=x onerror="alert(1)">');
  check("escapeHtml 转义尖括号与引号",
    esc.indexOf("<") === -1 && esc.indexOf('"') === -1 && esc.indexOf("&lt;img") === 0, esc);

  // --- SSE 解析 ---
  const one = sandbox.parseSSEChunk('event: meta\ndata: {"round":2}\n\n');
  check("parseSSEChunk 解析单个完整帧", one.events.length === 1 && one.events[0].event === "meta"
    && one.events[0].data.round === 2, JSON.stringify(one.events));
  check("parseSSEChunk 完整帧后无残留", one.rest === "", JSON.stringify(one.rest));

  const partial = sandbox.parseSSEChunk('event: delta\ndata: {"text":"你');
  check("parseSSEChunk 半截数据不解析、留作残留",
    partial.events.length === 0 && partial.rest.indexOf("event: delta") === 0, JSON.stringify(partial));

  const split = sandbox.parseSSEChunk('{"text":"好"}\n\nevent: done\ndata: {"chars":3}\n\n');
  check("parseSSEChunk 处理多帧", split.events.length === 2 && split.events[1].event === "done");

  const badJson = sandbox.parseSSEChunk("event: x\ndata: 不是JSON\n\n");
  check("parseSSEChunk 非 JSON data 降级为 raw、不抛异常",
    badJson.events.length === 1 && badJson.events[0].data.raw === "不是JSON", JSON.stringify(badJson.events));

  // --- consumeSSE：走 text() 分支（无 body.getReader 的桩响应）---
  const sseBody = 'event: status\ndata: {"message":"处理中"}\n\n' +
                  'event: delta\ndata: {"text":"你"}\n\nevent: delta\ndata: {"text":"好"}\n\n' +
                  'event: done\ndata: {"chars":2}\n\n';
  const seen = [];
  await sandbox.consumeSSE({ text: async () => sseBody }, (ev) => seen.push(ev.event));
  check("consumeSSE 顺序派发全部事件",
    seen.join(",") === "status,delta,delta,done", seen.join(","));

  // --- 渲染 ---
  const card = sandbox.renderOrderCard({ order_id: "O1", item_title: "蓝牙耳机", pay_amount: 199, status_text: "已签收" });
  check("renderOrderCard 输出订单信息", card.indexOf("O1") !== -1 && card.indexOf("蓝牙耳机") !== -1
    && card.indexOf("199") !== -1, card);
  check("renderOrderCard 空数据不产生空卡片", sandbox.renderOrderCard(null) === "");

  const msgs = sandbox.renderMessages([
    { role: "agent", content: "你好" },
    { role: "buyer", content: "包裹破损" },
  ]);
  check("renderMessages 渲染双方气泡", msgs.indexOf("msg agent") !== -1 && msgs.indexOf("msg buyer") !== -1, msgs);

  const xss = sandbox.renderMessages([{ role: "buyer", content: "<script>alert(1)</script>" }]);
  check("renderMessages 对买家内容转义（防注入）", xss.indexOf("<script>") === -1 && xss.indexOf("&lt;script&gt;") !== -1, xss);

  const streaming = sandbox.renderMessages([{ role: "agent", content: "处理", streaming: true }]);
  check("流式中气泡带光标类", streaming.indexOf("caret") !== -1, streaming);

  const tips = sandbox.renderTips({ overLengthWarned: true, escalated: true, gate: { paused: true, message: "已暂停" }, lastError: "" });
  check("renderTips 同时渲染超长 / 暂停 / 转人工",
    tips.indexOf("超过 200 字") !== -1 && tips.indexOf("已暂停") !== -1 && tips.indexOf("转人工客服") !== -1, tips);
  check("renderTips 无异常时返回空串", sandbox.renderTips({ lastError: "" }) === "");

  const errTip = sandbox.renderTips({ lastError: "无法连接后端" });
  check("renderTips 渲染错误为 danger 级", errTip.indexOf("tip danger") !== -1, errTip);

  const bar20 = sandbox.renderRoundBar(20, 20);
  check("轮数用满时进度条标红", bar20.indexOf("is-full") !== -1, bar20);
  const bar18 = sandbox.renderRoundBar(18, 20);
  check("接近上限时进度条预警", bar18.indexOf("is-warn") !== -1, bar18);

  const composer = sandbox.renderComposer({ sending: false, escalated: false, ended: false, attachments: ["a.png"] });
  check("renderComposer 含输入框 / 发送 / 上传 / 转人工",
    composer.indexOf('id="h5-input"') !== -1 && composer.indexOf('id="h5-send"') !== -1
    && composer.indexOf('id="h5-upload"') !== -1 && composer.indexOf('id="h5-escalate"') !== -1, composer);
  check("renderComposer 展示已上传材料条", composer.indexOf("a.png") !== -1);

  const lockedComposer = sandbox.renderComposer({ sending: false, escalated: true, ended: false, attachments: [] });
  check("已转人工时输入与发送被禁用",
    lockedComposer.indexOf('id="h5-input" rows="1" placeholder="已转人工，输入已关闭" disabled') !== -1, lockedComposer);

  const amountHtml = sandbox.renderAmountCard({ label: "建议退款金额", value: 50 });
  check("renderAmountCard 输出金额卡片", amountHtml.indexOf("¥50") !== -1 && amountHtml.indexOf("amount-card") !== -1, amountHtml);

  const basisHtml = sandbox.renderBasis(["命中规则：R2", "结论：通过"]);
  check("renderBasis 默认折叠、含两条依据",
    basisHtml.indexOf("<details") !== -1 && basisHtml.indexOf("R2") !== -1, basisHtml);
  check("renderBasis 空数组不渲染", sandbox.renderBasis([]) === "");

  // --- 全量渲染 ---
  sandbox.state.caseId = "O1";
  sandbox.state.round = 3;
  sandbox.state.messages = [{ role: "agent", content: "你好" }];
  const html = sandbox.render();
  check("render() 输出完整骨架（顶栏 / 消息区 / 输入区）",
    html.indexOf("h5-header") !== -1 && html.indexOf("h5-messages") !== -1 && html.indexOf("composer") !== -1, "");
  check("render() 把内容写进容器", elements["h5"] && elements["h5"].innerHTML.indexOf("h5-header") !== -1);
  check("render() 显示轮次进度", html.indexOf("第 3/20 轮") !== -1, "");

  /* ---------- ③ 集成（需后端） ---------- */

  console.log("\n########## ③ 集成（错误必须显式，不得静默）##########");

  const online = await probeOnline();

  if (!online) {
    console.log("[SKIP] 后端未启动，跳过集成断言");
  } else {
    // 后端可达时：入口未启用 → 必须拿到明确的 503 与原因（而不是 404/静默）
    sandbox.state.apiBase = API_BASE;
    sandbox.state.caseId = "";
    sandbox.state.lastError = "";
    const created = await sandbox.createCase("VERIFY_H5_ORDER");
    check("入口未启用时 createCase 返回 false（不误报成功）", created === false);
    check("入口未启用时给出明确错误文案",
      typeof sandbox.state.lastError === "string" && sandbox.state.lastError.length > 0
      && (sandbox.state.lastError.indexOf("入口") !== -1 || sandbox.state.lastError.indexOf("不可用") !== -1),
      sandbox.state.lastError);

    // 三个 P1.3 端点：关闭状态下应 503（证明路由存在且受开关控制），不得 404
    // ⚠️ 上传必须传真正的 Blob/File——`FormData.append` 不接受普通对象（测试自身曾因此报错）
    const fakeFile = new File([Buffer.from("fake-image-bytes")], "x.png", { type: "image/png" });
    for (const [label, fn] of [
      ["attachments", () => sandbox.uploadAttachment(fakeFile)],
      ["escalate", () => sandbox.escalate()],
      ["stream", () => sandbox.sendMessage("测试", { stream: true })],
    ]) {
      sandbox.state.lastError = "";
      sandbox.state.caseId = "VERIFY_H5_ORDER";
      sandbox.state.sending = false;
      sandbox.state.escalated = false;
      sandbox.state.ended = false;
      sandbox.state.messages = [];
      const res = await fn();
      check(`P1.3 端点 ${label} 拒绝时给出明确提示（非静默）`,
        res === false && sandbox.state.lastError.length > 0, `res=${res} err=${sandbox.state.lastError}`);
    }
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
