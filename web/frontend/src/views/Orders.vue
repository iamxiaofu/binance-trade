<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const tab = ref('orders')
const orders = ref([])
const rejects = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const [o, r] = await Promise.all([api.orders(150), api.rejects(150)])
    orders.value = o
    rejects.value = r
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function kindTag(k) {
  return { OPEN: 'success', CLOSE: 'primary', SL: 'danger', TP: 'warning' }[k] || 'info'
}
function statusTag(s) {
  return { filled: 'success', partial: 'warning', dry_run: 'info', rejected: 'danger', error: 'danger' }[s] || 'info'
}

onMounted(load)
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <el-radio-group v-model="tab">
            <el-radio-button value="orders">订单流水（{{ orders.length }}）</el-radio-button>
            <el-radio-button value="rejects">风控拒单（{{ rejects.length }}）</el-radio-button>
          </el-radio-group>
          <el-button size="small" :loading="loading" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
      </template>

      <el-table v-if="tab === 'orders'" :data="orders" stripe height="calc(100vh - 240px)">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="symbol" label="标的" width="100" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }"><el-tag :type="kindTag(row.client_kind)" size="small">{{ row.client_kind }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="side" label="方向" width="70" />
        <el-table-column label="数量" width="120">
          <template #default="{ row }"><span class="mono">{{ row.qty }}</span></template>
        </el-table-column>
        <el-table-column label="价格" width="110">
          <template #default="{ row }"><span class="mono">{{ row.price }}</span></template>
        </el-table-column>
        <el-table-column label="名义价值" width="110">
          <template #default="{ row }"><span class="mono">{{ Number(row.notional).toFixed(2) }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="statusTag(row.status)" size="small">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="DRY" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.dry_run" type="info" size="small">dry</el-tag>
            <el-tag v-else type="danger" size="small">真实</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <el-table v-else :data="rejects" stripe height="calc(100vh - 240px)">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="symbol" label="标的" width="100" />
        <el-table-column label="拒单码" width="180">
          <template #default="{ row }"><el-tag type="danger" size="small">{{ row.code }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="action" label="动作" width="100" />
        <el-table-column label="杠杆" width="70">
          <template #default="{ row }">{{ row.leverage }}x</template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="260" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>
