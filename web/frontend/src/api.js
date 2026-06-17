import { reactive } from 'vue'

// 统一的后端 API 封装。浏览器用户走应用内登录页 + HttpOnly session cookie；
// Basic Auth 仍由后端兼容给脚本使用。
const JSON_HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json' }
const ENV_KEY = 'binance-trade-environment'
let environment = localStorage.getItem(ENV_KEY) === 'mainnet' ? 'mainnet' : 'testnet'
const AUTH_EVENT = 'binance-trade-auth-required'
const AUTH_CHANGE_EVENT = 'binance-trade-auth-change'
const ENVIRONMENTS = ['testnet', 'mainnet']

export const authState = reactive({
  checked: false,
  authenticated: false,
  username: '',
})

function setAuthState(authenticated, username = '') {
  authState.checked = true
  authState.authenticated = authenticated
  authState.username = authenticated ? username : ''
  window.dispatchEvent(new CustomEvent(AUTH_CHANGE_EVENT, {
    detail: { authenticated: authState.authenticated, username: authState.username },
  }))
}

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
  return environmentPathFor(environment, path)
}

function environmentPathFor(env, path) {
  if (path.startsWith('/api/')) return `/api/${env}/${path.slice(5)}`
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

async function reqForEnv(env, path, opts = {}, authOptions = {}) {
  // 没 body 的 POST/PUT 不需要 Content-Type，让 fetch 不带该 header
  const hasBody = opts.body !== undefined && opts.body !== null
  const headers = {
    ...(hasBody ? JSON_HEADERS : { Accept: 'application/json' }),
    'X-Trade-Environment': env,
    ...(opts.headers || {}),
  }
  const resp = await fetch(environmentPathFor(env, path), {
    ...opts,
    headers,
    credentials: 'same-origin',
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const j = await resp.json()
      detail = j.detail !== undefined ? j.detail : detail
    } catch (_) { /* ignore */ }
    if (resp.status === 401 && !authOptions.suppressAuthError) {
      setAuthState(false)
      window.dispatchEvent(new CustomEvent(AUTH_EVENT, { detail: { path, env } }))
    }
    throw new Error(`${resp.status}: ${formatDetail(detail)}`)
  }
  return resp.json()
}

async function req(path, opts = {}) {
  return reqForEnv(environment, path, opts)
}

export async function authLogin(username, password) {
  const body = JSON.stringify({ username, password })
  const results = await Promise.allSettled(
    ENVIRONMENTS.map((env) => reqForEnv(
      env,
      '/api/auth/login',
      { method: 'POST', body },
      { suppressAuthError: true },
    ))
  )
  const failed = results
    .map((result, index) => ({ result, env: ENVIRONMENTS[index] }))
    .filter(({ result }) => result.status === 'rejected')
  if (failed.length) {
    await authLogout()
    throw new Error(failed.map(({ env, result }) => `${env}: ${result.reason.message}`).join('; '))
  }
  const user = results.find((result) => result.status === 'fulfilled')?.value?.username || username
  setAuthState(true, user)
  return { authenticated: true, username: user }
}

export async function authLogout() {
  await Promise.allSettled(
    ENVIRONMENTS.map((env) => reqForEnv(
      env,
      '/api/auth/logout',
      { method: 'POST' },
      { suppressAuthError: true },
    ))
  )
  setAuthState(false)
}

export async function authMe() {
  const results = await Promise.allSettled(
    ENVIRONMENTS.map((env) => reqForEnv(
      env,
      '/api/auth/me',
      {},
      { suppressAuthError: true },
    ))
  )
  const ok = results.every((result) => result.status === 'fulfilled' && result.value?.authenticated)
  if (!ok) {
    setAuthState(false)
    return { authenticated: false }
  }
  const user = results.find((result) => result.status === 'fulfilled')?.value?.username || ''
  setAuthState(true, user)
  return { authenticated: true, username: user }
}

export async function ensureAuthChecked(force = false) {
  if (authState.checked && !force) return authState.authenticated
  const result = await authMe()
  return Boolean(result.authenticated)
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
      'UPDATE_LLM_PROMPT', 'RELOAD_LLM_PROMPT',
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
  llmPrompt: () => req('/api/llm/prompt'),
  llmPromptPreview: (payload) =>
    req('/api/llm/prompt/preview', { method: 'POST', body: JSON.stringify(payload) }),
  llmPromptApply: async (payload) => {
    const commandPayload = JSON.stringify({
      name: payload.name || '',
      content: payload.content || '',
      render_mode: payload.render_mode || 'legacy_append',
      system_prompt_template: payload.system_prompt_template || '',
      user_prompt_template: payload.user_prompt_template || '',
      notes: payload.notes || '',
    })
    const confirmation_token = await mainnetConfirmation('UPDATE_LLM_PROMPT', commandPayload)
    return req('/api/llm/prompt/apply', {
      method: 'POST',
      body: JSON.stringify({ ...payload, confirmation_token }),
    })
  },
  llmPromptValidate: (payload) =>
    req('/api/llm/prompt/validate', { method: 'POST', body: JSON.stringify(payload) }),
  llmPromptActivate: async (version) => {
    const id = Number(version?.id || 0)
    const commandPayload = JSON.stringify({ id, version: Number(version?.version || 0) })
    const confirmation_token = await mainnetConfirmation('ACTIVATE_LLM_PROMPT', commandPayload)
    return req(`/api/llm/prompt/${encodeURIComponent(id)}/activate`, {
      method: 'POST',
      body: JSON.stringify({ confirmation_token }),
    })
  },
}
