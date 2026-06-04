<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { init, dispose } from 'klinecharts'
import * as echarts from 'echarts'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const CFG_SYMBOLS = ref([])
const symbol = ref('BTCUSDT')
const timeframe = ref('5m')
const timeframes = ['1m', '5m', '15m', '1h', '4h']
const loading = ref(false)
const lastPrice = ref(null)
const change24h = ref(null)

const klineEl = ref(null)
const priceEl = ref(null)
let kchart = null
let pchart = null
let tickerTimer = null
let klineTimer = null
const priceSeries = []   // [{t, v}] 实时价格点，滚动窗口

function applyKline(resp) {
  const bars = (resp.klines || []).map(k => ({
    timestamp: k[0], open: k[1], high: k[2], low: k[3], close: k[4], volume: k[5],
  }))
  kchart.applyNewData(bars)
  kchart.createIndicator('EMA', false, { id: 'candle_pane' })
  kchart.createIndicator('BOLL', false, { id: 'candle_pane' })
  kchart.createIndicator('VOL')
  kchart.createIndicator('MACD')
  kchart.createIndicator('RSI')
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
    applyKline(await api.klines(symbol.value, timeframe.value, 300))
  } catch (e) {
    ElMessage.error(`加载K线失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function renderPrice() {
  if (!pchart) return
  pchart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: priceSeries.map(p => p.t),
             axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true },
    series: [{
      name: '最新价', type: 'line', smooth: true, showSymbol: false,
      data: priceSeries.map(p => p.v),
      lineStyle: { color: '#409eff', width: 2 }, areaStyle: { opacity: 0.08 },
    }],
  })
}

async function pollTicker() {
  try {
    const t = await api.ticker(symbol.value)
    if (t.last != null) {
      lastPrice.value = t.last
      change24h.value = t.change_24h_pct
      const now = new Date().toLocaleTimeString()
      priceSeries.push({ t: now, v: t.last })
      if (priceSeries.length > 120) priceSeries.shift()  // 保留最近2分钟(1s*120)
      renderPrice()
    }
  } catch (_) { /* 单次失败忽略 */ }
}

function switchSymbol() {
  priceSeries.length = 0
  loadKline()
  pollTicker()
}

onMounted(async () => {
  const cfg = await api.config().catch(() => null)
  CFG_SYMBOLS.value = cfg ? cfg.symbols : ['BTCUSDT']
  kchart = init(klineEl.value)
  pchart = echarts.init(priceEl.value)
  await loadKline()
  await pollTicker()
  tickerTimer = setInterval(pollTicker, 1000)        // 实时价格每秒
  klineTimer = setInterval(loadKline, 30000)         // K线每30s刷新
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  if (tickerTimer) clearInterval(tickerTimer)
  if (klineTimer) clearInterval(klineTimer)
  window.removeEventListener('resize', onResize)
  if (kchart) dispose(klineEl.value)
  if (pchart) pchart.dispose()
})
function onResize() { if (pchart) pchart.resize() }
</script>

<template>
  <div class="page">
    <div style="margin-bottom:12px; display:flex; gap:12px; align-items:center; flex-wrap:wrap">
      <el-select v-model="symbol" style="width:140px" @change="switchSymbol">
        <el-option v-for="s in CFG_SYMBOLS" :key="s" :label="s" :value="s" />
      </el-select>
      <el-radio-group v-model="timeframe" @change="loadKline">
        <el-radio-button v-for="t in timeframes" :key="t" :value="t">{{ t }}</el-radio-button>
      </el-radio-group>
      <el-button :loading="loading" @click="loadKline" :icon="'Refresh'">刷新K线</el-button>
      <span v-if="lastPrice !== null" style="font-size:20px; font-weight:600" class="mono">
        {{ Number(lastPrice).toLocaleString() }}
      </span>
      <el-tag v-if="change24h !== null" :type="Number(change24h) >= 0 ? 'success' : 'danger'"
              effect="dark">
        24h {{ Number(change24h) >= 0 ? '+' : '' }}{{ Number(change24h).toFixed(2) }}%
      </el-tag>
      <span style="color:#909399; font-size:12px">价格每秒刷新 · K线30秒刷新</span>
    </div>

    <el-card shadow="never" style="margin-bottom:12px">
      <template #header>实时价格走势（每秒，{{ symbol }}）</template>
      <div ref="priceEl" style="height:200px; width:100%"></div>
    </el-card>

    <el-card shadow="never" body-style="padding:0">
      <template #header>
        K线图 · 主图叠加 EMA/BOLL，副图 VOL/MACD/RSI；绿/红线=持仓开仓价
      </template>
      <div ref="klineEl" style="height:520px; width:100%"></div>
    </el-card>
  </div>
</template>
