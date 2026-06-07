<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import * as echarts from 'echarts'
import { useLiveStore } from '../stores/live'
import { api } from '../api'
import { decisionLabel, localTime } from '../labels'

const live = useLiveStore()
const cfg = ref(null)
const equityEl = ref(null)
let chart = null

const bal = computed(() => live.balance || {})
const positions = computed(() => live.positions || [])
const dayPnl = computed(() => Number(bal.value.day_realized_pnl || 0))
const lastDecision = computed(() => (live.summary.recent_decisions || [])[0] || null)

function fmt(n, d = 2) {
  if (n === null || n === undefined || n === '') return '—'
  return Number(n).toFixed(d)
}

async function loadEquity() {
  const data = await api.equity(500)
  if (!chart) return
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: data.map(d => d.created_at), axisLabel: { show: false } },
    yAxis: { type: 'value', scale: true },
    series: [{
      name: '权益', type: 'line', smooth: true, showSymbol: false,
      data: data.map(d => d.total_equity), areaStyle: { opacity: 0.1 },
      lineStyle: { color: '#409eff' }, itemStyle: { color: '#409eff' },
    }],
  })
}

onMounted(async () => {
  cfg.value = await api.config().catch(() => null)
  chart = echarts.init(equityEl.value)
  await loadEquity().catch(() => {})
  window.addEventListener('resize', resize)
})
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  if (chart) chart.dispose()
})
function resize() { if (chart) chart.resize() }
// 余额每次推送变化时刷新权益曲线
watch(() => bal.value.ts_ms, () => loadEquity().catch(() => {}))
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">账户权益 (USDT)</div>
          <div class="value">{{ fmt(bal.total_equity) }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">可用保证金</div>
          <div class="value">{{ fmt(bal.available_margin) }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">当日已实现盈亏</div>
          <div class="value" :class="dayPnl >= 0 ? 'pnl-pos' : 'pnl-neg'">
            {{ dayPnl >= 0 ? '+' : '' }}{{ fmt(dayPnl) }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">当前回撤</div>
          <div class="value" :class="Number(bal.drawdown_pct) > 0 ? 'pnl-neg' : ''">
            {{ fmt(bal.drawdown_pct) }}%
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>权益曲线</template>
          <div ref="equityEl" class="chart-box" style="height:320px"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>运行状态</template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="模式">
              {{ cfg ? cfg.mode : '—' }}
              <el-tag v-if="cfg" :type="cfg.mode === 'mainnet' ? 'danger' : 'success'" size="small" style="margin-left:8px">
                {{ cfg.mode === 'mainnet' ? '主网' : '测试网' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="标的">{{ cfg ? cfg.symbols.join(', ') : '—' }}</el-descriptions-item>
            <el-descriptions-item label="周期">{{ cfg ? cfg.cycle_interval : '—' }}</el-descriptions-item>
            <el-descriptions-item label="当前持仓数">{{ positions.length }}</el-descriptions-item>
            <el-descriptions-item label="最近决策">
              <span v-if="lastDecision">
                {{ lastDecision.symbol }} ·
                {{ decisionLabel(lastDecision.action, lastDecision.skipped) }} ·
                {{ localTime(lastDecision.ts_ms, lastDecision.created_at) }}
              </span>
              <span v-else>—</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
