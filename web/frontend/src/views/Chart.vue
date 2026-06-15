<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { init, dispose } from 'klinecharts'
import * as echarts from 'echarts'
import { api, wsPath } from '../api'
import { ElMessage } from 'element-plus'

const CFG_SYMBOLS = ref([])
const symbol = ref('BTCUSDT')
const timeframe = ref('5m')
const marketSource = ref('mainnet')
const timeframes = ['1m', '5m', '15m', '1h', '4h']
const sources = [
  { label: 'Mainnet', value: 'mainnet' },
  { label: 'Testnet', value: 'testnet' },
]
const loading = ref(false)
const marketConnected = ref(false)
const lastPrice = ref(null)
const change24h = ref(null)

const klineEl = ref(null)
const priceEl = ref(null)
let kchart = null
let pchart = null
let marketWs = null
let reconnectTimer = null
let resyncTimer = null
let stopped = false
let indicatorsCreated = false
const priceSeries = []   // [{t: timestamp_ms, v: price}]

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

function toBar(k) {
  return {
    timestamp: k[0], open: k[1], high: k[2], low: k[3], close: k[4], volume: k[5],
  }
}

function upsertPricePoint(ts, price) {
  if (price == null) return
  const t = Number(ts) || Date.now()
  const v = Number(price)
  if (!Number.isFinite(v)) return
  const last = priceSeries[priceSeries.length - 1]
  if (last && Math.abs(last.t - t) < 1000) {
    last.t = t
    last.v = v
  } else {
    priceSeries.push({ t, v })
    if (priceSeries.length > 1500) priceSeries.shift()
  }
  renderPrice()
}

function seedPriceSeries(bars) {
  priceSeries.length = 0
  bars.forEach(b => priceSeries.push({ t: b.timestamp, v: b.close }))
  renderPrice()
}

function ensureIndicators() {
  if (indicatorsCreated || !kchart) return
  kchart.createIndicator('EMA', false, { id: 'candle_pane' })
  kchart.createIndicator('BOLL', false, { id: 'candle_pane' })
  kchart.createIndicator('VOL')
  kchart.createIndicator('MACD')
  kchart.createIndicator('RSI')
  indicatorsCreated = true
}

function applyKline(resp) {
  const bars = (resp.klines || []).map(toBar)
  kchart.applyNewData(bars)
  ensureIndicators()
  seedPriceSeries(bars)
  const last = bars[bars.length - 1]
  if (last) lastPrice.value = last.close

  const pos = resp.position
  if (pos && pos.entry_price) {
    kchart.createOverlay({
      name: 'priceLine',
      points: [{ value: Number(pos.entry_price) }],
      lock: true,
      styles: { line: { color: pos.side === 'long' ? '#16a34a' : '#dc2626' } },
      extendData: `开仓 ${pos.side} @${pos.entry_price}`,
    })
  }
}

async function loadKline() {
  loading.value = true
  try {
    applyKline(await api.klines(symbol.value, timeframe.value, 1000, marketSource.value))
  } catch (e) {
    ElMessage.error(`加载K线失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function renderPrice() {
  if (!pchart) return
  const textColor = cssVar('--bt-text', '#303133')
  const mutedColor = cssVar('--bt-muted', '#909399')
  const gridColor = cssVar('--bt-border', '#e5e7eb')
  const lineColor = cssVar('--bt-primary', '#409eff')
  pchart.setOption({
    animation: false,
    textStyle: { color: textColor },
    tooltip: {
      trigger: 'axis',
      backgroundColor: cssVar('--bt-card', '#ffffff'),
      borderColor: gridColor,
      textStyle: { color: textColor },
    },
    grid: { left: 64, right: 24, top: 20, bottom: 34 },
    xAxis: {
      type: 'time',
      axisLabel: { fontSize: 10, color: mutedColor },
      axisLine: { lineStyle: { color: gridColor } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: mutedColor },
      splitLine: { lineStyle: { color: gridColor } },
    },
    series: [{
      name: '最新价',
      type: 'line',
      smooth: true,
      showSymbol: false,
      data: priceSeries.map(p => [p.t, p.v]),
      lineStyle: { color: lineColor, width: 2 },
      itemStyle: { color: lineColor },
      areaStyle: { color: lineColor, opacity: 0.1 },
    }],
  })
}

function applyTicker(t) {
  if (t.last != null) {
    lastPrice.value = t.last
    upsertPricePoint(t.ts || Date.now(), t.last)
  }
  if (t.change_24h_pct != null) change24h.value = t.change_24h_pct
}

function applyRealtimeKline(k) {
  if (!k || !kchart) return
  const bar = toBar(k)
  try {
    kchart.updateData(bar)
  } catch (_) {
    loadKline()
  }
  lastPrice.value = bar.close
  upsertPricePoint(bar.timestamp, bar.close)
}

function marketWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const q = new URLSearchParams({
    source: marketSource.value,
    symbol: symbol.value,
    timeframe: timeframe.value,
  })
  return `${proto}://${location.host}${wsPath('/market')}?${q.toString()}`
}

function closeMarketWs() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  if (marketWs) {
    marketWs.onclose = null
    try { marketWs.close() } catch (_) { /* ignore */ }
    marketWs = null
  }
  marketConnected.value = false
}

function startResyncTimer() {
  if (resyncTimer) return
  resyncTimer = setInterval(loadKline, 300000)
}

function stopResyncTimer() {
  if (!resyncTimer) return
  clearInterval(resyncTimer)
  resyncTimer = null
}

function connectMarketWs() {
  if (stopped || document.hidden) return
  closeMarketWs()
  try {
    marketWs = new WebSocket(marketWsUrl())
  } catch (_) {
    scheduleReconnect()
    return
  }
  marketWs.onopen = () => { marketConnected.value = true }
  marketWs.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'ticker') applyTicker(msg)
      if (msg.type === 'kline') applyRealtimeKline(msg.kline)
    } catch (_) { /* ignore */ }
  }
  marketWs.onclose = () => {
    marketConnected.value = false
    scheduleReconnect()
  }
  marketWs.onerror = () => { try { marketWs.close() } catch (_) { /* ignore */ } }
}

