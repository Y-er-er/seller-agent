<script setup>
/**
 * 买家侧对话页（H5）—— Vue 3 版本。
 *
 * 与原 h5.js 的行为契约保持一致：
 *  - 单一后端出口（shared/api.js），失败给出可操作文案；
 *  - SSE 分段推送，界面做“打字效果”，不宣称 token 级流式；
 *  - 转人工后**不锁输入**，买家仍可补充信息；
 *  - 终态（转人工 / 已结束）提供「重新开始」出口；
 *  - 演示模式显著标注“合成数据”，不伪装成真实订单。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { request } from '../shared/api'
import { consumeSSE } from '../shared/sse'

const MAX_MSG_CHARS = 200
const DEMO_STORE_KEY = 'refund.h5.demoMode'
const DEFAULT_API_BASE = 'http://127.0.0.1:8000'

const state = reactive({
  apiBase: DEFAULT_API_BASE,
  caseId: '',
  orderId: '',
  orderInput: '',
  order: null,
  demoMode: false,
  demoInfo: null,
  messages: [],
  status: '',
  round: 0,
  maxRounds: 20,
  gate: null,
  escalated: false,
  ended: false,
  sending: false,
  overLengthWarned: false,
  attachments: [],
  lastError: '',
})

const draft = ref('')
const fileInput = ref(null)
const messagesEl = ref(null)

const api = (path, options) => request(state.apiBase, path, options)

/* ---------- 展示派生 ---------- */

const roundPct = computed(() => {
  const max = Number(state.maxRounds) || 20
  const used = Number(state.round) || 0
  return Math.max(0, Math.min(100, Math.round((used / max) * 100)))
})

const roundClass = computed(() => {
  const max = Number(state.maxRounds) || 20
  const used = Number(state.round) || 0
  if (used >= max) return 'is-full'
  if (used >= max - 3) return 'is-warn'
  return ''
})

const orderRows = computed(() => {
  const order = state.order
  if (!order) return []
  const rows = [
    ['平台订单号', order.order_no || order.order_id],
    ['商品', order.item_title || order.item_category || order.title],
    ['单价', order.unit_price != null ? `¥${order.unit_price}` : ''],
    ['数量', order.quantity != null ? `x${order.quantity}` : ''],
    ['实付', order.pay_amount != null ? `¥${order.pay_amount}` : ''],
    ['快递', order.courier],
    ['运单号', order.tracking_no],
    ['状态', order.logistics_text || order.status_text || order.status],
  ].filter((r) => r[1] !== undefined && r[1] !== null && r[1] !== '')
  if (order.order_no && order.order_id && order.order_no !== order.order_id) {
    rows.push(['查询号', order.order_id])
  }
  return rows
})

const demoParts = computed(() => {
  const info = state.demoInfo && state.demoInfo.order
  if (!info) return []
  const parts = []
  if (info.item_title || info.item_category) parts.push(info.item_title || info.item_category)
  if (info.pay_amount != null) parts.push(`¥${info.pay_amount}`)
  if (info.courier) parts.push(info.courier)
  if (info.tracking_no) parts.push(info.tracking_no)
  if (info.logistics_text || info.logistics_status) {
    parts.push(info.logistics_text || info.logistics_status)
  }
  return parts
})

const tips = computed(() => {
  const list = []
  if (state.lastError) list.push({ level: 'danger', text: state.lastError })
  if (state.overLengthWarned) {
    list.push({
      level: 'warn',
      text: `消息超过 ${MAX_MSG_CHARS} 字，本次已按超长提示处理；再次超长将转人工客服。`,
    })
  }
  if (state.gate && state.gate.paused) {
    list.push({ level: 'warn', text: state.gate.message || '对话入口已暂停。' })
  }
  if (state.escalated) {
    list.push({ level: 'info', text: '已转人工客服，人工会看到你后续的留言。' })
  }
  if (state.ended) {
    list.push({ level: 'info', text: '本会话已结束。可点下方「重新开始」换一个订单号继续咨询。' })
  }
  return list
})

const locked = computed(() => state.sending || state.ended || !state.caseId)
const counterOver = computed(() => draft.value.length > MAX_MSG_CHARS)

const composerHint = computed(() => {
  if (state.ended) return '会话已结束'
  if (!state.caseId) return '请先填写订单号并点「开始」'
  if (state.sending) return '正在发送…'
  return '描述你遇到的问题…'
})

function roleClass(m) {
  if (m.role === 'buyer') return 'buyer'
  if (m.role === 'system') return 'system'
  if (m.role === 'human') return ['human', 'agent']
  return 'agent'
}

