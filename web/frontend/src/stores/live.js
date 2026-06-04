// 全局实时状态 store。
//
// 策略：优先用 WebSocket（推送高效）；但自签证书下浏览器常静默拒绝 wss，
// 因此并行启动「1 秒 HTTPS 轮询」作为兜底。任一通道拿到数据都更新同一份
// 响应式 state，并把 connected 置真。轮询走的是已被浏览器信任的 HTTPS，
// 所以即使 WSS 连不上，看板依然每秒刷新、显示「实时」。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api'

export const useLiveStore = defineStore('live', () => {
  const connected = ref(false)
  const transport = ref('—')        // 'ws' | 'poll' | '—'
  const lastUpdate = ref(null)
  const summary = ref({
    balance: null,
    positions: [],
    recent_decisions: [],
    recent_orders: [],
    recent_rejects: [],
    recent_commands: [],
  })

  let ws = null
  let reconnectTimer = null
  let pollTimer = null
  let staleTimer = null

  const balance = computed(() => summary.value.balance || {})
  const positions = computed(() => summary.value.positions || [])

  function _apply(data, via) {
    summary.value = data
    lastUpdate.value = new Date()
    connected.value = true
    transport.value = via
    _resetStale()
  }

  function _resetStale() {
    if (staleTimer) clearTimeout(staleTimer)
    // 超过 4 秒没有任何新数据 → 标记断开（轮询是 1s，4s 足够宽容）
    staleTimer = setTimeout(() => { connected.value = false }, 4000)
  }

  // ---- WebSocket（可用则用，连不上不影响轮询）----
  function _connectWs() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws`)
    } catch (_) { return }
    ws.onmessage = (ev) => {
      try { _apply(JSON.parse(ev.data), 'ws') } catch (_) { /* ignore */ }
    }
    ws.onclose = () => {
      if (reconnectTimer) return
      reconnectTimer = setTimeout(() => { reconnectTimer = null; _connectWs() }, 5000)
    }
    ws.onerror = () => { try { ws.close() } catch (_) { /* ignore */ } }
  }

  // ---- 1 秒 HTTPS 轮询（主力兜底）----
  async function _pollOnce() {
    try {
      const data = await api.summary()
      // WS 也在推时，避免覆盖更"新"的来源；简单起见两者都更新（数据同源）
      _apply(data, ws && ws.readyState === WebSocket.OPEN ? 'ws' : 'poll')
    } catch (_) {
      // 单次失败不致命，下次再试
    }
  }

  function connect() {
    _connectWs()
    _pollOnce()
    if (!pollTimer) pollTimer = setInterval(_pollOnce, 1000)
  }

  function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    if (staleTimer) { clearTimeout(staleTimer); staleTimer = null }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    if (ws) { try { ws.close() } catch (_) { /* ignore */ } ws = null }
    connected.value = false
  }

  return { connected, transport, lastUpdate, summary, balance, positions, connect, disconnect }
})