function scheduleReconnect() {
  if (stopped || document.hidden || reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connectMarketWs()
  }, 2500)
}

async function reloadMarket() {
  priceSeries.length = 0
  closeMarketWs()
  await loadKline()
  connectMarketWs()
}

function onResize() {
  if (pchart) pchart.resize()
  if (kchart) kchart.resize()
}

function onThemeChange() {
  renderPrice()
}

function pauseMarketTransport() {
  stopResyncTimer()
  closeMarketWs()
}

function resumeMarketTransport() {
  startResyncTimer()
  connectMarketWs()
}

onMounted(async () => {
  const cfg = await api.config().catch(() => null)
  CFG_SYMBOLS.value = cfg ? cfg.symbols : ['BTCUSDT']
  marketSource.value = cfg?.market_source || 'mainnet'
  kchart = init(klineEl.value)
  pchart = echarts.init(priceEl.value)
  await reloadMarket()
  startResyncTimer()
  window.addEventListener('resize', onResize)
  window.addEventListener('binance-trade-theme-change', onThemeChange)
  window.addEventListener('binance-trade-pause-live', pauseMarketTransport)
  window.addEventListener('binance-trade-resume-live', resumeMarketTransport)
})

onUnmounted(() => {
  stopped = true
  closeMarketWs()
  stopResyncTimer()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('binance-trade-theme-change', onThemeChange)
  window.removeEventListener('binance-trade-pause-live', pauseMarketTransport)
  window.removeEventListener('binance-trade-resume-live', resumeMarketTransport)
  if (kchart) dispose(klineEl.value)
  if (pchart) pchart.dispose()
})
</script>

<template>
  <div class="page">
    <div class="chart-toolbar">
      <el-select v-model="symbol" style="width:140px" @change="reloadMarket">
        <el-option v-for="s in CFG_SYMBOLS" :key="s" :label="s" :value="s" />
      </el-select>
      <el-radio-group v-model="marketSource" @change="reloadMarket">
        <el-radio-button v-for="s in sources" :key="s.value" :value="s.value">
          {{ s.label }}
        </el-radio-button>
      </el-radio-group>
      <el-radio-group v-model="timeframe" @change="reloadMarket">
        <el-radio-button v-for="t in timeframes" :key="t" :value="t">{{ t }}</el-radio-button>
      </el-radio-group>
      <el-button :loading="loading" @click="reloadMarket" :icon="'Refresh'">刷新K线</el-button>
      <el-tag :type="marketConnected ? 'success' : 'warning'" effect="dark">
        {{ marketConnected ? '行情WS' : '重连中' }}
      </el-tag>
      <span v-if="lastPrice !== null" class="price-value mono">
        {{ Number(lastPrice).toLocaleString() }}
      </span>
      <el-tag v-if="change24h !== null" :type="Number(change24h) >= 0 ? 'success' : 'danger'"
              effect="dark">
        24h {{ Number(change24h) > 0 ? '+' : '' }}{{ Number(change24h).toFixed(2) }}%
      </el-tag>
    </div>

    <el-card shadow="never" style="margin-bottom:12px">
      <template #header>{{ marketSource.toUpperCase() }} · {{ symbol }} 价格走势</template>
      <div ref="priceEl" style="height:220px; width:100%"></div>
    </el-card>

    <el-card shadow="never" body-style="padding:0">
      <template #header>
        {{ marketSource.toUpperCase() }} · {{ symbol }} {{ timeframe }} K线
      </template>
      <div ref="klineEl" style="height:560px; width:100%"></div>
    </el-card>
  </div>
</template>

<style scoped>
.chart-toolbar {
  margin-bottom: 12px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}

.price-value {
  color: var(--bt-text);
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
  transition: color 0.2s ease;
}
</style>