function amountLabel(amount) {
  return typeof amount === 'object' ? amount.label || '涉及金额' : '涉及金额'
}

function amountValue(amount) {
  return typeof amount === 'object' ? amount.value : amount
}

/* ---------- 滚动 ---------- */

function scrollToEnd() {
  const el = messagesEl.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => (state.messages.length ? state.messages[state.messages.length - 1].content : ''),
  () => nextTick(scrollToEnd)
)
watch(() => state.caseId, () => nextTick(scrollToEnd))

/* ---------- 演示模式持久化 ---------- */

function readStoredDemo() {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(DEMO_STORE_KEY) === '1'
  } catch (e) {
    return false
  }
}

function storeDemo(on) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(DEMO_STORE_KEY, on ? '1' : '0')
  } catch (e) {
    /* 持久化失败不影响本次使用 */
  }
}

function toggleDemo() {
  state.demoMode = !state.demoMode
  storeDemo(state.demoMode)
}

/* ---------- 会话 ---------- */

function resetLocalSession() {
  state.caseId = ''
  state.messages = []
  state.status = ''
  state.round = 0
  state.gate = null
  state.escalated = false
  state.ended = false
  state.order = null
  state.demoInfo = null
  state.attachments = []
  state.overLengthWarned = false
  state.lastError = ''
  state.orderInput = ''
  draft.value = ''
}

function applyServerSnapshot(payload) {
  const s = (payload && payload.session) || {}
  if (s.status) state.status = s.status
  if (s.round != null) state.round = s.round
  // 终态按语义分开：ESCALATED = 等人工（仍可留言）；CLOSED = 已结束（输入关闭）
  if (s.status === 'ESCALATED') {
    state.escalated = true
    state.ended = false
  } else if (s.status === 'CLOSED') {
    state.ended = true
    state.escalated = false
  }
  if (payload && payload.messages) {
    state.messages = payload.messages.map((m) => ({
      role: m.role,
      content: m.content,
      seq: m.seq,
    }))
  }
}

async function createCase(orderId) {
  const id = String(orderId == null ? '' : orderId).trim()
  if (!id) {
    state.lastError = '请填写订单号'
    return false
  }
  state.orderId = id
  state.orderInput = id
  state.lastError = ''
  state.demoInfo = null

  const res = await api('/api/h5/cases', {
    method: 'POST',
    body: { order_id: id, demo: !!state.demoMode },
  })
  if (!res.ok) {
    const prefix =
      res.status === 403
        ? '演示模式不可用：'
        : res.status === 503
          ? '对话入口暂不可用：'
          : '创建会话失败：'
    state.lastError = prefix + res.error
    return false
  }
  state.caseId = res.data.case_id
  if (res.data.gate) state.gate = res.data.gate
  if (res.data.demo) {
    state.demoInfo = res.data.demo
    if (res.data.demo.order) state.order = res.data.demo.order
  }
  await loadCase()
  return true
}

