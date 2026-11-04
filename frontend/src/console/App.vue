<script setup>
/**
 * 售后指挥台 —— Vue 3 版本。
 *
 * 与原 console.js 的行为契约保持一致：
 *  - 单一后端出口（shared/api.js），失败给出明确文案；
 *  - 人始终在环：指派 / 介入 / 确认 / 驳回 / 接管 / 退回 / 结案全部交后端裁决；
 *  - 每 2 秒轮询列表与详情；只有详情内容签名变化才替换详情，避免打断输入；
 *  - 人工发言草稿独立于详情数据，刷新不丢；
 *  - 工单可关联商品，点击时通过 postMessage 请外层卖家控制台打开。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { request } from '../shared/api'

const DEFAULT_API_BASE = 'http://127.0.0.1:8000'
const LIVE_INTERVAL_MS = 2000

const WF_LABEL = {
  queued: '待处理',
  agent_running: '智能体处理中',
  awaiting_confirm: '待确认',
  resolved: '已完结',
  escalated: '已转人工',
}

const DECISION_LABEL = {
  refund: '退款',
  reship: '补发',
  reject: '拒绝',
  manual: '转人工',
  pending_evidence: '需补证',
}

const SLOT_LABEL = {
  issue_reason: '问题原因',
  issue_object: '问题对象',
  claim_direction: '诉求方向',
  symptom: '问题现象',
  affected_qty: '影响数量',
  signed_days: '签收后时长',
  evidence: '举证材料',
  note: '补充说明',
}

const state = reactive({
  apiBase: DEFAULT_API_BASE,
  filter: 'inbox',
  cases: [],
  selectedId: '',
  detail: null,
  detailSig: '',
  live: true,
  lastSync: '',
  modelAvailable: null,
  busy: false,
  lastError: '',
  conn: 'connecting',
})

const composerDraft = ref('')
const lightbox = ref('')
const convEl = ref(null)
let liveTimer = null

const api = (path, options) => request(state.apiBase, path, options)

/* ---------- 列表与计数 ---------- */

const counts = computed(() => {
  const c = { inbox: 0, agent_running: 0, awaiting_confirm: 0, resolved: 0, escalated: 0 }
  state.cases.forEach((x) => {
    const ws = x.workflow_status || ''
    if (c[ws] !== undefined) c[ws]++
    if (ws === 'queued' || ws === 'agent_running' || ws === 'awaiting_confirm') c.inbox++
  })
  return c
})

const navItems = computed(() => [
  { key: 'inbox', label: '收件箱', count: counts.value.inbox },
  { key: 'agent_running', label: '处理中', count: counts.value.agent_running },
  { key: 'awaiting_confirm', label: '待确认', count: counts.value.awaiting_confirm },
  { key: 'resolved', label: '已完结', count: counts.value.resolved },
  { key: 'escalated', label: '已转人工', count: counts.value.escalated },
])

const filteredCases = computed(() => {
  if (state.filter === 'inbox') {
    return state.cases.filter(
      (c) =>
        c.workflow_status === 'queued' ||
        c.workflow_status === 'agent_running' ||
        c.workflow_status === 'awaiting_confirm'
    )
  }
  return state.cases.filter((c) => c.workflow_status === state.filter)
})

/* ---------- 详情派生 ---------- */

const detailCase = computed(() => (state.detail && state.detail.case) || null)
const detailMessages = computed(() => (state.detail && state.detail.messages) || [])
const ws = computed(() => (detailCase.value && detailCase.value.workflow_status) || 'queued')
const buyerAsked = computed(
  () => ((detailCase.value && detailCase.value.escalated_reason) || '') === 'buyer_request'
)

const detailSub = computed(() => {
  const c = detailCase.value
  if (!c) return ''
  const sub = []
  if (c.decision_result && DECISION_LABEL[c.decision_result]) sub.push(DECISION_LABEL[c.decision_result])
  sub.push('轮 ' + (c.round || 0))
  return sub.join(' · ')
})

