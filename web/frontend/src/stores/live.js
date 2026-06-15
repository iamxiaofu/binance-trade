// 全局实时状态 store。
//
// 策略：优先用 WebSocket（推送高效）；WS 断开时才启动 HTTPS 轮询兜底。
// 页面隐藏时跳过轮询请求，避免后台标签页持续请求。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, wsPath } from '../api'

export const useLiveStore = defineStore('live', () => {
  const connected = ref(false)
  const transport = ref('—')        // 'ws' | 'poll' | '—'
  const lastUpdate = ref(null)
  const summary = ref({
    balance: null,
    positions: [],
    positions_source: 'db_snapshot',
    positions_error: '',
    positions_synced_at_ms: null,
    condition_orders: [],
    condition_orders_error: '',
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
    // 超过 10 秒没有任何新数据 → 标记断开（轮询 3s，给交易所接口留出余量）
    staleTimer = setTimeout(() => { connected.value = false }, 10000)
  }

  // ---- WebSocket（可用则用，连不上不影响轮询）----
  function _connectWs() {
    if (document.hidden) return
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${location.host}${wsPath()}`)
    } catch (_) { return }
    ws.onmessage = (ev) => {
      try { _apply(JSON.parse(ev.data), 'ws') } catch (_) { /* ignore */ }
    }
    ws.onopen = () => { _startPoll() }
    ws.onclose = () => {
      ws = null
      _startPoll()
      if (reconnectTimer) return
      reconnectTimer = setTimeout(() => { reconnectTimer = null; _connectWs() }, 5000)
    }
    ws.onerror = () => { try { ws.close() } catch (_) { /* ignore */ } }
  }

  // ---- HTTPS 轮询（WS 正常时也保留低频兜底）----
  async function _pollOnce() {
    if (document.hidden) return
    try {
      const data = await api.summary()
      _apply(data, 'poll')
    } catch (_) {
      // 单次失败不致命，下次再试
    }
  }

  function connect() {
    if (document.hidden) return
    _connectWs()
    _pollOnce()
    _startPoll()
  }

  function _startPoll() {
    if (document.hidden || pollTimer) return
    pollTimer = setInterval(_pollOnce, 3000)
  }

  function _stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }

  function _stopTransports(markDisconnected = true) {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    if (staleTimer) { clearTimeout(staleTimer); staleTimer = null }
    _stopPoll()
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.onmessage = null
      try { ws.close() } catch (_) { /* ignore */ }
      ws = null
    }
    if (markDisconnected) connected.value = false
  }

  function disconnect() {
    _stopTransports(true)
  }

  return { connected, transport, lastUpdate, summary, balance, positions, connect, disconnect }
})
