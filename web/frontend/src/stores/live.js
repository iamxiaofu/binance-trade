// 全局实时状态 store。
//
// 策略：优先用 WebSocket（推送高效）；WS 断开时才启动 HTTPS 轮询兜底。
// 页面隐藏时暂停连接/轮询，避免后台标签页持续请求导致浏览器焦点被打扰。
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
  let visibilityBound = false

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
    // 超过 4 秒没有任何新数据 → 标记断开（兜底轮询是 3s，4s 足够宽容）
    staleTimer = setTimeout(() => { connected.value = false }, 4000)
  }

  // ---- WebSocket（可用则用，连不上不影响轮询）----
  function _connectWs() {
    if (document.hidden) return
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws`)
    } catch (_) { return }
    ws.onmessage = (ev) => {
      try { _apply(JSON.parse(ev.data), 'ws') } catch (_) { /* ignore */ }
    }
    ws.onopen = () => { _stopPoll() }
    ws.onclose = () => {
      ws = null
      _startPoll()
      if (reconnectTimer) return
      reconnectTimer = setTimeout(() => { reconnectTimer = null; _connectWs() }, 5000)
    }
    ws.onerror = () => { try { ws.close() } catch (_) { /* ignore */ } }
  }

  // ---- HTTPS 轮询（仅 WS 断开时兜底）----
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
    if (!visibilityBound) {
      document.addEventListener('visibilitychange', _handleVisibility)
      visibilityBound = true
    }
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
    if (ws) { try { ws.close() } catch (_) { /* ignore */ } ws = null }
    if (markDisconnected) connected.value = false
  }

  function _handleVisibility() {
    if (document.hidden) {
      _stopTransports(false)
      transport.value = 'paused'
      return
    }
    connect()
  }

  function disconnect() {
    if (visibilityBound) {
      document.removeEventListener('visibilitychange', _handleVisibility)
      visibilityBound = false
    }
    _stopTransports(true)
  }

  return { connected, transport, lastUpdate, summary, balance, positions, connect, disconnect }
})
