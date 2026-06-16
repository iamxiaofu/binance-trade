// 统一的后端 API 封装。Basic Auth 由浏览器在首次 401 后自动附带，
// 因此这里不手动管理凭据；仅做 fetch + JSON 解析 + 错误处理。
const JSON_HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json' }
const ENV_KEY = 'binance-trade-environment'
let environment = localStorage.getItem(ENV_KEY) === 'mainnet' ? 'mainnet' : 'testnet'

export function getEnvironment() {
  return environment
}

export function setEnvironment(value) {
  environment = value === 'mainnet' ? 'mainnet' : 'testnet'
  localStorage.setItem(ENV_KEY, environment)
  window.dispatchEvent(new CustomEvent('binance-trade-environment-change', { detail: environment }))
}

export function wsPath(path = '') {
  return `/ws/${environment}${path}`
}

function environmentPath(path) {
  if (path.startsWith('/api/')) return `/api/${environment}/${path.slice(5)}`
  return path
}

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
  const headers = {
    ...(hasBody ? JSON_HEADERS : { Accept: 'application/json' }),
    'X-Trade-Environment': environment,
    ...(opts.headers || {}),
  }
  const resp = await fetch(environmentPath(path), { ...opts, headers })
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

async function mainnetConfirmation(action, payload = '') {
  if (environment !== 'mainnet') return ''
  const confirmation = window.prompt(`MAINNET 高风险操作：${action}\n请输入 MAINNET 确认`)
  if (confirmation !== 'MAINNET') throw new Error('mainnet confirmation canceled')
  const result = await req('/api/confirmations', {
    method: 'POST',
    body: JSON.stringify({ action, payload, confirmation }),
  })
  return result.token
}

export const api = {
  summary: () => req('/api/summary'),
  positions: () => req('/api/positions'),
  decisions: (params = 100, opts = {}) => {
    const query = typeof params === 'number' ? qs({ limit: params }) : qs(params)
    return req(`/api/decisions?${query}`, opts)
  },
  decisionDetail: (id) => req(`/api/decisions/${id}`),
  trades: (params = 100, opts = {}) => {
    const query = typeof params === 'number' ? qs({ limit: params }) : qs(params)
    return req(`/api/trades?${query}`, opts)
  },
  orders: (limit = 100, opts = {}) => req(`/api/orders?limit=${limit}`, opts),
  rejects: (limit = 100, opts = {}) => req(`/api/rejects?limit=${limit}`, opts),
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
  command: async (name, arg = '') => {
    const highRisk = new Set([
      'KILL_SWITCH', 'RESUME', 'RESUME_ALL_SYMBOLS', 'SET_SYMBOL_ENABLED',
      'CLOSE_POSITION', 'CANCEL_AND_FLATTEN', 'STOP_ENGINE',
      'UPDATE_RISK_SETTINGS', 'UPDATE_ENGINE_SETTINGS', 'UPDATE_EXECUTION_SETTINGS',
    ])
    const token = highRisk.has(name) ? await mainnetConfirmation(name, arg) : ''
    return req(`/api/command/${name}?${qs({ arg, confirmation_token: token })}`, { method: 'POST' })
  },
  riskSettings: () => req('/api/risk-settings'),
  riskPreview: (payload) =>
    req('/api/risk-settings/preview', { method: 'POST', body: JSON.stringify(payload) }),
  riskApply: async (payload) => {
    const commandPayload = JSON.stringify({ expected_version: payload.expected_version, ...payload.values })
    const confirmation_token = await mainnetConfirmation('UPDATE_RISK_SETTINGS', commandPayload)
    return req('/api/risk-settings/apply', {
      method: 'POST',
      body: JSON.stringify({ ...payload, confirmation_token }),
    })
  },
  engineSettings: () => req('/api/engine-settings'),
  enginePreview: (payload) =>
    req('/api/engine-settings/preview', { method: 'POST', body: JSON.stringify(payload) }),
  engineApply: async (payload) => {
    const commandPayload = JSON.stringify({ expected_version: payload.expected_version, ...payload.values })
    const confirmation_token = await mainnetConfirmation('UPDATE_ENGINE_SETTINGS', commandPayload)
    return req('/api/engine-settings/apply', {
      method: 'POST',
      body: JSON.stringify({ ...payload, confirmation_token }),
    })
  },
  executionSettings: () => req('/api/execution-settings'),
  executionPreview: (payload) =>
    req('/api/execution-settings/preview', { method: 'POST', body: JSON.stringify(payload) }),
  executionApply: async (payload) => {
    const commandPayload = JSON.stringify({ expected_version: payload.expected_version, ...payload.values })
    const confirmation_token = await mainnetConfirmation('UPDATE_EXECUTION_SETTINGS', commandPayload)
    return req('/api/execution-settings/apply', {
      method: 'POST',
      body: JSON.stringify({ ...payload, confirmation_token }),
    })
  },

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
