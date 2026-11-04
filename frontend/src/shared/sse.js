/**
 * SSE 解析（纯函数）。
 *
 * 后端 `/messages/stream` 是**确定性分段推送**（meta.stream_mode === "segmented"），
 * 不是 token 级流式；本模块只负责把已到达的完整帧解析出来。
 */

/**
 * 解析缓冲区里**已完整**的 SSE 帧，返回 { events, rest }。
 * `rest` 是尚未收到空行结尾的残留内容，需与后续数据拼接后再解析。
 */
export function parseSSEChunk(buffer) {
  let text = String(buffer == null ? '' : buffer)
  const events = []
  let idx
  while ((idx = text.indexOf('\n\n')) !== -1) {
    const block = text.slice(0, idx)
    text = text.slice(idx + 2)
    // 空块必须跳过，否则会凭空多出一个 message 事件
    if (!block.trim()) continue
    const ev = { event: 'message', data: null }
    let payload = ''
    block.split('\n').forEach((line) => {
      if (line.indexOf('event:') === 0) ev.event = line.slice(6).trim()
      else if (line.indexOf('data:') === 0) {
        payload += (payload ? '\n' : '') + line.slice(5).trim()
      }
    })
    if (payload) {
      try {
        ev.data = JSON.parse(payload)
      } catch (e) {
        ev.data = { raw: payload }
      }
    }
    events.push(ev)
  }
  return { events, rest: text }
}

/** 消费 SSE 响应，逐个回调事件。兼容无 `body.getReader` 的环境（测试桩）。 */
export async function consumeSSE(res, onEvent) {
  if (res && res.body && typeof res.body.getReader === 'function') {
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const chunk = await reader.read()
      if (chunk.done) break
      buf += decoder.decode(chunk.value, { stream: true })
      const parsed = parseSSEChunk(buf)
      buf = parsed.rest
      parsed.events.forEach(onEvent)
    }
    if (buf.trim()) {
      parseSSEChunk(buf + '\n\n').events.forEach(onEvent)
    }
    return
  }
  const body = await res.text()
  parseSSEChunk(body + '\n\n').events.forEach(onEvent)
}