const factsRows = computed(() => {
  const c = detailCase.value
  if (!c) return []
  return [
    ['订单号', c.order_id || c.case_id || ''],
    ['会话状态', c.status || ''],
    ['已用轮次', c.round != null ? String(c.round) : '0'],
    ['处理者', c.assignee === 'agent' ? '智能体' : c.assignee === 'manual' ? '管理员' : '—'],
  ]
})

const evidenceNames = computed(() => {
  const names = []
  const slots = (state.detail && state.detail.slots) || []
  slots.forEach((s) => {
    if (s.slot === 'evidence' && s.value) {
      String(s.value)
        .split(',')
        .forEach((n) => {
          const t = n.trim()
          if (t) names.push(t)
        })
    }
  })
  return names
})

const slotRows = computed(() =>
  ((state.detail && state.detail.slots) || []).map((s) => {
    const key = s.slot || ''
    const conf =
      s.confidence != null ? ` · 置信度 ${Math.round(Number(s.confidence) * 100)}%` : ''
    return {
      key,
      label: SLOT_LABEL[key] || key,
      value: s.value || '—',
      meta: (s.source_seq != null ? `来源轮 ${s.source_seq}` : '') + conf,
    }
  })
)

const actionLog = computed(() => {
  const acts = []
  detailMessages.value.forEach((m) => {
    const calls = m.tool_calls || []
    if (!Array.isArray(calls)) return
    calls.forEach((a) => {
      acts.push({
        tool: a.tool || '',
        ok: a.ok !== false,
        summary: a.summary || a.error || '',
      })
    })
  })
  return acts
})

const decision = computed(() => {
  const c = detailCase.value
  if (!c || !c.decision_result || !DECISION_LABEL[c.decision_result]) return null
  const dr = c.decision_result
  const cls =
    dr === 'reject'
      ? 'reject'
      : dr === 'manual' || dr === 'pending_evidence'
        ? dr
        : dr === 'reship'
          ? 'reship'
          : ''
  const icon = { refund: '✅', reship: '📦', reject: '⛔', manual: '🙋', pending_evidence: '📎' }[dr] || 'ℹ️'
  const title =
    {
      refund: '同意退款',
      reship: '同意补发',
      reject: '暂不满足处理条件',
      manual: '转人工核实',
      pending_evidence: '需补充举证',
    }[dr] || DECISION_LABEL[dr]
  return {
    dr,
    cls,
    icon,
    title,
    amount: c.decision_amount,
    confirmed_by: c.confirmed_by,
    confirmed_at: c.confirmed_at,
  }
})

const canCompose = computed(() => ws.value !== 'resolved')

/* ---------- 展示辅助 ---------- */

const connText = computed(() =>
  state.conn === 'online' ? '已连接' : state.conn === 'offline' ? '后端不可达' : '连接中…'
)

const liveText = computed(() =>
  state.live ? `实时刷新中${state.lastSync ? `（${state.lastSync}）` : ''}` : '实时刷新已暂停'
)

function workflowLabel(v) {
  return WF_LABEL[v] || v || '未知'
}

function badgeClass(v) {
  return WF_LABEL[v] ? v : 'queued'
}

function formatTime(v) {
  if (!v) return ''
  return String(v).replace('T', ' ').slice(0, 16)
}

function caseSummary(c) {
  if (c.decision_result && DECISION_LABEL[c.decision_result]) {
    return '结论：' + DECISION_LABEL[c.decision_result] + (c.decision_amount != null ? ' ¥' + c.decision_amount : '')
  }
  if (c.assignee === 'manual') return '管理员处理中'
  if (c.escalated_reason) return '转人工：' + c.escalated_reason
  return '待处理'
}

function cid(c) {
  return c.case_id || c.order_id || ''
}

function msgRole(m) {
  if (m.role === 'buyer') return 'buyer'
  if (m.role === 'system') return 'system'
  if (m.role === 'human') return 'human'
  return 'agent'
}

function msgMeta(m) {
  const role = msgRole(m)
  const label = role === 'buyer' ? '买家' : role === 'system' ? '' : role === 'human' ? '人工客服' : '智能体'
  return (label ? label + ' · ' : '') + formatTime(m.created_at)
}

/* ---------- 数据加载 ---------- */

