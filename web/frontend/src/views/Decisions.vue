<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const rows = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref(null)
const ctxPretty = ref('')

async function load() {
  loading.value = true
  try {
    rows.value = await api.decisions(150)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function showDetail(row) {
  try {
    detail.value = await api.decisionDetail(row.id)
    try {
      ctxPretty.value = JSON.stringify(JSON.parse(detail.value.context_json || '{}'), null, 2)
    } catch (_) {
      ctxPretty.value = detail.value.context_json || '(无)'
    }
    detailVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>决策日志（含跳过 LLM 记录）</span>
          <el-button size="small" :loading="loading" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table :data="rows" stripe height="calc(100vh - 220px)">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="symbol" label="标的" width="100" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.skipped" type="info" size="small">跳过</el-tag>
            <el-tag v-else :type="row.action === 'HOLD' ? 'warning' : 'primary'" size="small">
              {{ row.action }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="置信度" width="90">
          <template #default="{ row }">{{ row.skipped ? '—' : row.confidence }}</template>
        </el-table-column>
        <el-table-column label="杠杆" width="70">
          <template #default="{ row }">{{ row.skipped ? '—' : row.leverage + 'x' }}</template>
        </el-table-column>
        <el-table-column label="SL/TP" width="120">
          <template #default="{ row }">
            <span v-if="!row.skipped" class="mono">{{ row.stop_loss_pct }}/{{ row.take_profit_pct }}</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="原因" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.skipped ? row.skip_reason : row.reason }}</template>
        </el-table-column>
        <el-table-column label="" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="showDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="detailVisible" title="决策详情" width="720px">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="时间">{{ detail.created_at }}</el-descriptions-item>
          <el-descriptions-item label="标的">{{ detail.symbol }}</el-descriptions-item>
          <el-descriptions-item label="动作">{{ detail.skipped ? '跳过' : detail.action }}</el-descriptions-item>
          <el-descriptions-item label="置信度">{{ detail.confidence }}</el-descriptions-item>
          <el-descriptions-item label="杠杆">{{ detail.leverage }}x</el-descriptions-item>
          <el-descriptions-item label="参考价">{{ detail.ref_price }}</el-descriptions-item>
          <el-descriptions-item label="SL/TP">{{ detail.stop_loss_pct }} / {{ detail.take_profit_pct }}</el-descriptions-item>
          <el-descriptions-item label="理由" :span="2">{{ detail.skipped ? detail.skip_reason : detail.reason }}</el-descriptions-item>
        </el-descriptions>
        <div style="margin-top:12px; font-size:13px; color:#909399">喂给 LLM 的市场上下文 (context_json)：</div>
        <pre style="max-height:300px; overflow:auto; background:#1f2329; color:#cfd3dc;
                    padding:12px; border-radius:6px; font-size:12px">{{ ctxPretty }}</pre>
      </template>
    </el-dialog>
  </div>
</template>
