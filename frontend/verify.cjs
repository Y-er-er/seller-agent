/* ============================================================
   前端验证脚本（免构建 · 零依赖）

   用途：在没有浏览器工具的环境下，验证「前端逻辑 + 接口契约 + 渲染输出」三段是否连通。
   原理：在 Node 里 mock 一个最小 DOM + localStorage，用**真实 fetch** 打到本机后端，
        执行完整的 `init()`，再检查最终渲染出的 HTML。

   为什么需要它：
   - 契约错误（前端调的接口后端没有、event 无处理分支）**只靠读代码很难发现**；
   - 本脚本还把"后端不可达时必须明确报错、不得静默失败"作为一条硬断言。

   运行（需先启动后端）：
       node frontend/verify.cjs
   后端未启动时脚本会给出 SKIP 提示并返回 0，不视为失败。
   ============================================================ */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND_DIR = __dirname;
const APP_JS = path.join(FRONTEND_DIR, "app.js");
const INDEX_HTML = path.join(FRONTEND_DIR, "index.html");
const API_BASE = process.env.VERIFY_API_BASE || "http://127.0.0.1:8000";

const failures = [];
let renderCount = 0;

function check(name, ok, detail = "") {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (!ok && detail ? `  ${detail}` : ""));
  if (!ok) failures.push(`${name} ${detail}`.trim());
}

function makeSandbox() {
  const elements = {};
  const makeEl = () => ({
    _html: "",
    value: "",
    set innerHTML(v) { this._html = v; renderCount += 1; },
    get innerHTML() { return this._html; },
  });
  const sandbox = {
    console, URLSearchParams, Promise, JSON, Number, String, Array, Object, Math, Date, Error,
    fetch: (...args) => fetch(...args),
    localStorage: { getItem: () => null, setItem: () => {} },
    document: {
      addEventListener: () => {},
      getElementById: (id) => (elements[id] = elements[id] || makeEl()),
      querySelector: () => makeEl(),
    },
  };
  vm.createContext(sandbox);
  return { sandbox, elements };
}

/** 静态契约：前端调用的 API 路径必须都能在后端路由表里找到 */
function checkContract(source) {
  const calls = new Set();
  const re = /api\(\s*[`"]([^`"]+)[`"]/g;
  let m;
  while ((m = re.exec(source)) !== null) {
    const p = m[1].split("?")[0].replace(/\$\{[^}]+\}/g, "{param}");
    if (p.startsWith("/api")) calls.add(p);
  }

  const specs = [
    ["/api/requests", "POST"],
    ["/api/reviews/pending", "GET"],
    ["/api/review/{param}", "POST"],
    ["/api/audits", "GET"],
    ["/api/audits/stats", "GET"],
    ["/api/qa", "POST"],
    ["/api/qa/knowledge", "GET"],
    ["/api/dev/scenarios", "GET"],
    ["/api/dev/seed", "POST"],
  ];
  const known = new Set(specs.map(([p]) => p));
  const missing = [...calls].filter((c) => !known.has(c));
  check("接口路径均在已知路由表中", missing.length === 0, `未知=${missing}`);
  check("后端端点均被前端使用",
    specs.every(([p]) => calls.has(p)), `前端调用=${[...calls].sort()}`);
}

/** 静态契约：模板里的 data-action 必须都有处理分支 */
function checkActions(source, html) {
  const declared = new Set([...(source + html).matchAll(/data-action="([a-z-]+)"/g)].map((m) => m[1]));
  const handled = new Set([...source.matchAll(/action === "([a-z-]+)"/g)].map((m) => m[1]));
  const unhandled = [...declared].filter((a) => !handled.has(a));
  check("data-action 均有处理分支", unhandled.length === 0, `未处理=${unhandled}`);
}