async function loadCase() {
  if (!state.caseId) return false
  const res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}`)
  if (!res.ok) {
    state.lastError = '读取会话失败：' + res.error
    return false
  }
  applyServerSnapshot(res.data)
  return true
}

async function start() {
  await createCase(state.orderInput)
}

/* ---------- 发送 ---------- */

async function sendMessage(text, options = {}) {
  const body = String(text == null ? '' : text).trim()
  if (!body || state.sending || state.escalated || state.ended) return false

  state.messages.push({ role: 'buyer', content: body })
  state.sending = true
  state.lastError = ''

  if (body.length > MAX_MSG_CHARS && !state.overLengthWarned) {
    state.overLengthWarned = true
  }

  if (options.stream === false) {
    const plain = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/messages`, {
      method: 'POST',
      body: { text: body },
    })
    state.sending = false
    if (!plain.ok) {
      state.messages.push({ role: 'system', content: '发送失败：' + plain.error })
      state.lastError = plain.error
      return false
    }
    applySendResult(plain.data)
    return true
  }

  const streamRes = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/messages/stream`, {
    method: 'POST',
    body: { text: body },
    raw: true,
  })
  if (!streamRes.ok) {
    let detail = ''
    try {
      detail = await streamRes.response.text()
    } catch (e) {
      detail = ''
    }
    state.sending = false
    state.lastError = `发送失败（HTTP ${streamRes.status}）${detail ? '：' + detail : ''}`
    return false
  }

  // 分段渲染：先插入一个空的 agent 气泡，逐段 append
  const bubbleIndex = state.messages.length
  state.messages.push({ role: 'agent', content: '', streaming: true })
  await nextTick(scrollToEnd)

  let meta = null
  let decision = null
  let finished = false

  await consumeSSE(streamRes.response, (ev) => {
    const bubble = state.messages[bubbleIndex]
    if (ev.event === 'status') {
      bubble.content = (ev.data && ev.data.message) || '处理中…'
    } else if (ev.event === 'meta') {
      meta = ev.data || {}
      if (meta.max_rounds) state.maxRounds = meta.max_rounds
      if (meta.round != null) state.round = meta.round
      if (meta.status) state.status = meta.status
      if (meta.gate) state.gate = meta.gate
      bubble.content = meta.notice || ''
    } else if (ev.event === 'delta') {
      bubble.content += (ev.data && ev.data.text) || ''
    } else if (ev.event === 'done') {
      finished = true
      if (ev.data) decision = ev.data.decision || null
    } else if (ev.event === 'error') {
      bubble.content =
        '（本次未能完成）' + (ev.data && ev.data.message ? ev.data.message : '未知错误')
      if (ev.data && ev.data.code) state.lastError = ev.data.message || ev.data.code
    }
  })

  const bubble = state.messages[bubbleIndex]
  bubble.streaming = false
  if (!finished && !bubble.content) {
    bubble.content = '（未收到完整回复，请重试或转人工）'
  }
  if (meta) {
    if (meta.escalated) state.escalated = true
    if (meta.status === 'CLOSED') state.ended = true
  }
  if (decision) {
    if (decision.refund_amount != null) {
      bubble.amount = { label: '建议退款金额', value: decision.refund_amount }
    }
    const basis = []
    if (decision.hit_rules && decision.hit_rules.length) {
      basis.push('命中规则：' + decision.hit_rules.join('、'))
    }
    if (decision.conclusion) basis.push('结论： ' + decision.conclusion)
    if (basis.length) bubble.basis = basis
  }
  state.sending = false
  return true
}

function applySendResult(data) {
  const d = data || {}
  if (d.max_rounds) state.maxRounds = d.max_rounds
  if (d.round != null) state.round = d.round
  if (d.status) state.status = d.status
  if (d.gate) state.gate = d.gate
  const msg = { role: 'agent', content: d.reply || d.notice || '（无回复）' }
  if (d.decision && d.decision.refund_amount != null) {
    msg.amount = { label: '建议退款金额', value: d.decision.refund_amount }
  }
  state.messages.push(msg)
  if (d.escalated) state.escalated = true
  if (d.status === 'CLOSED') state.ended = true
}

async function sendDraft() {
  const text = draft.value
  if (!text.trim()) return
  await sendMessage(text)
  draft.value = ''
}

/* ---------- 附件与转人工 ---------- */

async function uploadAttachment(file) {
  if (!file) return false
  const form = new FormData()
  form.append('file', file, file.name)
  const res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/attachments`, {
    method: 'POST',
    form,
  })
  if (!res.ok) {
    state.lastError = '上传失败：' + res.error
    return false
  }
  const name = res.data.filename || file.name
  state.attachments.push(name)
  state.messages.push({ role: 'system', content: '已上传材料：' + name })
  return true
}

function onFile(event) {
  const f = event.target.files && event.target.files[0]
  if (f) uploadAttachment(f)
  event.target.value = ''
}

async function escalate() {
  if (!state.caseId || state.escalated) return false
  const res = await api(`/api/h5/cases/${encodeURIComponent(state.caseId)}/escalate`, {
    method: 'POST',
    body: { note: '' },
  })
  if (!res.ok) {
    state.lastError = '转人工失败：' + res.error
    return false
  }
  state.messages.push({ role: 'agent', content: res.data.reply || '已为你转接人工客服。' })
  state.escalated = true
  return true
}

/* ---------- 初始化 ---------- */

function readQuery() {
  let search = ''
  try {
    search = (typeof window !== 'undefined' && window.location && window.location.search) || ''
  } catch (e) {
    search = ''
  }
  if (!search) return { order_id: '', api: '' }
  const params = new URLSearchParams(search)
  return { order_id: params.get('order_id') || '', api: params.get('api') || '' }
}

onMounted(async () => {
  const q = readQuery()
  if (q.api) state.apiBase = q.api
  state.demoMode = readStoredDemo()
  state.orderInput = q.order_id || ''
  if (q.order_id) await createCase(q.order_id)
  await nextTick(scrollToEnd)
})
</script>

