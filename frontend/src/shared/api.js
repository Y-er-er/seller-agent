/**
 * 统一的后端调用出口。
 *
 * 与原 h5.js / console.js 的 `api()` 保持同一契约：
 * 返回 { ok, status, data, error }，**不抛异常**，让调用方始终能给出明确提示。
 */
export async function request(base, path, options = {}) {
  const url = base + path
  const init = { method: options.method || 'GET', headers: {} }

  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(options.body)
  }
  if (options.form) {
    // FormData：交给浏览器自行设置 multipart 边界
    init.body = options.form
  }

  try {
    const res = await fetch(url, init)
    if (options.raw) {
      return { ok: res.ok, status: res.status, response: res }
    }
    const text = await res.text()
    let data = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch (e) {
        data = { detail: text }
      }
    }
    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        data,
        error: (data && data.detail) || `HTTP ${res.status}`,
      }
    }
    return { ok: true, status: res.status, data, error: '' }
  } catch (err) {
    return {
      ok: false,
      status: 0,
      data: null,
      error: `无法连接后端（${base}）：${err && err.message ? err.message : err}`,
    }
  }
}
