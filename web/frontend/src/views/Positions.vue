<script setup>
import { computed, ref, onUnmounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, getEnvironment, wsPath } from '../api'
import { useLiveStore } from '../stores/live'
import { orderStatusLabel, utc8DateTime, utc8Time } from '../labels'

const live = useLiveStore()
const repairing = ref({})
const takeoverVisible = ref(false)
const takeoverSubmitting = ref(false)
const takeoverRow = ref(null)
const closeVisible = ref(false)
const closeSubmitting = ref(false)
const closeRow = ref(null)
const closeConfirm = ref(false)
const takeoverForm = ref({
  qty: '',
  sl: '',
  tpTargets: [],
  confirm: false,
})
const positions = computed(() => live.positions || [])
const positionsSource = computed(() => live.summary?.positions_source || 'db_snapshot')
const positionsError = computed(() => live.summary?.positions_error || '')
const conditionOrdersError = computed(() => live.summary?.condition_orders_error || '')
const openOrders = computed(() => live.summary?.open_orders || [])
const openOrdersError = computed(() => live.summary?.open_orders_error || '')
const openOrdersSyncedAtMs = computed(() => live.summary?.open_orders_synced_at_ms || live.summary?.positions_synced_at_ms)
const openOrdersSyncedText = computed(() => {
  const ts = openOrdersSyncedAtMs.value
  return utc8Time(ts)
})
const syncedAtText = computed(() => {
  const ts = live.summary?.positions_synced_at_ms
  return utc8Time(ts)
})
const positionSourceLabel = computed(() => {
  if (positionsSource.value === 'exchange') return '交易所实时'
  if (positionsSource.value === 'account_projection') return '账户投影实时'
  return '本地快照'
})
const positionSourceTagType = computed(() => {
  if (positionsSource.value === 'db_snapshot') return 'warning'
  return 'success'
})
const marketTicks = ref({})
const marketSockets = new Map()
const marketConnected = ref({})
const hasRealtimeMarket = computed(() => Object.values(marketConnected.value).some(Boolean))
const marketSyncedAtText = computed(() => {
  const times = Object.values(marketTicks.value).map((tick) => Number(tick?.ts || 0)).filter((ts) => ts > 0)
  return utc8Time(times.length ? Math.max(...times) : null)
})
const displayPositions = computed(() => positions.value.map(applyRealtimeMark))
const hasMissingProtection = computed(() => displayPositions.value.some((p) =>
  p.protection?.missing_sl || p.protection?.missing_tp
))
const hasProtectionConflict = computed(() => displayPositions.value.some((p) =>
  (p.protection?.conflicts || []).length > 0
))
// B5：暴露 symbol_enabled 表与「持仓 + 币种已禁用」集合。
const symbolEnabledMap = computed(() => live.summary?.symbol_enabled || {})
const disabledWithPosition = computed(() => new Set(live.summary?.disabled_with_position || []))
function isSymbolDisabled(symbol) {
  return symbolEnabledMap.value[symbol] === false
}
const enabling = ref({})
async function enableSymbol(row) {
  const symbol = row.symbol
  if (!symbol) return
  enabling.value = { ...enabling.value, [symbol]: true }
  try {
    await api.command('SET_SYMBOL_ENABLED', `${symbol}=true`)
    ElMessage.success(`${symbol} 已重新启用，孤儿持仓将在下个周期自动接管并补 SL/TP`)
  } catch (e) {
    ElMessage.error(`启用 ${symbol} 失败: ${e.message}`)
  } finally {
    const next = { ...enabling.value }
    delete next[symbol]
    enabling.value = next
  }
}

function fmt(n, d = 4) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? v.toFixed(d) : '—'
}

function fmtPct(n) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : '—'
}

function fmtTime(ts) {
  return utc8DateTime(ts)
}

function margin(row) {
  const wallet = Number(row.isolated_wallet || 0)
  const pnl = Number(row.unrealized_pnl || 0)
  if (String(row.margin_mode || '').toLowerCase() === 'isolated' && wallet > 0) {
    return Math.max(0, wallet + (Number.isFinite(pnl) ? pnl : 0))
  }
  return Number(row.isolated_margin || row.initial_margin || 0)
}

function realtimePrice(row) {
  const tick = marketTicks.value[row.symbol]
  const mark = Number(tick?.mark || 0)
  if (Number.isFinite(mark) && mark > 0) return { price: mark, kind: 'mark' }
  const last = Number(tick?.last || 0)
  if (Number.isFinite(last) && last > 0) return { price: last, kind: 'last' }
  return null
}