<template>
  <div class="h5-header">
    <h1>售后助手</h1>
    <div class="sub">
      {{
        state.caseId
          ? `会话 ${state.caseId} · 第 ${state.round}/${state.maxRounds} 轮`
          : '请先输入订单号开始咨询'
      }}
    </div>
    <div class="round-bar" :class="roundClass"><i :style="{ width: roundPct + '%' }"></i></div>
  </div>

  <div v-if="state.demoMode" class="demo-badge">
    <b>演示模式</b>
    <span>订单事实为合成数据，非真实订单</span>
    <em v-if="demoParts.length">{{ demoParts.join(' · ') }}</em>
  </div>

  <div v-if="state.order && orderRows.length" class="order-card">
    <div class="title">关联订单</div>
    <div v-for="r in orderRows" :key="r[0]" class="row">
      <span class="k">{{ r[0] }}</span><span class="v">{{ r[1] }}</span>
    </div>
    <ol v-if="state.order.traces && state.order.traces.length" class="trace">
      <li v-for="(t, i) in state.order.traces" :key="i">{{ t }}</li>
    </ol>
  </div>

  <div v-if="!state.caseId" class="start-card">
    <div class="sc-title">开始咨询</div>
    <div class="sc-row">
      <input
        v-model="state.orderInput"
        type="text"
        placeholder="填写订单号（如 TEST-001）"
        @keyup.enter="start"
      />
      <button class="btn primary" @click="start">开始</button>
    </div>
    <div class="sc-row">
      <button
        class="switch"
        :class="{ on: state.demoMode }"
        role="switch"
        :aria-checked="state.demoMode ? 'true' : 'false'"
        @click="toggleDemo"
      >
        <i></i><span>演示模式</span>
      </button>
      <span class="sc-hint">
        {{
          state.demoMode
            ? '任意订单号都按「订单存在」受理；自动编造订单号/商品/快递/物流轨迹/买家画像等真实电商数据，换个号即换一组'
            : '关闭时只受理真实 / 已预置的订单号'
        }}
      </span>
    </div>
  </div>

  <div ref="messagesEl" class="messages">
    <div v-for="(m, i) in state.messages" :key="i" class="msg" :class="roleClass(m)">
      <div class="bubble" :class="{ caret: m.streaming }">
        {{ m.content }}
        <div v-if="m.amount != null" class="amount-card">
          <div class="label">{{ amountLabel(m.amount) }}</div>
          <div class="value">¥{{ amountValue(m.amount) }}</div>
        </div>
        <details v-if="m.basis && m.basis.length" class="basis">
          <summary>判定依据（点击展开）</summary>
          <ol>
            <li v-for="(b, bi) in m.basis" :key="bi">{{ b }}</li>
          </ol>
        </details>
        <div v-if="m.quick && m.quick.length" class="quick">
          <button v-for="q in m.quick" :key="q" type="button" @click="sendMessage(q)">
            {{ q }}
          </button>
        </div>
      </div>
    </div>
  </div>

  <div class="tips">
    <div v-for="t in tips" :key="t.text" class="tip" :class="t.level">{{ t.text }}</div>
  </div>

  <div v-if="state.escalated || state.ended" class="restart-row">
    <span>本会话{{ state.ended ? '已结束' : '已转人工' }}。换一个订单号即可开始新会话。</span>
    <button class="btn ghost" @click="resetLocalSession">重新开始</button>
  </div>

  <div class="composer">
    <div v-if="state.attachments.length" class="toolbar">
      <span>
        <span v-for="n in state.attachments" :key="n" class="file-chip">📎 {{ n }}</span>
      </span>
    </div>
    <div class="row">
      <textarea
        v-model="draft"
        rows="1"
        :placeholder="composerHint"
        :disabled="locked"
        @keydown.enter.ctrl.prevent="sendDraft"
      ></textarea>
      <button class="btn primary" :disabled="locked" @click="sendDraft">发送</button>
    </div>
    <div class="toolbar">
      <span class="counter" :class="{ over: counterOver }">{{ draft.length }} / {{ MAX_MSG_CHARS }}</span>
      <span>
        <input
          ref="fileInput"
          type="file"
          class="hidden"
          accept=".jpg,.jpeg,.png,.webp,.pdf"
          @change="onFile"
        />
        <button class="btn ghost" :disabled="locked" @click="fileInput && fileInput.click()">
          上传举证
        </button>
        <button class="btn ghost" :disabled="state.escalated" @click="escalate">转人工</button>
      </span>
    </div>
  </div>
</template>