async function loadHealth() {
  const r = await api('/api/health')
  if (r.ok && r.data && r.data.llm) {
    state.modelAvailable = !!r.data.llm.text_available
  }
}

async function loadList() {
  const r = await api('/api/console/cases')
  if (!r.ok) {
    state.conn = 'offline'
    state.lastError = r.error
    return
  }
  state.conn = 'online'
  state.lastError = ''
  state.cases = (r.data && r.data.items) || []
}

async function loadDetail(caseId) {
  state.selectedId = caseId
  const r = await api(`/api/console/cases/${encodeURIComponent(caseId)}`)
  if (!r.ok) {
    state.lastError = r.error
    return
  }
  state.lastError = ''
  state.detail = r.data
  state.detailSig = signature(r.data)
  await nextTick()
  scrollConversationToEnd()
}

/* ---------- 动作 ---------- */

async function doAction(op) {
  if (!state.selectedId || state.busy) return
  state.busy = true
  const caseId = state.selectedId
  let r

  if (op === 'dispatch') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/dispatch`, { method: 'POST' })
  } else if (op === 'intervene') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/intervene`, { method: 'POST' })
  } else if (op === 'confirm') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/confirm`, {
      method: 'POST',
      body: { reviewer: 'admin' },
    })
  } else if (op === 'reject') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/reject`, {
      method: 'POST',
      body: { reason: '管理员驳回判定' },
    })
  } else if (op === 'takeover') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/takeover`, { method: 'POST' })
  } else if (op === 'resume') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/resume`, { method: 'POST' })
  } else if (op === 'close') {
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/close`, {
      method: 'POST',
      body: { note: '管理员人工结案' },
    })
  } else if (op === 'send-human') {
    const text = composerDraft.value.trim()
    if (!text) {
      state.busy = false
      return
    }
    r = await api(`/api/console/cases/${encodeURIComponent(caseId)}/messages`, {
      method: 'POST',
      body: { text, author: 'admin' },
    })
  } else {
    state.busy = false
    return
  }

  state.busy = false
  if (!r.ok) {
    state.lastError = r.error
    return
  }
  state.lastError = ''
  if (op === 'send-human') composerDraft.value = ''
  await loadList()
  await loadDetail(caseId)
}

/* ---------- 实时刷新 ---------- */

function signature(detail) {
  if (!detail || !detail.case) return ''
  const c = detail.case
  const msgs = detail.messages || []
  const last = msgs.length ? msgs[msgs.length - 1].seq || msgs.length : 0
  return [
    c.case_id,
    c.status,
    c.workflow_status,
    c.round,
    c.assignee || '',
    c.decision_result || '',
    c.confirmed_by || '',
    msgs.length,
    last,
  ].join('|')
}

function nowClock() {
  const d = new Date()
  const pad = (n) => (n < 10 ? '0' : '') + n
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

async function syncLive() {
  if (!state.live || state.busy) return
  const r = await api('/api/console/cases')
  if (!r.ok) {
    state.conn = 'offline'
    return
  }
  state.conn = 'online'
  state.cases = (r.data && r.data.items) || []
  state.lastSync = nowClock()

  if (!state.selectedId) return
  const d = await api(`/api/console/cases/${encodeURIComponent(state.selectedId)}`)
  if (!d.ok) return
  const sig = signature(d.data)
  if (sig !== state.detailSig) {
    state.detail = d.data
    state.detailSig = sig
    await nextTick()
    scrollConversationToEnd()
  }
}

function scrollConversationToEnd() {
  const el = convEl.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => ((state.detail && state.detail.messages) || []).length,
  () => nextTick(scrollConversationToEnd)
)

async function toggleLive() {
  state.live = !state.live
  if (state.live) await syncLive()
}

function refresh() {
  loadHealth()
  loadList()
}

function openGoods(goodsNo) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type: 'seller-open-goods', goods_no: goodsNo }, '*')
    return
  }
  alert(`该工单关联商品 ${goodsNo}。请在卖家控制台的「商品管理」中查看。`)
}

function showEvidence(name) {
  lightbox.value = name
}

onMounted(() => {
  const params = new URLSearchParams(location.search || '')
  const apiBase = params.get('api')
  if (apiBase) state.apiBase = apiBase
  liveTimer = setInterval(syncLive, LIVE_INTERVAL_MS)
  loadHealth()
  loadList()
})

onUnmounted(() => {
  if (liveTimer) clearInterval(liveTimer)
})
</script>

<template>
  <div class="topbar">
    <div class="brand">售后指挥台</div>
    <div class="conn" :class="state.conn">
      <span class="dot"></span><span>{{ connText }}</span>
    </div>
    <div class="live" :class="{ paused: !state.live }">
      <i></i><span>{{ liveText }}</span>
    </div>
    <button @click="toggleLive">{{ state.live ? '暂停实时' : '开启实时' }}</button>
    <button @click="refresh">刷新</button>
  </div>

  <div class="stage">
    <aside class="side">
      <a
        v-for="it in navItems"
        :key="it.key"
        :class="{ on: state.filter === it.key }"
        @click="state.filter = it.key"
      >
        {{ it.label }}<span class="n">{{ it.count }}</span>
      </a>
    </aside>

    <section class="list">
      <div class="hd">
        <span>售后问题（实时收件箱）</span><span>{{ filteredCases.length }} 单</span>
      </div>
      <div class="items">
        <div v-if="!filteredCases.length" class="empty">暂无工单</div>
        <div
          v-for="c in filteredCases"
          :key="cid(c)"
          class="case"
          :class="{ on: cid(c) === state.selectedId }"
          @click="loadDetail(cid(c))"
        >
          <div class="r1">
            <span class="oid">{{ c.order_id || cid(c) }}</span>
            <span class="badge" :class="badgeClass(c.workflow_status)">{{
              workflowLabel(c.workflow_status)
            }}</span>
          </div>
          <div class="meta">
            <span>{{ caseSummary(c) }}</span>
            <span>轮 {{ c.round || 0 }}</span>
            <span>{{ formatTime(c.updated_at || c.created_at) }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="detail">
      <div v-if="!detailCase" class="empty-detail">请从左侧选择一笔工单</div>
      <template v-else>
        <div class="top">
          <div class="r1">
            <h3>{{ detailCase.order_id || detailCase.case_id || '' }}</h3>
            <span class="badge" :class="badgeClass(ws)">{{ workflowLabel(ws) }}</span>
            <span class="sub">{{ detailSub }}</span>
          </div>
          <div class="sub" style="margin-top: 4px">
            工单号：{{ detailCase.case_id || '—' }}　|　商品：
            <a
              v-if="detailCase.goods_no"
              href="javascript:void(0)"
              style="color: #2563eb"
              @click="openGoods(detailCase.goods_no)"
              >{{ detailCase.goods_no }}</a
            >
            <span v-else title="该订单未带 goods_no，无法关联商品">—</span>
            <template v-if="detailCase.updated_at"> · 更新于 {{ formatTime(detailCase.updated_at) }}</template>
          </div>
        </div>

        <div class="body">
          <div class="grid">
            <div class="box">
              <div class="bt">工单事实</div>
              <div v-for="r in factsRows" :key="r[0]" class="row">
                <span class="k">{{ r[0] }}</span><span class="v">{{ r[1] }}</span>
              </div>
            </div>
            <div class="box">
              <div class="bt">举证材料（点击查看）</div>
              <div v-if="!evidenceNames.length" class="hint">暂无</div>
              <div v-else class="thumbs">
                <div
                  v-for="n in evidenceNames"
                  :key="n"
                  class="thumb"
                  :data-ev="n"
                  @click="showEvidence(n)"
                >
                  🖼<span class="nm">{{ n }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="box">
            <div class="bt">智能体抽取的槽位（推断，不参与判定）</div>
            <div class="slotgrid">
              <div v-if="!slotRows.length" class="hint">暂无</div>
              <div v-for="s in slotRows" :key="s.key" class="slot">
                <div class="k">{{ s.label }}</div>
                <div class="v">{{ s.value }}</div>
                <div class="c">{{ s.meta }}</div>
              </div>
            </div>
          </div>

          <div id="conv" ref="convEl" class="box">
            <div class="bt">完整对话历史（管理员可见 · 实时刷新）</div>
            <div v-if="!detailMessages.length" class="hint">暂无消息</div>
            <div v-for="(m, i) in detailMessages" :key="i" class="msg" :class="msgRole(m)">
              <div>
                <div class="bubble">{{ m.content }}</div>
                <div class="meta">{{ msgMeta(m) }}</div>
              </div>
            </div>
          </div>

          <div v-if="actionLog.length" class="actionlog">
            <div v-for="(a, i) in actionLog" :key="i" class="act" :class="{ fail: !a.ok }">
              <i></i>
              <span>{{ a.tool || '工具' }}{{ a.ok ? '' : '（失败）' }}</span>
              <span v-if="a.summary" class="res">{{ a.summary }}</span>
            </div>
          </div>

          <div v-if="decision" class="card" :class="decision.cls">
            <div class="ct">{{ decision.icon }} {{ decision.title }}</div>
            <div v-if="decision.dr === 'refund' && decision.amount != null" class="amt">
              ¥{{ decision.amount }}
            </div>
            <div v-else-if="decision.amount != null" class="line">
              <span class="k">金额：</span>¥{{ decision.amount }}
            </div>
            <div v-if="decision.confirmed_by" class="line">
              <span class="k">确认人：</span>{{ decision.confirmed_by
              }}<template v-if="decision.confirmed_at"> · {{ formatTime(decision.confirmed_at) }}</template>
            </div>
          </div>

          <div v-if="canCompose" class="composer">
            <textarea
              v-model="composerDraft"
              rows="2"
              placeholder="以人工客服身份直接回复买家（发送即接管，智能体将停止自动应答）"
            ></textarea>
            <button class="btn primary" @click="doAction('send-human')">发送</button>
          </div>

          <div class="actions">
            <template v-if="ws === 'queued'">
              <button class="btn primary" @click="doAction('dispatch')">指派智能体</button>
              <button class="btn ghost" @click="doAction('intervene')">自己处理</button>
              <span v-if="state.modelAvailable === false" class="hint hint-warn"
                >⚠️ 当前未启用模型：指派将直接转人工</span
              >
              <span v-else class="hint">指派后智能体将与买家沟通、取证、判定</span>
            </template>
            <template v-else-if="ws === 'agent_running'">
              <span class="hint">智能体处理中…</span>
              <button class="btn danger" @click="doAction('intervene')">介入（接管）</button>
            </template>
            <template v-else-if="ws === 'awaiting_confirm'">
              <button class="btn ok" @click="doAction('confirm')">
                {{
                  detailCase.decision_result === 'reship'
                    ? '确认补发'
                    : detailCase.decision_result === 'reject'
                      ? '确认拒绝'
                      : '确认退款'
                }}
              </button>
              <button class="btn danger" @click="doAction('reject')">驳回</button>
              <button class="btn ghost" @click="doAction('intervene')">介入（自己接手）</button>
              <span class="hint">确认后资金/库存动作按双闸门执行；也可点「介入」随时自己接手</span>
            </template>
            <template v-else-if="ws === 'resolved'">
              <span class="hint">已完结，结论已归档。</span>
            </template>
            <template v-else-if="ws === 'escalated'">
              <button class="btn primary" @click="doAction('takeover')">接管</button>
              <button
                v-if="buyerAsked"
                class="btn ghost"
                disabled
                title="买家已明确要求人工，不能退回智能体"
              >
                退回智能体
              </button>
              <button v-else class="btn ghost" @click="doAction('resume')">退回智能体</button>
              <button class="btn ok" @click="doAction('close')">人工结案</button>
              <span class="hint">{{
                buyerAsked
                  ? '买家主动要求人工：可接管后自己处理，或直接结案'
                  : '故障排除后可退回智能体继续处理；也可接管自己处理，或直接结案'
              }}</span>
            </template>
          </div>
        </div>
      </template>

      <Teleport to="body">
        <div v-if="lightbox" class="lightbox" @click="lightbox = ''">
          <div class="lb-inner">
            <div class="lb-img">🖼 证据图（图片回显未接入）</div>
            <div class="lb-name">{{ lightbox }}</div>
          </div>
        </div>
      </Teleport>
    </section>
  </div>
</template>