function applyRealtimeMark(row) {
  const realtime = realtimePrice(row)
  if (realtime == null) return row
  const mark = realtime.price
  const qty = Number(row.contracts || 0)
  const entry = Number(row.entry_price || 0)
  const side = row.side
  const pnl = (
    Number.isFinite(qty) && Number.isFinite(entry) && entry > 0
      ? side === 'long'
        ? (mark - entry) * qty
        : side === 'short'
          ? (entry - mark) * qty
          : Number(row.unrealized_pnl || 0)
      : Number(row.unrealized_pnl || 0)
  )
  const initialMargin = Number(row.initial_margin || 0)
  return {
    ...row,
    mark_price: mark,
    notional: Math.abs(qty * mark),
    unrealized_pnl: pnl,
    roi_pct: initialMargin > 0 ? (pnl / initialMargin) * 100 : row.roi_pct,
    market_realtime: true,
    market_price_kind: realtime.kind,
    market_ts_ms: marketTicks.value[row.symbol]?.ts || 0,
  }
}

function marketPriceLabel(row) {
  if (row.market_price_kind === 'mark') return '标记实时'
  if (row.market_price_kind === 'last') return '最新价'
  return '实时'
}

function estimatedClosePnl(row) {
  const qty = Number(row.contracts || 0)
  const entry = Number(row.entry_price || 0)
  const mark = Number(row.mark_price || 0)
  if (!Number.isFinite(qty) || !Number.isFinite(entry) || !Number.isFinite(mark)) return 0
  if (row.side === 'long') return (mark - entry) * qty
  if (row.side === 'short') return (entry - mark) * qty
  return 0
}

function closeMarketSocket(symbol) {
  const ws = marketSockets.get(symbol)
  if (!ws) return
  ws.onclose = null
  ws.onerror = null
  ws.onmessage = null
  try { ws.close() } catch (_) { /* ignore */ }
  marketSockets.delete(symbol)
  const next = { ...marketConnected.value }
  delete next[symbol]
  marketConnected.value = next
}

function closeAllMarketSockets() {
  for (const symbol of Array.from(marketSockets.keys())) closeMarketSocket(symbol)
}

function connectMarketSocket(symbol) {
  if (!symbol || marketSockets.has(symbol) || document.hidden) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const environment = getEnvironment()
  let ws
  try {
    ws = new WebSocket(`${proto}://${location.host}${wsPath(`/market?symbol=${encodeURIComponent(symbol)}&source=${environment}`)}`)
  } catch (_) { return }
  marketSockets.set(symbol, ws)
  ws.onopen = () => {
    marketConnected.value = { ...marketConnected.value, [symbol]: true }
  }
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type !== 'ticker') return
      const mark = Number(msg.mark || 0)
      const last = Number(msg.last || 0)
      if ((!Number.isFinite(mark) || mark <= 0) && (!Number.isFinite(last) || last <= 0)) return
      marketTicks.value = {
        ...marketTicks.value,
        [symbol]: {
          mark: Number.isFinite(mark) && mark > 0 ? mark : null,
          last: Number.isFinite(last) && last > 0 ? last : null,
          ts: Number(msg.ts || Date.now()),
        },
      }
    } catch (_) { /* ignore */ }
  }
  ws.onclose = () => {
    marketSockets.delete(symbol)
    const next = { ...marketConnected.value }
    delete next[symbol]
    marketConnected.value = next
  }
  ws.onerror = () => { try { ws.close() } catch (_) { /* ignore */ } }
}

function syncMarketSockets() {
  const symbols = new Set(positions.value.map((row) => row.symbol).filter(Boolean))
  for (const symbol of Array.from(marketSockets.keys())) {
    if (!symbols.has(symbol)) closeMarketSocket(symbol)
  }
  for (const symbol of symbols) {
    connectMarketSocket(symbol)
  }
}

function onEnvironmentChange() {
  closeAllMarketSockets()
  marketTicks.value = {}
  syncMarketSockets()
}

function onVisibilityChange() {
  if (document.hidden) {
    closeAllMarketSockets()
    return
  }
  syncMarketSockets()
}

watch(
  () => positions.value.map((row) => row.symbol).sort().join(','),
  syncMarketSockets,
  { immediate: true },
)

window.addEventListener('binance-trade-environment-change', onEnvironmentChange)
document.addEventListener('visibilitychange', onVisibilityChange)

onUnmounted(() => {
  window.removeEventListener('binance-trade-environment-change', onEnvironmentChange)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  closeAllMarketSockets()
})

function protection(row, kind) {
  return kind === 'SL' ? row.protection?.sl : row.protection?.tp
}

function protectionOrders(row, kind) {
  const key = kind === 'SL' ? 'sl_orders' : 'tp_orders'
  const orders = row.protection?.[key]
  if (Array.isArray(orders) && orders.length) return orders
  const fallback = protection(row, kind)
  return fallback ? [fallback] : []
}

function needsRepair(row) {
  return Boolean(row.protection?.missing_sl || row.protection?.missing_tp)
}

function protectionNeedsAttention(row) {
  return row.protection?.status !== 'COMPLETE'
}

function missingProtectionText(row) {
  const missing = []
  if (row.protection?.missing_sl) missing.push('止损')
  if (row.protection?.missing_tp) missing.push('止盈')
  return missing.length ? `缺少${missing.join('、')}` : '保护完整'
}

function protectionTag(order) {
  if (!order) return 'danger'
  return { placed: 'success', filled: 'primary', canceled: 'danger', expired: 'warning' }[order.status] || 'info'
}