async function main() {
  const source = fs.readFileSync(APP_JS, "utf8");
  const html = fs.readFileSync(INDEX_HTML, "utf8");

  check("HTML 已引用样式与脚本", html.includes("styles.css") && html.includes("app.js"));

  checkContract(source);
  checkActions(source, html);

  // ---------- 在线端到端 ----------
  const { sandbox, elements } = makeSandbox();
  vm.runInContext(source, sandbox);

  const required = ["init", "render", "renderPending", "renderAudits", "renderStats", "renderSubmit", "renderQa"];
  check("app.js 导出必要函数", required.every((f) => typeof sandbox[f] === "function"),
    `缺失=${required.filter((f) => typeof sandbox[f] !== "function")}`);

  await sandbox.init();
  const rendered = elements["app"] ? elements["app"].innerHTML : "";

  if (rendered.includes("无法连接后端")) {
    console.log(`SKIP：后端 ${API_BASE} 不可达，跳过在线渲染断言`);
    console.log("      请先启动：cd backend && python -m uvicorn app.main:app --port 8000");
  } else {
    check("首屏渲染出内容", rendered.length > 500, `${rendered.length} 字符`);
    const needles = [
      ["顶栏标题", "电商售后智能体 · 复核工作台"],
      ["后端在线状态", "后端在线"],
      ["标签页", "统计看板"],
      ["待复核面板", "待复核队列"],
    ];
    needles.forEach(([name, needle]) =>
      check(`渲染包含${name}`, rendered.includes(needle), `未找到「${needle}」`));

    // 待复核队列的渲染随**数据状态**变化（有人复核后即出队，队列可能为空），
    // 故不能固定断言"有复核按钮"——否则测试会随数据状态变得脆弱（曾真实误报）。
    // 正确断言：**二选一必居其一**——有数据则渲染按钮，无数据则渲染空态提示。
    const hasReviewBtn = rendered.includes('data-action="review"');
    const hasEmptyState = rendered.includes("队列为空");
    check("待复核队列渲染正确（有数据→按钮 / 空队列→提示）",
      hasReviewBtn || hasEmptyState,
      `既无复核按钮也无空态提示（按钮=${hasReviewBtn}，空态=${hasEmptyState}）`);

    // 命中明细必须对业务岗可读：不能把规则表里的字段代码与运算符代码直接呈现。
    // ⚠️ 不要依赖 `rendered` —— 待复核队列可能为空（复核后即出队），此时明细不存在，
    //    断言会被整体跳过、保护形同虚设。改为**构造样本直接调用渲染函数**，使断言始终生效。
    try {
      const sampleItem = {
        hit_details: [{
          rule_id: "R1",
          description: "金额闸门",
          leaves: [{ field: "claim_amount", op: "gte", threshold: 300, actual: 1200 }],
        }],
      };
      const detailHtml = sandbox.renderHitDetails(sampleItem);
      const leaked = ["gte", "lte", " eq ", "claim_amount", "refund_count_90d"]
        .filter((token) => detailHtml.includes(token));
      check("命中明细已中文化（构造样本，不依赖队列数据）", leaked.length === 0,
        `仍暴露：${leaked.join(", ")}`);
      check("命中明细使用中文运算符与字段名",
        detailHtml.includes("≥") && detailHtml.includes("申请退款金额"),
        `渲染片段：${detailHtml.replace(/<[^>]+>/g, " ").slice(0, 80)}`);
    } catch (err) {
      check("命中明细渲染（构造样本）", false, String(err && err.message));
    }

    // ---------- 口径问答视图 ----------
    // 注意：app.js 里 `loadKnowledge` 是 `const` 箭头函数，**不会**成为 vm 全局属性，
    // 故此处只调用函数声明式的 `renderQa()`（知识库未加载时渲染占位提示），
    // 口径库可用性改由下面的接口断言覆盖。
    try {
      const qaHtml = sandbox.renderQa();
      check("问答视图渲染出提问框", qaHtml.includes('id="f-qa-question"'));
      check("问答视图含提问按钮", qaHtml.includes('data-action="ask-qa"'));
      check("问答视图声明边界（不参与资格判定）", qaHtml.includes("不会改变任何判定结论"));
      check("问答视图含可选受理号输入", qaHtml.includes('id="f-qa-order"'));
      check("问答视图含建议区（口径标题由后端驱动）",
        qaHtml.includes("口径库") || qaHtml.includes('data-action="qa-suggest"'));
    } catch (err) {
      check("问答视图渲染", false, String(err && err.message));
    }

    // 真实调用问答接口：确认零模型调用 + 边界声明 + 口径库可读
    try {
      const kbResp = await fetch(`${API_BASE}/api/qa/knowledge`);
      const kb = await kbResp.json();
      check("口径知识库接口可读", kbResp.status === 200 && (kb.total || 0) >= 10,
        `HTTP ${kbResp.status} total=${kb.total}`);

      const resp = await fetch(`${API_BASE}/api/qa`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: "质量问题为什么不自动判定？" }),
      });
      const data = await resp.json();
      check("问答接口返回 200", resp.status === 200, `HTTP ${resp.status}`);
      check("问答结果声明不参与判定",
        typeof data.boundary === "string" && data.boundary.includes("不参与资格判定"),
        `boundary=${data.boundary}`);
      check("问答默认不调用模型（零成本）", data.model_used === false, `model_used=${data.model_used}`);
      check("问答命中质量红线口径",
        (data.sources || []).some((s) => s.id === "KB-QUALITY-ALWAYS-HUMAN"),
        `sources=${JSON.stringify(data.sources)}`);
    } catch (err) {
      check("问答接口在线调用", false, String(err && err.message));
    }
  }

  // ---------- 离线降级 ----------
  const bad = makeSandbox();
  vm.runInContext(source.replace(/"http:\/\/127\.0\.0\.1:8000"/, '"http://127.0.0.1:9"'), bad.sandbox);
  await bad.sandbox.init();
  const badHtml = bad.elements["app"].innerHTML;
  check("后端不可达时明确报错（不静默失败）",
    badHtml.includes("无法连接后端") && badHtml.includes("后端离线"));

  console.log("-".repeat(64));
  if (failures.length) {
    console.log(`结果：${failures.length} 项未通过`);
    failures.forEach((f) => console.log(`  - ${f}`));
    process.exit(1);
  }
  console.log("结果：全部通过");
}

main().catch((err) => {
  console.error("验证脚本异常：", err);
  process.exit(1);
});
