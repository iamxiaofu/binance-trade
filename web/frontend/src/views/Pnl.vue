<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'

const stats = ref(null)
const barEl = ref(null)
let chart = null

async function load() {
  stats.value = await api.pnl().catch(() => null)
  if (chart && stats.value) {
    const bySym = stats.value.close_by_symbol || {}
    chart.setOption({
      tooltip: {},
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      xAxis: { type: 'category', data: Object.keys(bySym) },
      yAxis: { type: 'value' },
      series: [{ name: '平仓笔数', type: 'bar', data: Object.values(bySym),
                 itemStyle: { color: '#409eff' } }],
    })
  }
}

onMounted(async () => {
  chart = echarts.init(barEl.value)
  await load()
  window.addEventListener('resize', resize)
})
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  if (chart) chart.dispose()
})
function resize() { if (chart) chart.resize() }
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card class="metric-card" shadow="never">
          <div class="label">当日已实现盈亏 (USDT)</div>
          <div class="value" :class="Number(stats?.day_realized_pnl) >= 0 ? 'pnl-pos' : 'pnl-neg'">
            {{ stats ? Number(stats.day_realized_pnl).toFixed(2) : '—' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card class="metric-card" shadow="never">
          <div class="label">累计平仓笔数</div>
          <div class="value">{{ stats ? stats.close_count : '—' }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card class="metric-card" shadow="never">
          <div class="label">交易标的数</div>
          <div class="value">{{ stats ? Object.keys(stats.close_by_symbol || {}).length : '—' }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>各标的平仓笔数</span>
          <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
      </template>
      <div ref="barEl" style="height:320px"></div>
      <div style="color:#909399; font-size:12px; margin-top:8px">
        说明：已实现盈亏为运行态累计（入场价 vs 标记价近似，未计手续费/资金费），用于驱动日亏熔断；
        精确对账以交易所流水为准。
      </div>
    </el-card>
  </div>
</template>
