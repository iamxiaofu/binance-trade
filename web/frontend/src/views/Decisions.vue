<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'
import { decisionLabel, decisionTagType, llmLatencyTag, localTime } from '../labels'
import { DEFAULT_TIME_RANGE, QUICK_TIME_RANGES } from '../timeRanges'

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const DECISION_SEARCH_DEBOUNCE_MS = 200
// 实时刷新：开启后每 3s 静默重拉一次，不重置滚动位置；只在第一页时启用，避免翻页时被强制顶回第一页。
const liveRefresh = ref(false)
let liveTimer = null
let searchTimer = null
let listAbortController = null
let listRequestSeq = 0
function startLiveRefresh() {
  stopLiveRefresh()
  liveTimer = setInterval(() => {
    if (!liveRefresh.value) return
    if (page.value.offset !== 0) return
    if (loading.value || listAbortController) return
    // 静默拉取：直接覆盖 rows/total，不翻转 loading（避免表格 v-loading 闪烁）
    load({ silent: true }).catch(() => {})
  }, 3000)
}
function stopLiveRefresh() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null }
}
function clearSearchTimer() {
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null }
}
function abortListRequest() {
  if (listAbortController) {
    listAbortController.abort()
    listAbortController = null
  }
}
function isAbortError(e) {
  return e?.name === 'AbortError'
}
function toggleLiveRefresh() {
  liveRefresh.value = !liveRefresh.value
  if (liveRefresh.value) startLiveRefresh()
  else stopLiveRefresh()
}
const detailVisible = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
const ctxPretty = ref('')
const llmPromptPretty = ref('')
const llmRequestPretty = ref('')
const llmResponsePretty = ref('')
const featureSnapshotPretty = ref('')
const llmDataRows = ref([])
const cfg = ref(null)
const filters = ref({
  symbols: [],
  types: [],
  range: [],
  quickRange: DEFAULT_TIME_RANGE,
  // 默认隐藏两类无信息量的跳过日志：停用币种、no significant change。
  hideSymbolDisabled: true,
  hideNoSignificantChange: true,
})
const page = ref({
  limit: 25,
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

function quickRangeBounds(value) {
  if (!value) return null
  const map = {
    '1h': 60 * 60_000,
    '3h': 3 * 60 * 60_000,
    '12h': 12 * 60 * 60_000,
    '1d': 24 * 60 * 60_000,
    '7d': 7 * 24 * 60 * 60_000,
    '30d': 30 * 24 * 60 * 60_000,
  }
  const span = map[value]
  if (!span) return null
  const end = Date.now()
  return { start: end - span, end }
}

function activeTimeRange() {
  if (filters.value.quickRange) return filters.value.quickRange
  const [start, end] = filters.value.range || []
  if (start instanceof Date && end instanceof Date) return ''
  return ''
}

function queryParams() {
  const params = {
    symbol: filters.value.symbols,
    type: filters.value.types,
    hide_symbol_disabled: filters.value.hideSymbolDisabled ? 'true' : undefined,
    hide_no_significant_change: filters.value.hideNoSignificantChange ? 'true' : undefined,
    limit: page.value.limit,
    offset: page.value.offset,
  }
  if (filters.value.quickRange) {
    const bounds = quickRangeBounds(filters.value.quickRange)
    if (bounds) {
      params.start_ts_ms = bounds.start
      params.end_ts_ms = bounds.end
    }
  } else {
    const [start, end] = filters.value.range || []
    if (start instanceof Date) params.start_ts_ms = start.getTime()
    if (end instanceof Date) params.end_ts_ms = end.getTime()
  }
  return params
}

function onQuickRangeChange(value) {
  filters.value.quickRange = value || ''
  if (value) filters.value.range = []
  search()
}

function onManualRangeChange(value) {
  if (Array.isArray(value) && value.length === 2
      && value[0] instanceof Date && value[1] instanceof Date) {
    filters.value.quickRange = ''
  }
  search()
}

function clearTimeRange() {
  filters.value.quickRange = ''
  filters.value.range = []
  search()
}

async function load(options = {}) {
  const silent = Boolean(options.silent)
  clearSearchTimer()
  abortListRequest()
  const controller = new AbortController()
  listAbortController = controller
  const seq = ++listRequestSeq
  if (!silent) loading.value = true
  try {
    const res = await api.decisions(queryParams(), { signal: controller.signal })
    if (seq !== listRequestSeq) return
    rows.value = res.items || []
    total.value = Number(res.total || 0)
  } catch (e) {
    if (!isAbortError(e) && seq === listRequestSeq && !silent) {
      ElMessage.error(e.message)
    }
  } finally {
    if (seq === listRequestSeq) {
      if (listAbortController === controller) listAbortController = null
      if (!silent) loading.value = false
    }
  }
}

function priceText(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n === 0) return '—'
  return String(value)
}

function protectionOrderText(order, expected) {
  if (!expected) return '未要求'
  if (!order) return '缺失'
  const status = order.status ? ` ${order.status}` : ''
  return `${priceText(order.price)}${status}`
}

function actualProtectionText(protection) {
  if (!protection || protection.status === 'not_applicable') return '—'
  if (!protection.entry) return protection.message || '未找到成交 OPEN'
  const expected = protection.expected || {}
  return [
    `入场 ${priceText(protection.entry.price)}${protection.entry.status ? ` ${protection.entry.status}` : ''}`,
    `SL ${protectionOrderText(protection.sl, expected.sl)}`,
    `TP ${protectionOrderText(protection.tp, expected.tp)}`,
  ].join(' | ')
}

function actualProtectionStatusLabel(protection) {
  const status = protection?.status || 'not_applicable'
  const map = {
    complete: '已匹配',
    missing: '有缺失',
    no_entry: '未成交',
    not_applicable: '不适用',
  }
  return map[status] || status
}

function actualProtectionTagType(protection) {
  const status = protection?.status || 'not_applicable'
  if (status === 'complete') return 'success'
  if (status === 'missing') return 'danger'
  if (status === 'no_entry') return 'warning'
  return 'info'
}

function hydrateDetailView() {
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
    featureSnapshotPretty.value = JSON.stringify(
      JSON.parse(detail.value.feature_snapshot_json || '{}'),
      null,
      2,
    )
  } catch (_) {
    featureSnapshotPretty.value = detail.value.feature_snapshot_json || '(无)'
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
}

async function loadDecisionDetail(id, openDialog = false) {
  detailLoading.value = true
  try {
    detail.value = await api.decisionDetail(id)
    hydrateDetailView()
    if (openDialog) detailVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    detailLoading.value = false
  }
}

async function showDetail(row) {
  await loadDecisionDetail(row.id, true)
}

async function refreshDetail() {
  if (!detail.value?.id) return
  await loadDecisionDetail(detail.value.id, false)
}

function search(options = {}) {
  page.value.offset = 0
  clearSearchTimer()
  const immediate = Boolean(options.immediate)
  if (immediate) {
    load()
    return
  }
  searchTimer = setTimeout(() => {
    searchTimer = null
    load()
  }, DECISION_SEARCH_DEBOUNCE_MS)
}

function resetFilters() {
  filters.value = {
    symbols: [],
    types: [],
    range: [],
    quickRange: DEFAULT_TIME_RANGE,
    // 重置也保留默认隐藏两类无信息量日志，避免重置后看到大量 skip。
    hideSymbolDisabled: true,
    hideNoSignificantChange: true,
  }
  search()
}

function toggleHideSymbolDisabled() {
  filters.value.hideSymbolDisabled = !filters.value.hideSymbolDisabled
  search()
}

function toggleHideNoSignificantChange() {
  filters.value.hideNoSignificantChange = !filters.value.hideNoSignificantChange
  search()
}

function handlePageChange(nextPage) {
  page.value.offset = (nextPage - 1) * page.value.limit
  clearSearchTimer()
  load()
}

function handleSizeChange(size) {
  page.value.limit = size
  page.value.offset = 0
  clearSearchTimer()
  load()
}

const currentPage = computed(() => Math.floor(page.value.offset / page.value.limit) + 1)

onMounted(async () => {
  cfg.value = await api.config().catch(() => null)
  await load()
})
onUnmounted(() => {
  stopLiveRefresh()
  clearSearchTimer()
  abortListRequest()
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>决策日志（含跳过 LLM 记录）</span>
          <el-button size="small" :loading="loading" :icon="'Refresh'" @click="load">刷新</el-button>
          <el-button
            size="small"
            :type="liveRefresh ? 'success' : 'default'"
            :plain="!liveRefresh"
            :icon="liveRefresh ? 'VideoPlay' : 'VideoPause'"
            :title="liveRefresh ? '点击停止实时刷新' : '点击开启：每 3 秒自动刷新（仅首页）'"
            style="margin-left:6px"
            @click="toggleLiveRefresh"
          >{{ liveRefresh ? '实时刷新中' : '实时刷新' }}</el-button>
        </div>
      </template>
      <div class="filter-bar">
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
          :disabled="!!filters.quickRange"
          @change="onManualRangeChange"
        />
        <el-radio-group
          v-model="filters.quickRange"
          size="small"
          class="quick-range-group"
          @change="onQuickRangeChange"
        >
          <el-radio-button
            v-for="item in QUICK_TIME_RANGES"
            :key="item.value"
            :value="item.value"
          >
            {{ item.label }}
          </el-radio-button>
        </el-radio-group>
        <el-button
          size="small"
          :icon="'RefreshLeft'"
          :disabled="!filters.quickRange && !(filters.range && filters.range.length === 2)"
          @click="clearTimeRange"
        >
          清除时间
        </el-button>
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
      </div>
      <div class="filter-bar">
        <el-button
          :type="filters.hideSymbolDisabled ? 'primary' : 'default'"
          :plain="!filters.hideSymbolDisabled"
          :icon="filters.hideSymbolDisabled ? 'Hide' : 'View'"
          @click="toggleHideSymbolDisabled"
        >
          忽略停用币种日志
        </el-button>
        <el-button
          :type="filters.hideNoSignificantChange ? 'primary' : 'default'"
          :plain="!filters.hideNoSignificantChange"
          :icon="filters.hideNoSignificantChange ? 'Hide' : 'View'"
          @click="toggleHideNoSignificantChange"
        >
          忽略 no significant change 日志
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
        <el-table-column label="LLM耗时" width="120">
          <template #default="{ row }">
            <el-tag v-if="!row.skipped" :type="llmLatencyTag(row).type" size="small">
              {{ llmLatencyTag(row).label }}
            </el-tag>
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
          :page-sizes="[25, 50, 100]"
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>

    <el-dialog v-model="detailVisible" width="90vw">
      <template #header>
        <div class="dialog-header">
          <span>决策详情</span>
          <el-button
            size="small"
            :icon="'Refresh'"
            :loading="detailLoading"
            :disabled="!detail?.id"
            @click="refreshDetail"
          >
            刷新保护价
          </el-button>
        </div>
      </template>
      <template v-if="detail">
        <div class="detail-body" v-loading="detailLoading">
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
          <el-descriptions-item label="成交后保护价" :span="2">
            <el-tag
              size="small"
              :type="actualProtectionTagType(detail.actual_protection)"
            >
              {{ actualProtectionStatusLabel(detail.actual_protection) }}
            </el-tag>
            <span class="actual-protection mono">
              {{ actualProtectionText(detail.actual_protection) }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="LLM耗时">
            <el-tag v-if="!detail.skipped" :type="llmLatencyTag(detail).type" size="small">
              {{ llmLatencyTag(detail).label }}
            </el-tag>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item label="LLM状态">
            <template v-if="detail.skipped">—</template>
            <template v-else-if="!detail.llm_status_available">未采集</template>
            <template v-else>
              {{ detail.llm_status || 'ok' }} · 尝试 {{ detail.llm_attempts || 1 }} 次
            </template>
          </el-descriptions-item>
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
          <el-tab-pane label="Feature Snapshot">
            <pre class="detail-pre">{{ featureSnapshotPretty }}</pre>
          </el-tab-pane>
          <el-tab-pane label="Context JSON">
            <pre class="detail-pre">{{ ctxPretty }}</pre>
          </el-tab-pane>
        </el-tabs>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}
.filter-bar:last-of-type {
  margin-bottom: 12px;
}
.quick-range-group {
  margin-left: 4px;
}
.trace-alert {
  margin-top: 12px;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 28px;
}

.actual-protection {
  margin-left: 8px;
  word-break: break-word;
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
