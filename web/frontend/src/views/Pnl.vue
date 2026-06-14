<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'
import { DEFAULT_TIME_RANGE, QUICK_TIME_RANGES } from '../timeRanges'

const stats = ref(null)
const barEl = ref(null)
const range = ref(DEFAULT_TIME_RANGE)
let chart = null

function fmt(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}

function pnlClass(value) {
  const n = Number(value || 0); return n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : ''
}

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

async function load() {
  stats.value = await api.pnl({ range: range.value }).catch(() => null)
  if (chart && stats.value) {
    const bySym = stats.value.close_by_symbol || {}
    const textColor = cssVar('--bt-text', '#303133')
    const mutedColor = cssVar('--bt-muted', '#909399')
    const gridColor = cssVar('--bt-border', '#e5e7eb')
    const barColor = cssVar('--bt-primary', '#409eff')
    chart.setOption({
      tooltip: { backgroundColor: cssVar('--bt-card', '#ffffff'), textStyle: { color: textColor } },
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      xAxis: {
        type: 'category',
        data: Object.keys(bySym),
        axisLabel: { color: mutedColor },
        axisLine: { lineStyle: { color: gridColor } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: mutedColor },
        splitLine: { lineStyle: { color: gridColor } },
      },
      series: [{ name: '平仓笔数', type: 'bar', data: Object.values(bySym),
                 itemStyle: { color: barColor } }],
    })
  }
}

function onRangeChange() {
  load().catch(() => {})
}

function onThemeChange() {
  load().catch(() => {})
}

onMounted(async () => {
  chart = echarts.init(barEl.value)
  await load()
  window.addEventListener('resize', resize)
  window.addEventListener('binance-trade-theme-change', onThemeChange)
})
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  window.removeEventListener('binance-trade-theme-change', onThemeChange)
  if (chart) chart.dispose()
})
function resize() { if (chart) chart.resize() }
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">当日权益变化 (USDT)</div>
          <div class="value" :class="pnlClass(stats?.day_equity_change)">
            {{ stats ? fmt(stats.day_equity_change) : '—' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">当日未实现盈亏 (USDT)</div>
          <div class="value" :class="pnlClass(stats?.day_unrealized_pnl)">
            {{ stats ? fmt(stats.day_unrealized_pnl) : '—' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">范围已实现盈亏 (USDT)</div>
          <div class="value" :class="pnlClass(stats?.range_net_realized_pnl)">
            {{ stats ? fmt(stats.range_net_realized_pnl) : '—' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">范围交易数 / 平仓笔数</div>
          <div class="value">{{ stats ? `${stats.range_trade_count} / ${stats.range_close_count}` : '—' }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div class="card-header-row">
          <span>各币种平仓笔数</span>
          <div class="toolbar-row">
            <el-radio-group v-model="range" size="small" @change="onRangeChange">
              <el-radio-button
                v-for="item in QUICK_TIME_RANGES"
                :key="item.value"
                :value="item.value"
              >
                {{ item.label }}
              </el-radio-button>
            </el-radio-group>
            <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
          </div>
        </div>
      </template>
      <div ref="barEl" style="height:320px"></div>
      <div style="color:#909399; font-size:12px; margin-top:8px">
        说明：当日权益变化按 UTC+8 日界统计 total_equity 差值；当日未实现盈亏优先来自交易所实时持仓，失败时回退到最近持仓快照。
      </div>
    </el-card>
  </div>
</template>
