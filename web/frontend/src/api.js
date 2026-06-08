// 统一的后端 API 封装。Basic Auth 由浏览器在首次 401 后自动附带，
// 因此这里不手动管理凭据；仅做 fetch + JSON 解析 + 错误处理。
const JSON_HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json' }

function qs(params) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (Array.isArray(v)) {
      v.forEach((item) => {
        if (item !== undefined && item !== null && item !== '') q.append(k, item)
      })
      return
    }
    if (v !== undefined && v !== null && v !== '') q.set(k, v)
  })
  return q.toString()
}

function formatDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === "string") return detail
  if (Array.isArray(detail)) {
    // FastAPI 422: [{type, loc, msg, input}, ...]
    return detail
      .map((e) => {
        const loc = Array.isArray(e.loc) ? e.loc.join(".") : ""
        return loc ? `${loc}: ${e.msg}` : e.msg || JSON.stringify(e)
      })
      .join("; ")
  }
  if (typeof detail === "object") return JSON.stringify(detail)
  return String(detail)
}

async function req(path, opts = {}) {
  // 没 body 的 POST/PUT 不需要 Content-Type，让 fetch 不带该 header
  const hasBody = opts.body !== undefined && opts.body !== null
  const headers = hasBody ? JSON_HEADERS : { Accept: 'application/json' }
  const resp = await fetch(path, { headers, ...opts })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const j = await resp.json()
      detail = j.detail !== undefined ? j.detail : detail
    } catch (_) { /* ignore */ }
    throw new Error(`${resp.status}: ${formatDetail(detail)}`)
  }
  return resp.json()
}

export const api = {
  summary: () => req('/api/summary'),
  positions: () => req('/api/positions'),
  decisions: (params = 100) => {
    const query = typeof params === 'number' ? qs({ limit: params }) : qs(params)
    return req(`/api/decisions?${query}`)
  },
  decisionDetail: (id) => req(`/api/decisions/${id}`),
  trades: (params = 100) => {
    const query = typeof params === 'number' ? qs({ limit: params }) : qs(params)
    return req(`/api/trades?${query}`)
  },
  orders: (limit = 100) => req(`/api/orders?limit=${limit}`),
  rejects: (limit = 100) => req(`/api/rejects?limit=${limit}`),
  pnl: (params = {}) => req(`/api/pnl?${qs(params)}`),
  equity: (params = 500) => {
    const query = typeof params === 'number' ? qs({ limit: params }) : qs(params)
    return req(`/api/equity?${query}`)
  },
  commands: (limit = 50) => req(`/api/commands?limit=${limit}`),
  config: () => req('/api/config'),
  klines: (symbol, timeframe = '5m', limit = 200, source = undefined) =>
    req(`/api/klines/${symbol}?${qs({ timeframe, limit, source })}`),
  ticker: (symbol, source = undefined) =>
    req(`/api/ticker/${symbol}?${qs({ source })}`),
  command: (name, arg = '') =>
    req(`/api/command/${name}?arg=${encodeURIComponent(arg)}`, { method: 'POST' }),

  // LLM profile 管理
  llmStatus: () => req('/api/llm/status'),
  llmProfiles: () => req('/api/llm/profiles'),
  llmCreate: (payload) =>
    req('/api/llm/profiles', { method: 'POST', body: JSON.stringify(payload) }),
  llmUpdate: (name, payload) =>
    req(`/api/llm/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify(payload),
    }),
  llmDelete: (name) =>
    req(`/api/llm/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  llmTest: (name) =>
    req(`/api/llm/profiles/${encodeURIComponent(name)}/test`, { method: 'POST' }),
  llmActivate: (name) =>
    req(`/api/llm/profiles/${encodeURIComponent(name)}/activate`, { method: 'POST' }),
}
