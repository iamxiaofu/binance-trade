<script setup>
import { computed } from 'vue'
import { useLiveStore } from '../stores/live'
import { orderStatusLabel } from '../labels'

const live = useLiveStore()
const positions = computed(() => live.positions || [])
const positionsSource = computed(() => live.summary?.positions_source || 'db_snapshot')
const positionsError = computed(() => live.summary?.positions_error || '')
const conditionOrdersError = computed(() => live.summary?.condition_orders_error || '')
const syncedAtText = computed(() => {
  const ts = live.summary?.positions_synced_at_ms
  return ts ? new Date(Number(ts)).toLocaleTimeString() : '—'
})
const isExchangeLive = computed(() => positionsSource.value === 'exchange')
const hasMissingProtection = computed(() => positions.value.some((p) =>
  p.protection?.missing_sl || p.protection?.missing_tp
))

function fmt(n, d = 4) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? v.toFixed(d) : '—'
}

function fmtPct(n) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '—'
}

function margin(row) {
  return Number(row.isolated_margin || row.initial_margin || 0)
}

function protection(row, kind) {
  return kind === 'SL' ? row.protection?.sl : row.protection?.tp
}

function protectionTag(order) {
  if (!order) return 'danger'
  return { placed: 'success', filled: 'primary', canceled: 'danger', expired: 'warning' }[order.status] || 'info'
}

function protectionText(order) {
  if (!order) return '未挂出'
  const price = order.trigger_price || order.price
  return `${orderStatusLabel({ client_kind: order.kind, status: order.status })} @ ${fmt(price, 2)}`
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="positions-header">
          <span>当前持仓（{{ positions.length }}）</span>
          <span class="positions-meta">
            <el-tag :type="isExchangeLive ? 'success' : 'warning'" size="small" effect="dark">
              {{ isExchangeLive ? '交易所实时' : '本地快照' }}
            </el-tag>
            <span>同步于 {{ syncedAtText }}</span>
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
      <el-table :data="positions" stripe empty-text="当前无持仓">
        <el-table-column prop="symbol" label="标的" width="120" />
        <el-table-column label="方向" width="90">
          <template #default="{ row }">
            <el-tag
              :type="row.side === 'long' ? 'success' : row.side === 'short' ? 'danger' : 'info'"
              size="small"
            >
              {{ row.side === 'long' ? '多' : row.side === 'short' ? '空' : '—' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="contracts" label="持仓数量" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.contracts) }}</span></template>
        </el-table-column>
        <el-table-column label="开仓价" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.entry_price, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="标记价" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.mark_price, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="杠杆" width="80">
          <template #default="{ row }">{{ Number(row.leverage) > 0 ? `${row.leverage}x` : '—' }}</template>
        </el-table-column>
        <el-table-column label="名义价值" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.notional, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="保证金" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(margin(row), 2) }}</span></template>
        </el-table-column>
        <el-table-column label="投资回报率" width="120">
          <template #default="{ row }">
            <span class="mono" :class="Number(row.roi_pct) >= 0 ? 'pnl-pos' : 'pnl-neg'">
              {{ fmtPct(row.roi_pct) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="强平价格" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.liquidation_price, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="未实现盈亏" width="120">
          <template #default="{ row }">
            <span class="mono" :class="Number(row.unrealized_pnl) >= 0 ? 'pnl-pos' : 'pnl-neg'">
              {{ fmt(row.unrealized_pnl, 2) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="止损条件单" width="190">
          <template #default="{ row }">
            <el-tag :type="protectionTag(protection(row, 'SL'))" size="small">
              {{ protectionText(protection(row, 'SL')) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="止盈条件单" width="190">
          <template #default="{ row }">
            <el-tag :type="protectionTag(protection(row, 'TP'))" size="small">
              {{ protectionText(protection(row, 'TP')) }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
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
</style>