function protectionText(order) {
  if (!order) return '未挂出'
  const price = order.trigger_price || order.price
  const qty = order.close_position ? '全仓' : `${fmt(order.qty)}`
  return `${orderStatusLabel({ client_kind: order.kind, status: order.status })} @ ${fmt(price, 2)} · ${qty}`
}

function protectionOriginText(row) {
  return {
    ENGINE: 'Engine 管理',
    EXTERNAL: 'Binance 外部',
    MIXED: '混合管理',
  }[row.protection?.authority] || '未识别'
}

function protectionStatusText(row) {
  return {
    COMPLETE: '保护完整',
    PARTIAL_TP_COVERAGE: '止盈部分覆盖',
    MISSING_SL: '缺少止损',
    MISSING_TP: '缺少止盈',
    CONFLICT: '保护单冲突',
  }[row.protection?.status] || '状态未知'
}

function tpCoverageText(row) {
  const pct = Number(row.protection?.tp_coverage_pct || 0) * 100
  const runner = Number(row.protection?.runner_qty || 0)
  return `覆盖 ${pct.toFixed(2)}% · Runner ${fmt(runner)}`
}

function isRepairing(row) {
  return Boolean(repairing.value[row.symbol])
}

async function repairProtection(row) {
  const symbol = row.symbol
  if (!symbol) return
  repairing.value = { ...repairing.value, [symbol]: true }
  try {
    const res = await api.command('REPAIR_SL_TP', symbol)
    ElMessage.success(`${symbol} 补止盈止损命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`补单命令下发失败: ${e.message}`)
  } finally {
    const next = { ...repairing.value }
    delete next[symbol]
    repairing.value = next
  }
}

function openTakeover(row) {
  const existingTargets = protectionOrders(row, 'TP').map((order, index) => ({
    leg_id: `TP${index + 1}`,
    trigger_price: order.trigger_price || order.price || '',
    position_pct: Number(row.contracts || 0) > 0
      ? Number(order.qty || 0) / Number(row.contracts)
      : 0,
  }))
  takeoverRow.value = row
  takeoverForm.value = {
    qty: row.contracts || '',
    sl: protectionOrders(row, 'SL')[0]?.trigger_price
      || protectionOrders(row, 'SL')[0]?.price
      || '',
    tpTargets: existingTargets.length
      ? existingTargets
      : [{ leg_id: 'TP1', trigger_price: '', position_pct: 1 }],
    confirm: false,
  }
  takeoverVisible.value = true
}

function addTakeProfitTarget() {
  if (takeoverForm.value.tpTargets.length >= 3) return
  const index = takeoverForm.value.tpTargets.length + 1
  takeoverForm.value.tpTargets.push({
    leg_id: `TP${index}`,
    trigger_price: '',
    position_pct: 0,
  })
}

function removeTakeProfitTarget(index) {
  takeoverForm.value.tpTargets.splice(index, 1)
  takeoverForm.value.tpTargets.forEach((target, idx) => {
    target.leg_id = `TP${idx + 1}`
  })
}

function recomputeTakeover() {
  const row = takeoverRow.value
  if (!row) return
  const mark = Number(row.mark_price || 0)
  const entry = Number(row.entry_price || 0)
  if (!Number.isFinite(mark) || mark <= 0 || !Number.isFinite(entry) || entry <= 0) return
  if (row.side === 'long') {
    takeoverForm.value.sl = (mark * 0.99).toFixed(2)
    takeoverForm.value.tpTargets = [
      { leg_id: 'TP1', trigger_price: (entry * 1.02).toFixed(2), position_pct: 0.5 },
      { leg_id: 'TP2', trigger_price: (entry * 1.04).toFixed(2), position_pct: 0.5 },
    ]
  } else if (row.side === 'short') {
    takeoverForm.value.sl = (mark * 1.01).toFixed(2)
    takeoverForm.value.tpTargets = [
      { leg_id: 'TP1', trigger_price: (entry * 0.98).toFixed(2), position_pct: 0.5 },
      { leg_id: 'TP2', trigger_price: (entry * 0.96).toFixed(2), position_pct: 0.5 },
    ]
  }
}

async function submitTakeover() {
  const row = takeoverRow.value
  if (!row) return
  if (!takeoverForm.value.confirm) {
    ElMessage.error('请先确认接管当前持仓')
    return
  }
  const sl = Number(takeoverForm.value.sl)
  const qty = Number(takeoverForm.value.qty)
  if (!Number.isFinite(sl) || sl <= 0 || !Number.isFinite(qty) || qty <= 0) {
    ElMessage.error('请输入有效的接管数量和止损触发价')
    return
  }
  const takeProfitTargets = takeoverForm.value.tpTargets
    .map((target, index) => ({
      leg_id: target.leg_id || `TP${index + 1}`,
      trigger_price: Number(target.trigger_price),
      position_pct: Number(target.position_pct),
    }))
    .filter((target) => Number.isFinite(target.trigger_price) && target.trigger_price > 0)
  const totalPct = takeProfitTargets.reduce((sum, target) => sum + target.position_pct, 0)
  if (takeProfitTargets.some((target) =>
    !Number.isFinite(target.position_pct) || target.position_pct <= 0
  ) || totalPct > 1.000001) {
    ElMessage.error('每档止盈比例必须大于 0，且合计不能超过 100%')
    return
  }
  const payload = {
    symbol: row.symbol,
    mode: 'manual',
    qty,
    sl_trigger: sl,
    confirm: true,
    replace_external: row.protection?.authority === 'EXTERNAL' || row.protection?.authority === 'MIXED',
    position: {
      side: row.side,
      qty: row.contracts,
      entry: row.entry_price,
    },
  }
  if (takeProfitTargets.length) payload.take_profit_targets = takeProfitTargets
  takeoverSubmitting.value = true
  try {
    const res = await api.command('PROTECT_POSITION', JSON.stringify(payload))
    ElMessage.success(`${row.symbol} 接管保护命令已入队 (#${res.id})`)
    takeoverVisible.value = false
  } catch (e) {
    ElMessage.error(`接管保护命令下发失败: ${e.message}`)
  } finally {
    takeoverSubmitting.value = false
  }
}

function openManualClose(row) {
  closeRow.value = row
  closeConfirm.value = false
  closeVisible.value = true
}

async function submitManualClose() {
  const row = closeRow.value
  if (!row) return
  if (!closeConfirm.value) {
    ElMessage.error('请先确认市价平仓')
    return
  }
  const payload = {
    symbol: row.symbol,
    confirm: true,
    force: true,
    position: {
      side: row.side,
      qty: row.contracts,
      entry: row.entry_price,
    },
  }
  closeSubmitting.value = true
  try {
    const res = await api.command('CLOSE_POSITION', JSON.stringify(payload))
    ElMessage.success(`${row.symbol} 手动平仓命令已入队 (#${res.id})`)
    closeVisible.value = false
  } catch (e) {
    ElMessage.error(`手动平仓命令下发失败: ${e.message}`)
  } finally {
    closeSubmitting.value = false
  }
}

const cancelOrderSubmitting = ref({})
function cancelOrderKey(symbol, orderId) {
  return `${symbol}::${orderId}`
}
function isCancelingOrder(symbol, orderId) {
  return Boolean(cancelOrderSubmitting.value[cancelOrderKey(symbol, orderId)])
}
async function cancelOpenOrder(order) {
  if (!order || !order.id) return
  const key = cancelOrderKey(order.symbol, order.id)
  cancelOrderSubmitting.value = { ...cancelOrderSubmitting.value, [key]: true }
  try {
    const payload = {
      symbol: order.symbol,
      order_id: order.id,
    }
    if (order.client_order_id) payload.client_order_id = order.client_order_id
    const res = await api.command('CANCEL_OPEN_ORDER', JSON.stringify(payload))
    ElMessage.success(`${order.symbol} 撤销挂单命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`撤销挂单失败: ${e.message}`)
  } finally {
    const next = { ...cancelOrderSubmitting.value }
    delete next[key]
    cancelOrderSubmitting.value = next
  }
}
async function cancelConditionOrder(order) {
  if (!order || !order.id) return
  const key = cancelOrderKey(order.symbol, order.id)
  cancelOrderSubmitting.value = { ...cancelOrderSubmitting.value, [key]: true }
  try {
    const payload = { symbol: order.symbol, algo_id: order.id }
    if (order.client_algo_id) payload.client_algo_id = order.client_algo_id
    const res = await api.command('CANCEL_CONDITION_ORDER', JSON.stringify(payload))
    ElMessage.success(`${order.symbol} 撤销条件单命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`撤销条件单失败: ${e.message}`)
  } finally {
    const next = { ...cancelOrderSubmitting.value }
    delete next[key]
    cancelOrderSubmitting.value = next
  }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="positions-header">
          <span>当前持仓（{{ displayPositions.length }}）</span>
          <span class="positions-meta">
            <el-tag :type="positionSourceTagType" size="small" effect="dark">
              {{ positionSourceLabel }}
            </el-tag>
            <span>账户同步于 {{ syncedAtText }}</span>
            <span v-if="hasRealtimeMarket">行情更新于 {{ marketSyncedAtText }}</span>
          </span>
        </div>
      </template>
      <el-alert
        v-if="positionsError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        :title="`交易所持仓同步失败，当前展示降级数据：${positionsError}`"
      />
      <el-alert
        v-if="conditionOrdersError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        :title="`交易所条件单同步失败：${conditionOrdersError}`"
      />
      <el-alert
        v-if="hasMissingProtection"
        type="error"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="存在持仓缺少止盈或止损条件单，请检查交易所后台或重新挂保护单"
      />
      <el-alert
        v-if="hasProtectionConflict"
        type="error"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="检测到保护单过度覆盖或多止损冲突；外部订单不会被系统自动撤销，请人工确认后接管"
      />
      <el-alert
        v-if="disabledWithPosition.size > 0"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
      >
        <template #title>
          检测到 {{ disabledWithPosition.size }} 个币种被禁用但仍有交易所持仓
          <span style="margin-left:8px">
            <el-button
              v-for="sym in [...disabledWithPosition]"
              :key="sym"
              size="small"
              type="success"
              :loading="enabling[sym]"
              @click="enableSymbol({ symbol: sym })"
            >
              启用 {{ sym }}
            </el-button>
          </span>
        </template>
      </el-alert>
      <div v-if="displayPositions.length" class="position-card-list">
        <article
          v-for="row in displayPositions"
          :key="row.symbol"
          class="position-card"
          :class="[`side-${row.side || 'flat'}`, { 'needs-attention': protectionNeedsAttention(row) || isSymbolDisabled(row.symbol) }]"
        >
          <div class="position-card-head">
            <div>
              <div class="position-symbol">{{ row.symbol }}</div>
              <div class="position-time">开仓于 {{ fmtTime(row.local_opened_at_ms) }}</div>
            </div>
            <div class="position-tags">
              <el-tag
                :type="row.side === 'long' ? 'success' : row.side === 'short' ? 'danger' : 'info'"
                effect="dark"
              >
                {{ row.side === 'long' ? '多仓' : row.side === 'short' ? '空仓' : '—' }}
              </el-tag>
              <el-tag size="small">{{ Number(row.leverage) > 0 ? `${row.leverage}x` : '—' }}</el-tag>
            </div>
          </div>

          <div class="position-pnl">
            <div>
              <span class="position-field-label">未实现盈亏</span>
              <strong class="mono" :class="Number(row.unrealized_pnl) >= 0 ? 'pnl-pos' : 'pnl-neg'">
                {{ fmt(row.unrealized_pnl, 2) }} USDT
              </strong>
            </div>
            <div>
              <span class="position-field-label">保证金收益率</span>
              <strong class="mono" :class="Number(row.roi_pct) >= 0 ? 'pnl-pos' : 'pnl-neg'">
                {{ fmtPct(row.roi_pct) }}
              </strong>
            </div>
          </div>

          <div class="position-detail-grid">
            <div><span>持仓数量</span><strong class="mono">{{ fmt(row.contracts) }}</strong></div>
            <div><span>保证金</span><strong class="mono">{{ fmt(margin(row), 2) }}</strong></div>
            <div><span>开仓价</span><strong class="mono">{{ fmt(row.entry_price, 2) }}</strong></div>
            <div>
              <span>标记价</span>
              <strong class="mono mark-value">
                {{ fmt(row.mark_price, 2) }}
                <el-tag v-if="row.market_realtime" type="success" size="small">
                  {{ marketPriceLabel(row) }}
                </el-tag>
              </strong>
            </div>
            <div><span>名义价值</span><strong class="mono">{{ fmt(row.notional, 2) }}</strong></div>
            <div><span>强平价格</span><strong class="mono">{{ fmt(row.liquidation_price, 2) }}</strong></div>
          </div>

          <div class="position-protection-grid">
            <div>
              <span class="position-field-label">止损保护</span>
              <div class="protection-order-list">
                <el-tag
                  v-for="order in protectionOrders(row, 'SL')"
                  :key="order.id || `${order.kind}-${order.trigger_price}`"
                  :type="protectionTag(order)"
                  size="small"
                >
                  {{ protectionText(order) }}
                </el-tag>
                <el-tag v-if="!protectionOrders(row, 'SL').length" type="danger" size="small">
                  未挂出
                </el-tag>
              </div>
            </div>
            <div>
              <span class="position-field-label">止盈保护</span>
              <div class="protection-order-list">
                <el-tag
                  v-for="(order, index) in protectionOrders(row, 'TP')"
                  :key="order.id || `${order.kind}-${order.trigger_price}`"
                  :type="protectionTag(order)"
                  size="small"
                >
                  TP{{ index + 1 }} · {{ protectionText(order) }}
                </el-tag>
                <el-tag v-if="!protectionOrders(row, 'TP').length" type="danger" size="small">
                  未挂出
                </el-tag>
              </div>
            </div>
            <div class="protection-summary">
              <span class="position-field-label">止盈覆盖</span>
              <strong class="mono">{{ tpCoverageText(row) }}</strong>
            </div>
            <div class="protection-summary">
              <span class="position-field-label">订单来源</span>
              <strong>{{ protectionOriginText(row) }}</strong>
            </div>
          </div>

          <div class="position-actions">
            <div class="position-actions__meta">
              <el-tag v-if="isSymbolDisabled(row.symbol)" type="warning" size="small" effect="dark">
                币种已禁用
              </el-tag>
              <el-tag
                v-else
                :type="row.protection?.status === 'COMPLETE' ? 'success' : 'warning'"
                size="small"
              >
                {{ protectionStatusText(row) }}
              </el-tag>
            </div>
            <div v-if="protectionNeedsAttention(row) || isSymbolDisabled(row.symbol)" class="position-actions__row">
              <template v-if="protectionNeedsAttention(row)">
                <el-button
                  v-if="needsRepair(row)"
                  type="danger"
                  size="small"
                  :icon="'CirclePlus'"
                  :loading="isRepairing(row)"
                  @click="repairProtection(row)"
                >
                  补止盈止损
                </el-button>
                <el-button size="small" @click="openTakeover(row)">接管保护</el-button>
              </template>
              <el-button
                v-if="isSymbolDisabled(row.symbol)"
                size="small"
                type="success"
                :loading="enabling[row.symbol]"
                @click="enableSymbol(row)"
              >
                启用并接管
              </el-button>
            </div>
            <div class="position-actions__row position-actions__row--full">
              <el-button type="danger" plain size="small" @click="openManualClose(row)">
                手动平仓
              </el-button>
            </div>
          </div>
        </article>
      </div>
      <el-empty v-else description="当前无持仓" :image-size="70" />
    </el-card>

    <el-dialog v-model="takeoverVisible" title="接管保护" width="520px">
      <el-form v-if="takeoverRow" label-width="120px">
        <el-alert
          v-if="['EXTERNAL', 'MIXED'].includes(takeoverRow.protection?.authority)"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom:12px"
          title="提交成功后，系统会先挂出新保护单，再撤销当前 Binance 外部保护单，并切换为 Engine 管理。"
        />
        <el-form-item label="币种">
          <span class="mono">{{ takeoverRow.symbol }}</span>
        </el-form-item>
        <el-form-item label="方向/数量">
          <span class="mono">{{ takeoverRow.side }} / {{ fmt(takeoverRow.contracts) }}</span>
        </el-form-item>
        <el-form-item label="开仓价/标记价">
          <span class="mono">{{ fmt(takeoverRow.entry_price, 2) }} / {{ fmt(takeoverRow.mark_price, 2) }}</span>
        </el-form-item>
        <el-form-item label="接管数量">
          <el-input v-model="takeoverForm.qty" class="protect-input" />
        </el-form-item>
        <el-form-item label="止损触发价">
          <el-input v-model="takeoverForm.sl" class="protect-input" />
        </el-form-item>
        <el-form-item label="分批止盈">
          <div class="take-profit-editor">
            <div
              v-for="(target, index) in takeoverForm.tpTargets"
              :key="target.leg_id"
              class="take-profit-row"
            >
              <span class="mono">{{ target.leg_id }}</span>
              <el-input v-model="target.trigger_price" placeholder="触发价" />
              <el-input-number
                v-model="target.position_pct"
                :min="0.01"
                :max="1"
                :step="0.05"
                :precision="2"
                controls-position="right"
              />
              <span>仓位比例</span>
              <el-button
                v-if="takeoverForm.tpTargets.length > 1"
                link
                type="danger"
                @click="removeTakeProfitTarget(index)"
              >
                删除
              </el-button>
            </div>
            <el-button
              v-if="takeoverForm.tpTargets.length < 3"
              size="small"
              @click="addTakeProfitTarget"
            >
              添加止盈档
            </el-button>
          </div>
        </el-form-item>
        <el-form-item>
          <el-button size="small" @click="recomputeTakeover">按当前价重算</el-button>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="takeoverForm.confirm">
            确认用以上触发价接管当前交易所持仓
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="takeoverVisible = false">取消</el-button>
        <el-button type="danger" :loading="takeoverSubmitting" @click="submitTakeover">
          提交接管
        </el-button>
      </template>
    </el-dialog>

    <el-card shadow="never" class="open-orders-card">
      <template #header>
        <div class="positions-header">
          <span>普通挂单（限价/未成交，交易所实时）</span>
          <span class="positions-meta">
            <el-tag :type="openOrdersError ? 'warning' : 'success'" size="small" effect="dark">
              {{ openOrdersError ? '同步失败' : '交易所实时' }}
            </el-tag>
            <span>同步于 {{ openOrdersSyncedText }}</span>
          </span>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="普通挂单是已进入交易所订单簿但尚未完全成交的普通订单；不包含止损/止盈条件单。"
      />
      <el-alert
        v-if="openOrdersError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        :title="`交易所普通挂单同步失败：${openOrdersError}`"
      />
      <div v-if="openOrders.length" class="mobile-order-list mobile-only">
        <article v-for="row in openOrders" :key="`${row.symbol}-${row.id}`" class="mobile-order-card">
          <div class="mobile-order-head">
            <strong>{{ row.symbol }}</strong>
            <div>
              <el-tag :type="row.side === 'buy' ? 'success' : 'danger'" size="small">
                {{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}
              </el-tag>
              <el-tag size="small">{{ row.status || 'placed' }}</el-tag>
            </div>
          </div>
          <div class="mobile-position-grid compact">
            <div><span>类型</span><strong>{{ row.order_type || 'LIMIT' }}</strong></div>
            <div><span>价格</span><strong class="mono">{{ Number(row.price || 0).toFixed(2) }}</strong></div>
            <div><span>数量</span><strong class="mono">{{ Number(row.qty || 0).toFixed(4) }}</strong></div>
            <div><span>已成交</span><strong class="mono">{{ Number(row.filled_qty || 0).toFixed(4) }}</strong></div>
          </div>
          <el-button
            type="danger"
            size="small"
            plain
            :loading="isCancelingOrder(row.symbol, row.id)"
            @click="cancelOpenOrder(row)"
          >
            撤销挂单
          </el-button>
        </article>
      </div>
      <el-empty v-else class="mobile-only compact-empty" description="当前无普通挂单" :image-size="60" />
      <el-table
        class="desktop-only"
        :data="openOrders"
        stripe
        size="small"
        empty-text="当前无普通挂单"
      >
        <el-table-column prop="symbol" label="标的" width="110" />
        <el-table-column label="方向" width="70">
          <template #default="{ row }">
            <el-tag :type="row.side === 'buy' ? 'success' : 'danger'" size="small">
              {{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <span class="mono">{{ row.order_type || 'LIMIT' }}</span>
            <el-tag v-if="row.reduce_only" type="info" size="small" style="margin-left:4px">reduce-only</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="价格" width="120">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.price || 0).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="数量" width="110">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.qty || 0).toFixed(4) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已成交" width="110">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.filled_qty || 0).toFixed(4) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.status || 'placed' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              type="danger"
              size="small"
              plain
              :loading="isCancelingOrder(row.symbol, row.id)"
              @click="cancelOpenOrder(row)"
            >
              撤销
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="condition-orders-section">
        <div class="section-title">条件单（止损/止盈，触发后市价执行）</div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          style="margin-bottom:12px"
          title="条件单不会提前进入普通订单簿，只有达到触发价后才由交易所触发执行；这里主要展示 SL/TP 保护单明细。"
        />
        <div v-if="(live.summary?.condition_orders || []).length" class="mobile-order-list mobile-only">
          <article
            v-for="row in live.summary?.condition_orders || []"
            :key="`${row.symbol}-${row.id}`"
            class="mobile-order-card"
          >
            <div class="mobile-order-head">
              <strong>{{ row.symbol }}</strong>
              <div>
                <el-tag :type="row.kind === 'SL' ? 'danger' : 'success'" size="small">{{ row.kind || '—' }}</el-tag>
                <el-tag size="small">{{ orderStatusLabel({ client_kind: row.kind, status: row.status || 'placed' }) }}</el-tag>
              </div>
            </div>
            <div class="mobile-position-grid compact">
              <div><span>方向</span><strong>{{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}</strong></div>
              <div><span>触发价</span><strong class="mono">{{ Number(row.trigger_price || row.price || 0).toFixed(2) }}</strong></div>
              <div><span>数量</span><strong class="mono">{{ Number(row.qty || 0).toFixed(4) }}</strong></div>
              <div><span>类型</span><strong>{{ row.kind || '—' }}</strong></div>
            </div>
            <el-button
              type="danger"
              size="small"
              plain
              :loading="isCancelingOrder(row.symbol, row.id)"
              @click="cancelConditionOrder(row)"
            >
              撤销条件单
            </el-button>
          </article>
        </div>
        <el-empty
          v-else
          class="mobile-only compact-empty"
          description="当前无 SL/TP 条件单"
          :image-size="60"
        />
        <el-table
          class="desktop-only"
          :data="live.summary?.condition_orders || []"
          stripe
          size="small"
          empty-text="当前无 SL/TP 条件单"
        >
          <el-table-column prop="symbol" label="标的" width="110" />
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.kind === 'SL' ? 'danger' : 'success'" size="small">
                {{ row.kind || '—' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="方向" width="70">
            <template #default="{ row }">
              <span class="mono">{{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="触发价" width="120">
            <template #default="{ row }">
              <span class="mono">{{ Number(row.trigger_price || row.price || 0).toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="数量" width="110">
            <template #default="{ row }">
              <span class="mono">{{ Number(row.qty || 0).toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small">{{ orderStatusLabel({ client_kind: row.kind, status: row.status || 'placed' }) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <el-button
                type="danger"
                size="small"
                plain
                :loading="isCancelingOrder(row.symbol, row.id)"
                @click="cancelConditionOrder(row)"
              >
                撤销
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-dialog v-model="closeVisible" title="手动平仓" width="520px">
      <el-form v-if="closeRow" label-width="120px">
        <el-form-item label="币种">
          <span class="mono">{{ closeRow.symbol }}</span>
        </el-form-item>
        <el-form-item label="方向/数量">
          <span class="mono">{{ closeRow.side }} / {{ fmt(closeRow.contracts) }}</span>
        </el-form-item>
        <el-form-item label="开仓价/标记价">
          <span class="mono">{{ fmt(closeRow.entry_price, 2) }} / {{ fmt(closeRow.mark_price, 2) }}</span>
        </el-form-item>
        <el-form-item label="杠杆">
          <span class="mono">{{ Number(closeRow.leverage) > 0 ? `${closeRow.leverage}x` : '—' }}</span>
        </el-form-item>
        <el-form-item label="预计盈亏">
          <span class="mono" :class="estimatedClosePnl(closeRow) >= 0 ? 'pnl-pos' : 'pnl-neg'">
            {{ fmt(estimatedClosePnl(closeRow), 2) }} USDT
          </span>
        </el-form-item>
        <el-form-item label="保护单处理">
          <span>平仓确认后撤销该币种关联止盈止损条件单</span>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="closeConfirm">
            确认强制市价 reduce-only 平掉当前交易所持仓（绕过滑点保护）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeVisible = false">取消</el-button>
        <el-button type="danger" :loading="closeSubmitting" @click="submitManualClose">
          强制市价平仓
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.positions-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.positions-meta {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: #909399;
  font-size: 13px;
}

.protect-input {
  width: 180px;
}

.take-profit-editor {
  display: grid;
  width: 100%;
  gap: 8px;
}

.take-profit-row {
  display: grid;
  grid-template-columns: 38px minmax(100px, 1fr) 130px auto auto;
  align-items: center;
  gap: 8px;
}

.open-orders-card {
  margin-top: 16px;
}

.mark-value {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.condition-orders-section {
  margin-top: 16px;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}

.position-card-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 12px;
}

.position-card {
  min-width: 0;
  padding: 14px;
  overflow: hidden;
  background: var(--bt-card);
  border: 1px solid var(--bt-border);
  border-left: 4px solid var(--bt-muted);
  border-radius: 10px;
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.05);
}

.position-card.side-long {
  border-left-color: #16a34a;
}

.position-card.side-short {
  border-left-color: #dc2626;
}

.position-card.needs-attention {
  border-top-color: #e6a23c;
  border-right-color: #e6a23c;
  border-bottom-color: #e6a23c;
}

.position-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.position-symbol {
  font-size: 18px;
  font-weight: 700;
}

.position-time {
  margin-top: 3px;
  color: var(--bt-muted);
  font-size: 11px;
}

.position-tags {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
}

.position-pnl {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
  margin: 12px 0;
  padding: 10px;
  background: color-mix(in srgb, var(--bt-primary) 7%, transparent);
  border-radius: 8px;
}

.position-pnl > div,
.position-protection-grid > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.position-pnl strong {
  font-size: 16px;
  overflow-wrap: anywhere;
}

.position-field-label {
  color: var(--bt-muted);
  font-size: 11px;
}

.position-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  background: var(--bt-border);
  border: 1px solid var(--bt-border);
  border-radius: 8px;
}

.position-detail-grid > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px;
  background: var(--bt-card);
}

.position-detail-grid span {
  color: var(--bt-muted);
  font-size: 11px;
}

.position-detail-grid strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
}

.position-protection-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
  margin-top: 10px;
}

.position-protection-grid .el-tag {
  max-width: 100%;
  height: auto;
  min-height: 24px;
  padding: 3px 7px;
  white-space: normal;
  line-height: 1.25;
}

.protection-order-list {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.protection-summary strong {
  font-size: 12px;
  overflow-wrap: anywhere;
}

.position-actions {
  display: grid;
  gap: 8px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--bt-border);
}

.position-actions__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.position-actions__row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.position-actions__row--full {
  display: block;
}

.position-actions .el-tag {
  max-width: 100%;
  white-space: normal;
  line-height: 1.3;
}

.mobile-only {
  display: none;
}

@media (max-width: 767px) {
  .desktop-only {
    display: none;
  }

  .mobile-only {
    display: block;
  }

  .positions-header,
  .positions-meta {
    align-items: flex-start;
  }

  .positions-meta {
    flex-wrap: wrap;
  }

  .protect-input {
    width: 100%;
  }

  .mobile-order-list {
    display: grid;
    gap: 10px;
  }

  .mobile-order-card {
    min-width: 0;
    padding: 12px;
    overflow: hidden;
    background: var(--bt-card);
    border: 1px solid var(--bt-border);
    border-left: 4px solid var(--bt-muted);
    border-radius: 10px;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.05);
  }

  .mobile-order-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
  }

  .mobile-order-head > div {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 5px;
  }

  .mobile-position-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    overflow: hidden;
    background: var(--bt-border);
    border: 1px solid var(--bt-border);
    border-radius: 8px;
  }

  .mobile-position-grid > div {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 8px;
    background: var(--bt-card);
  }

  .mobile-position-grid span {
    color: var(--bt-muted);
    font-size: 11px;
  }

  .mobile-position-grid strong {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: 13px;
  }

  .mobile-position-grid.compact {
    margin: 10px 0;
  }

  .position-card-list {
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
  }

  .position-card {
    padding: 12px;
  }

  .position-actions__row {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .position-actions__row--full {
    grid-template-columns: minmax(0, 1fr);
  }

  .position-actions .el-tag,
  .position-actions .el-button,
  .mobile-order-card > .el-button {
    min-height: 36px;
  }

  .position-actions .el-button {
    width: 100%;
    min-width: 0;
    white-space: normal;
  }

  .mobile-order-card {
    border-left-color: var(--bt-primary);
  }

  .compact-empty {
    padding: 8px 0;
  }
}
</style>
