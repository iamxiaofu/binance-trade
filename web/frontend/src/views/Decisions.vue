<script setup>
import { computed, ref, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'
import { decisionLabel, decisionTagType, localTime } from '../labels'

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref(null)
const ctxPretty = ref('')
const llmPromptPretty = ref('')
const llmRequestPretty = ref('')
const llmResponsePretty = ref('')
const llmDataRows = ref([])
const cfg = ref(null)
const filters = ref({
  symbols: [],
  types: [],
  range: [],
  hideSymbolDisabled: false,
})
const page = ref({
  limit: 100,
  offset: 0,
})

const decisionTypeOptions = [
  { label: '跳过 LLM', value: 'SKIPPED' },
  { label: '建议开多', value: 'OPEN_LONG' },
  { label: '建议开空', value: 'OPEN_SHORT' },
  { label: '建议平仓', value: 'CLOSE' },
  { label: '继续观望', value: 'HOLD' },
]

const symbolOptions = computed(() => cfg.value?.symbols || [])

function queryParams() {
  const [start, end] = filters.value.range || []
  return {
    symbol: filters.value.symbols,
    type: filters.value.types,
    start_ts_ms: start instanceof Date ? start.getTime() : undefined,
    end_ts_ms: end instanceof Date ? end.getTime() : undefined,
    hide_symbol_disabled: filters.value.hideSymbolDisabled ? 'true' : undefined,
    limit: page.value.limit,
    offset: page.value.offset,
  }
}

async function load() {
  loading.value = true
  try {
    const res = await api.decisions(queryParams())
    rows.value = res.items || []
    total.value = Number(res.total || 0)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function showDetail(row) {
  try {
    detail.value = await api.decisionDetail(row.id)
    llmDataRows.value = detail.value.llm_data_items || []
    llmPromptPretty.value = [
      '【System Prompt】',
      detail.value.llm_system_prompt || '(无)',
      '',
      '【User Prompt + 数据】',
      detail.value.llm_user_prompt || '(无)',
    ].join('\n')
    try {
      ctxPretty.value = JSON.stringify(JSON.parse(detail.value.context_json || '{}'), null, 2)
    } catch (_) {
      ctxPretty.value = detail.value.context_json || '(无)'
    }
    try {
      llmRequestPretty.value = JSON.stringify(
        JSON.parse(detail.value.llm_request_effective_json || '{}'),
        null,
        2,
      )
    } catch (_) {
      llmRequestPretty.value = detail.value.llm_request_effective_json || '(无)'
    }
    try {
      llmResponsePretty.value = JSON.stringify(
        JSON.parse(detail.value.llm_response_effective_json || '{}'),
        null,
        2,
      )
    } catch (_) {
      llmResponsePretty.value = detail.value.llm_response_effective_json || '(无)'
    }
    detailVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function search() {
  page.value.offset = 0
  load()
}

function resetFilters() {
  filters.value = { symbols: [], types: [], range: [], hideSymbolDisabled: false }
  search()
}

function toggleHideSymbolDisabled() {
  filters.value.hideSymbolDisabled = !filters.value.hideSymbolDisabled
  search()
}

function handlePageChange(nextPage) {
  page.value.offset = (nextPage - 1) * page.value.limit
  load()
}

function handleSizeChange(size) {
  page.value.limit = size
  page.value.offset = 0
  load()
}

const currentPage = computed(() => Math.floor(page.value.offset / page.value.limit) + 1)

onMounted(async () => {
  cfg.value = await api.config().catch(() => null)
  await load()
})
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
      <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px">
        <el-select
          v-model="filters.symbols"
          multiple
          clearable
          collapse-tags
          collapse-tags-tooltip
          placeholder="币种"
          style="width:220px"
          @change="search"
        >
          <el-option v-for="symbol in symbolOptions" :key="symbol" :label="symbol" :value="symbol" />
        </el-select>
        <el-date-picker
          v-model="filters.range"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          style="width:360px"
          @change="search"
        />
        <el-select
          v-model="filters.types"
          multiple
          clearable
          collapse-tags
          collapse-tags-tooltip
          placeholder="类型"
          style="width:240px"
          @change="search"
        >
          <el-option v-for="item in decisionTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-button
          :type="filters.hideSymbolDisabled ? 'primary' : 'default'"
          :plain="!filters.hideSymbolDisabled"
          :icon="filters.hideSymbolDisabled ? 'Hide' : 'View'"
          @click="toggleHideSymbolDisabled"
        >
          忽略停用币种日志
        </el-button>
        <el-button :icon="'RefreshLeft'" @click="resetFilters">重置</el-button>
      </div>

      <el-table :data="rows" stripe height="calc(100vh - 300px)" v-loading="loading">
        <el-table-column label="本地时间" width="180">
          <template #default="{ row }">{{ localTime(row.ts_ms, row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="symbol" label="币种" width="100" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="decisionTagType(row.action, row.skipped)" size="small">
              {{ decisionLabel(row.action, row.skipped) }}
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
      <div style="display:flex; justify-content:flex-end; margin-top:12px">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next"
          :total="total"
          :current-page="currentPage"
          :page-size="page.limit"
          :page-sizes="[50, 100, 200, 500]"
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>

    <el-dialog v-model="detailVisible" title="决策详情" width="90vw">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="本地时间">{{ localTime(detail.ts_ms, detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="币种">{{ detail.symbol }}</el-descriptions-item>
          <el-descriptions-item label="动作">
            {{ decisionLabel(detail.action, detail.skipped) }}
          </el-descriptions-item>
          <el-descriptions-item label="置信度">{{ detail.confidence }}</el-descriptions-item>
          <el-descriptions-item label="杠杆">{{ detail.leverage }}x</el-descriptions-item>
          <el-descriptions-item label="参考价">{{ detail.ref_price }}</el-descriptions-item>
          <el-descriptions-item label="SL/TP">{{ detail.stop_loss_pct }} / {{ detail.take_profit_pct }}</el-descriptions-item>
          <el-descriptions-item label="理由" :span="2">{{ detail.skipped ? detail.skip_reason : detail.reason }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="!detail.skipped && !detail.llm_trace_available"
          class="trace-alert"
          type="warning"
          show-icon
          :closable="false"
          title="该历史记录没有保存原始 LLM request/response，页面已基于 context_json 重建 Prompt；新决策会保存完整调用记录。"
        />

        <el-tabs class="decision-detail-tabs">
          <el-tab-pane label="LLM数据列表">
            <el-table :data="llmDataRows" border height="360px" size="small">
              <el-table-column prop="category" label="分类" width="110" />
              <el-table-column prop="field" label="字段" width="220" show-overflow-tooltip />
              <el-table-column prop="value" label="发送值" min-width="320" show-overflow-tooltip />
              <el-table-column prop="note" label="说明" min-width="220" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="Prompt">
            <pre class="detail-pre">{{ llmPromptPretty }}</pre>
          </el-tab-pane>
          <el-tab-pane label="完整请求JSON">
            <pre class="detail-pre">{{ llmRequestPretty }}</pre>
          </el-tab-pane>
          <el-tab-pane label="LLM回传结果">
            <pre class="detail-pre">{{ llmResponsePretty }}</pre>
          </el-tab-pane>
          <el-tab-pane label="Context JSON">
            <pre class="detail-pre">{{ ctxPretty }}</pre>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.trace-alert {
  margin-top: 12px;
}

.decision-detail-tabs {
  margin-top: 12px;
}

.detail-pre {
  max-height: 460px;
  overflow: auto;
  background: #1f2329;
  color: #cfd3dc;
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
