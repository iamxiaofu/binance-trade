<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import * as echarts from 'echarts'
import { useLiveStore } from '../stores/live'
import { api } from '../api'
import { decisionLabel, localTime, utc8AxisTime } from '../labels'
import { DEFAULT_TIME_RANGE, QUICK_TIME_RANGES } from '../timeRanges'

const live = useLiveStore()
const cfg = ref(null)
const equityEl = ref(null)
const equityRange = ref(DEFAULT_TIME_RANGE)
let chart = null

const bal = computed(() => live.balance || {})
const positions = computed(() => live.positions || [])
const dayEquityChange = computed(() => Number(bal.value.day_equity_change || 0))
const lastDecision = computed(() => (live.summary.recent_decisions || [])[0] || null)

function fmt(n, d = 2) {
  if (n === null || n === undefined || n === '') return '—'
  return Number(n).toFixed(d)
}

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

async function loadEquity() {
  const data = await api.equity({ range: equityRange.value, limit: 800 })
  if (!chart) return
  const textColor = cssVar('--bt-text', '#303133')
  const mutedColor = cssVar('--bt-muted', '#909399')
  const gridColor = cssVar('--bt-border', '#e5e7eb')
  const lineColor = cssVar('--bt-primary', '#409eff')
  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: cssVar('--bt-card', '#ffffff'), textStyle: { color: textColor } },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: data.map(d => utc8AxisTime(d.ts_ms, d.created_at)),
      axisLabel: { show: false, color: mutedColor },
      axisLine: { lineStyle: { color: gridColor } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: mutedColor },
      splitLine: { lineStyle: { color: gridColor } },
    },
    series: [{
      name: '权益', type: 'line', smooth: true, showSymbol: false,
      data: data.map(d => d.total_equity), areaStyle: { opacity: 0.1 },
      lineStyle: { color: lineColor }, itemStyle: { color: lineColor },
    }],
  })
}

function onRangeChange() {
  loadEquity().catch(() => {})
}

function onThemeChange() {
  loadEquity().catch(() => {})
}

onMounted(async () => {
  cfg.value = await api.config().catch(() => null)
  chart = echarts.init(equityEl.value)
  await loadEquity().catch(() => {})
  window.addEventListener('resize', resize)
  window.addEventListener('binance-trade-theme-change', onThemeChange)
})
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  window.removeEventListener('binance-trade-theme-change', onThemeChange)
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
          <div class="label">当日权益变化</div>
          <div class="value" :class="dayEquityChange > 0 ? 'pnl-pos' : dayEquityChange < 0 ? 'pnl-neg' : ''">
            {{ dayEquityChange > 0 ? '+' : '' }}{{ fmt(dayEquityChange) }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <el-tooltip
            content="当前自然日权益相对当日最高权益的回撤；风控参数“回撤熔断”使用此指标。人工确认恢复后可豁免至当日结束。"
            placement="top"
          >
            <div class="label">当日风控回撤（熔断依据）</div>
          </el-tooltip>
          <div class="value" :class="Number(bal.risk_day_drawdown_pct) > 0 ? 'pnl-neg' : ''">
            {{ fmt(bal.risk_day_drawdown_pct) }}%
          </div>
          <div class="metric-note">
            当日峰值 {{ fmt(bal.risk_day_equity_peak) }}
            <el-tag v-if="bal.drawdown_bypass_active" type="warning" size="small">今日已人工豁免</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <el-tooltip content="账户权益相对历史最高权益的回撤，仅用于绩效展示和审计，不再直接触发回撤熔断。" placement="top">
            <div class="label">账户历史回撤</div>
          </el-tooltip>
          <div class="value" :class="Number(bal.account_drawdown_pct ?? bal.drawdown_pct) > 0 ? 'pnl-neg' : ''">
            {{ fmt(bal.account_drawdown_pct ?? bal.drawdown_pct) }}%
          </div>
          <div class="metric-note">历史峰值 {{ fmt(bal.account_equity_peak) }} USDT</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <div class="label">当前持仓未实现盈亏</div>
          <div
            class="value"
            :class="Number(bal.position_unrealized_pnl) > 0
              ? 'pnl-pos'
              : Number(bal.position_unrealized_pnl) < 0 ? 'pnl-neg' : ''"
          >
            {{ Number(bal.position_unrealized_pnl || 0) > 0 ? '+' : '' }}{{ fmt(bal.position_unrealized_pnl) }}
          </div>
          <div class="metric-note">无持仓时为 0</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <el-tooltip content="仅统计当前持仓负的未实现盈亏，占当前账户权益的比例；不参与回撤熔断。" placement="top">
            <div class="label">当前持仓浮亏占权益</div>
          </el-tooltip>
          <div class="value" :class="Number(bal.position_floating_loss_pct_equity) > 0 ? 'pnl-neg' : ''">
            {{ fmt(bal.position_floating_loss_pct_equity) }}%
          </div>
          <div class="metric-note">浮亏 {{ fmt(bal.position_floating_loss) }} USDT</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="metric-card" shadow="never">
          <el-tooltip content="不可用保证金扣除持仓初始保证金后的估算值；交易所未提供逐挂单精确占用。" placement="top">
            <div class="label">挂单预留保证金（估算）</div>
          </el-tooltip>
          <div class="value">{{ fmt(bal.open_order_reserved_margin_estimate) }}</div>
          <div class="metric-note">
            不可用 {{ fmt(bal.unavailable_margin) }} · 外部挂单 {{ bal.external_open_order_count || 0 }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header-row">
              <span>权益曲线</span>
              <el-radio-group v-model="equityRange" size="small" @change="onRangeChange">
                <el-radio-button
                  v-for="item in QUICK_TIME_RANGES"
                  :key="item.value"
                  :value="item.value"
                >
                  {{ item.label }}
                </el-radio-button>
              </el-radio-group>
            </div>
          </template>
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
            <el-descriptions-item label="分析周期">
              {{
                cfg
                  ? (cfg.cycle_interval_seconds || cfg.engine?.cycle_interval_seconds
                    ? `${cfg.cycle_interval_seconds || cfg.engine?.cycle_interval_seconds}s`
                    : cfg.cycle_interval)
                  : '—'
              }}
            </el-descriptions-item>
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

<style scoped>
.metric-note {
  color: var(--bt-muted);
  font-size: 12px;
  margin-top: 6px;
}
</style>
