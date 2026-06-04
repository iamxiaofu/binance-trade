<script setup>
import { computed } from 'vue'
import { useLiveStore } from '../stores/live'

const live = useLiveStore()
const positions = computed(() => live.positions || [])

function fmt(n, d = 4) {
  if (n === null || n === undefined || n === '') return '—'
  return Number(n).toFixed(d)
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>当前持仓（{{ positions.length }}）</template>
      <el-table :data="positions" stripe empty-text="当前无持仓">
        <el-table-column prop="symbol" label="标的" width="120" />
        <el-table-column label="方向" width="90">
          <template #default="{ row }">
            <el-tag :type="row.side === 'long' ? 'success' : 'danger'" size="small">
              {{ row.side === 'long' ? '多' : '空' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="contracts" label="数量" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.contracts) }}</span></template>
        </el-table-column>
        <el-table-column label="开仓价" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.entry_price, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="标记价" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.mark_price, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="杠杆" width="80">
          <template #default="{ row }">{{ row.leverage }}x</template>
        </el-table-column>
        <el-table-column label="名义价值" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.notional, 2) }}</span></template>
        </el-table-column>
        <el-table-column label="未实现盈亏">
          <template #default="{ row }">
            <span class="mono" :class="Number(row.unrealized_pnl) >= 0 ? 'pnl-pos' : 'pnl-neg'">
              {{ fmt(row.unrealized_pnl, 2) }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
