// 统一的后端 API 封装。Basic Auth 由浏览器在首次 401 后自动附带，
// 因此这里不手动管理凭据；仅做 fetch + JSON 解析 + 错误处理。
const JSON_HEADERS = { Accept: 'application/json' }

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

async function req(path, opts = {}) {
  const resp = await fetch(path, { headers: JSON_HEADERS, ...opts })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const j = await resp.json()
      detail = j.detail || detail
    } catch (_) { /* ignore */ }
    throw new Error(`${resp.status}: ${detail}`)
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
  orders: (limit = 100) => req(`/api/orders?limit=${limit}`),
  rejects: (limit = 100) => req(`/api/rejects?limit=${limit}`),
  pnl: () => req('/api/pnl'),
  equity: (limit = 500) => req(`/api/equity?limit=${limit}`),
  commands: (limit = 50) => req(`/api/commands?limit=${limit}`),
  config: () => req('/api/config'),
  klines: (symbol, timeframe = '5m', limit = 200, source = undefined) =>
    req(`/api/klines/${symbol}?${qs({ timeframe, limit, source })}`),
  ticker: (symbol, source = undefined) =>
    req(`/api/ticker/${symbol}?${qs({ source })}`),
  command: (name, arg = '') =>
    req(`/api/command/${name}?arg=${encodeURIComponent(arg)}`, { method: 'POST' }),
}
