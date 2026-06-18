<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'
import {
  localTime,
  utc8InputToMs,
  decisionLabel,
  executionModeLabel,
  liquidityLabel,
  orderActionLabel,
  orderKindTag,
  orderStatusLabel,
  orderStatusTag,
  orderTypeLabel,
  rejectCodeLabel,
  sideLabel,
  tradeExitReasonLabel,
  tradeDirectionLabel,
  tradeDirectionTag,
  tradeStatusLabel,
  tradeStatusTag,
} from '../labels'

const tab = ref('trades')
const trades = ref([])
const tradeTotal = ref(0)
const fillAudit = ref({ external: 0, mixed: 0, unknown: 0 })
const orders = ref([])
const rejects = ref([])
const rawLoaded = ref(false)
const cfg = ref(null)
const tradeLoading = ref(false)
const rawLoading = ref(false)
const loading = computed(() => tab.value === 'trades' ? tradeLoading.value : rawLoading.value)
const TRADE_SEARCH_DEBOUNCE_MS = 200
const RAW_LIST_LIMIT = 100
let tradeSearchTimer = null
let tradeAbortController = null
let tradeRequestSeq = 0
let rawAbortController = null
let rawRequestSeq = 0
const filters = ref({
  symbols: [],
  directions: [],
  statuses: [],
  exitReasons: [],
  sources: [],
  range: [],
})
const page = ref({
  limit: 25,
  offset: 0,
})

const directionOptions = [
  { label: '多单', value: 'long' },
  { label: '空单', value: 'short' },
]
const statusOptions = [
  { label: '持仓中', value: 'open' },
  { label: '已平仓', value: 'closed' },
  { label: '部分平仓', value: 'partial' },
  { label: '未匹配', value: 'unmatched' },
]
const exitReasonOptions = [
  { label: '策略平仓', value: 'CLOSE' },
  { label: '止盈成交', value: 'TP' },
  { label: '止损/移动止损', value: 'SL' },
  { label: '保护平仓', value: 'EMERGENCY' },
  { label: '熔断平仓', value: 'CIRCUIT' },
  { label: '未知退出', value: 'UNKNOWN' },
  { label: '外部平仓', value: 'EXTERNAL' },
]
const sourceOptions = [
  { label: 'Engine 策略交易', value: 'strategy' },
  { label: 'Binance 外部/手工交易', value: 'external' },
]
const symbolOptions = computed(() => cfg.value?.symbols || [])
const currentPage = computed(() => Math.floor(page.value.offset / page.value.limit) + 1)

function clearTradeSearchTimer() {
  if (tradeSearchTimer) {
    clearTimeout(tradeSearchTimer)
    tradeSearchTimer = null
  }
}

function abortTradeRequest() {
  if (tradeAbortController) {
    tradeAbortController.abort()
    tradeAbortController = null
  }
}

function abortRawRequest() {
  if (rawAbortController) {
    rawAbortController.abort()
    rawAbortController = null
  }
}

function isAbortError(e) {
  return e?.name === 'AbortError'
}

function queryParams() {
  const [start, end] = filters.value.range || []
  return {
    symbol: filters.value.symbols,
    direction: filters.value.directions,
    status: filters.value.statuses,
    exit_reason: filters.value.exitReasons,
    source: filters.value.sources,
    start_ts_ms: utc8InputToMs(start),
    end_ts_ms: utc8InputToMs(end),
    limit: page.value.limit,
    offset: page.value.offset,
  }
}

function fmt(value, digits = 2) {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}

function pnlClass(value) {
  return Number(value || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'
}

async function loadTrades(options = {}) {
  const silent = Boolean(options.silent)
  clearTradeSearchTimer()
  abortTradeRequest()
  const controller = new AbortController()
  tradeAbortController = controller
  const seq = ++tradeRequestSeq
  if (!silent) tradeLoading.value = true
  try {
    const res = await api.trades(queryParams(), { signal: controller.signal })
    if (seq !== tradeRequestSeq) return
    trades.value = res.items || []
    tradeTotal.value = Number(res.total || 0)
    fillAudit.value = res.fill_audit || { external: 0, mixed: 0, unknown: 0 }
  } catch (e) {
    if (!isAbortError(e) && seq === tradeRequestSeq && !silent) {
      ElMessage.error(e.message)
    }
  } finally {
    if (seq === tradeRequestSeq) {
      if (tradeAbortController === controller) tradeAbortController = null
      if (!silent) tradeLoading.value = false
    }
  }
}

async function loadRaw(options = {}) {
  const silent = Boolean(options.silent)
  abortRawRequest()
  const controller = new AbortController()
  rawAbortController = controller
  const seq = ++rawRequestSeq
  if (!silent) rawLoading.value = true
  try {
    const [o, r] = await Promise.all([
      api.orders(RAW_LIST_LIMIT, { signal: controller.signal }),
      api.rejects(RAW_LIST_LIMIT, { signal: controller.signal }),
    ])
    if (seq !== rawRequestSeq) return
    orders.value = o
    rejects.value = r
    rawLoaded.value = true
  } catch (e) {
    if (!isAbortError(e) && seq === rawRequestSeq && !silent) {
      ElMessage.error(e.message)
    }
  } finally {
    if (seq === rawRequestSeq) {
      if (rawAbortController === controller) rawAbortController = null
      if (!silent) rawLoading.value = false
    }
  }
}

async function load() {
  if (tab.value === 'trades') {
    abortRawRequest()
    await loadTrades()
    return
  }
  clearTradeSearchTimer()
  abortTradeRequest()
  await loadRaw()
}

function search() {
  page.value.offset = 0
  clearTradeSearchTimer()
  tradeSearchTimer = setTimeout(() => {
    tradeSearchTimer = null
    loadTrades()
  }, TRADE_SEARCH_DEBOUNCE_MS)
}

function resetFilters() {
  filters.value = {
    symbols: [], directions: [], statuses: [], exitReasons: [], sources: [], range: [],
  }
  search()
}

function handlePageChange(nextPage) {
  page.value.offset = (nextPage - 1) * page.value.limit
  clearTradeSearchTimer()
  loadTrades()
}

function handleSizeChange(size) {
  page.value.limit = size
  page.value.offset = 0
  clearTradeSearchTimer()
  loadTrades()
}

onMounted(async () => {
  cfg.value = await api.config().catch(() => null)
  await loadTrades()
})

onUnmounted(() => {
  clearTradeSearchTimer()
  abortTradeRequest()
  abortRawRequest()
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <el-radio-group v-model="tab" @change="load">
            <el-radio-button value="trades">交易汇总（{{ tradeTotal }}）</el-radio-button>
            <el-radio-button value="orders">订单流水{{ rawLoaded ? `（${orders.length}）` : '' }}</el-radio-button>
            <el-radio-button value="rejects">风控拒单{{ rawLoaded ? `（${rejects.length}）` : '' }}</el-radio-button>
          </el-radio-group>
          <el-button size="small" :loading="loading" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
      </template>

      <template v-if="tab === 'trades'">
        <el-alert
          v-if="fillAudit.mixed || fillAudit.unknown"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom:12px"
          :title="`检测到 ${fillAudit.mixed || 0} 笔混合归属成交、${fillAudit.unknown || 0} 笔待确认成交；这些成交仅保留审计账本，不会并入策略或外部交易汇总。`"
        />
        <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px">
          <el-select
            v-model="filters.sources"
            multiple
            clearable
            placeholder="交易来源"
            style="width:230px"
            @change="search"
          >
            <el-option v-for="item in sourceOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
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
          <el-select
            v-model="filters.directions"
            multiple
            clearable
            placeholder="方向"
            style="width:160px"
            @change="search"
          >
            <el-option v-for="item in directionOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-select
            v-model="filters.statuses"
            multiple
            clearable
            placeholder="状态"
            style="width:180px"
            @change="search"
          >
            <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-select
            v-model="filters.exitReasons"
            multiple
            clearable
            collapse-tags
            collapse-tags-tooltip
            placeholder="退出原因"
            style="width:210px"
            @change="search"
          >
            <el-option v-for="item in exitReasonOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-date-picker
            v-model="filters.range"
            type="datetimerange"
            value-format="YYYY-MM-DD HH:mm:ss"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            style="width:360px"
            @change="search"
          />
          <el-button :icon="'RefreshLeft'" @click="resetFilters">重置</el-button>
        </div>

        <el-table :data="trades" stripe height="calc(100vh - 315px)" v-loading="loading" row-key="record_key">
          <el-table-column type="expand" width="42">
            <template #default="{ row }">
              <el-table :data="row.orders || []" size="small" stripe>
                <el-table-column label="时间" width="170">
                  <template #default="{ row: order }">{{ localTime(order.ts_ms, order.created_at) }}</template>
                </el-table-column>
                <el-table-column label="动作" width="130">
                  <template #default="{ row: order }">
                    <el-tag :type="orderKindTag(order.client_kind)" size="small">
                      {{ orderActionLabel(order) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="订单类型" width="150">
                  <template #default="{ row: order }">{{ orderTypeLabel(order.order_type) }}</template>
                </el-table-column>
                <el-table-column label="执行模式" width="110">
                  <template #default="{ row: order }">{{ executionModeLabel(order.execution_mode) }}</template>
                </el-table-column>
                <el-table-column label="流动性" width="80">
                  <template #default="{ row: order }">{{ liquidityLabel(order.liquidity) }}</template>
                </el-table-column>
                <el-table-column label="买卖" width="70">
                  <template #default="{ row: order }">{{ sideLabel(order.side) }}</template>
                </el-table-column>
                <el-table-column label="数量" width="110">
                  <template #default="{ row: order }"><span class="mono">{{ order.qty }}</span></template>
                </el-table-column>
                <el-table-column label="价格" width="110">
                  <template #default="{ row: order }"><span class="mono">{{ order.price }}</span></template>
                </el-table-column>
                <el-table-column label="名义价值" width="110">
                  <template #default="{ row: order }"><span class="mono">{{ fmt(order.notional) }}</span></template>
                </el-table-column>
                <el-table-column label="保证金" width="110">
                  <template #default="{ row: order }"><span class="mono">{{ fmt(order.margin) }}</span></template>
                </el-table-column>
                <el-table-column label="手续费" width="105">
                  <template #default="{ row: order }"><span class="mono">{{ fmt(order.fee, 4) }} {{ order.fee_asset || '' }}</span></template>
                </el-table-column>
                <el-table-column label="状态" min-width="140">
                  <template #default="{ row: order }">
                    <el-tag :type="orderStatusTag(order)" size="small">
                      {{ orderStatusLabel(order) }}
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </template>
          </el-table-column>
          <el-table-column label="开仓时间" width="180">
            <template #default="{ row }">{{ localTime(row.opened_at_ms, row.opened_at) }}</template>
          </el-table-column>
          <el-table-column prop="symbol" label="币种" width="100" />
          <el-table-column label="来源" width="185">
            <template #default="{ row }">
              <el-tooltip
                :content="row.record_type === 'external'
                  ? '该记录来自 Binance 外部成交，仅同步归档，Engine 未接管止盈止损或主动平仓。'
                  : '该记录由 Engine 策略创建并管理。'"
                placement="top"
              >
                <el-tag :type="row.record_type === 'external' ? 'warning' : 'primary'" size="small">
                  {{ row.source_label }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="方向" width="85">
            <template #default="{ row }">
              <el-tag :type="tradeDirectionTag(row.direction)" size="small">
                {{ tradeDirectionLabel(row.direction) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="tradeStatusTag(row.status)" size="small">
                {{ tradeStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="数量" width="110">
            <template #default="{ row }"><span class="mono">{{ row.qty_opened }}</span></template>
          </el-table-column>
          <el-table-column label="开/平价" width="150">
            <template #default="{ row }">
              <span class="mono">{{ row.entry_price ? fmt(row.entry_price, 4) : '—' }} / {{ row.exit_price ? fmt(row.exit_price, 4) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="杠杆" width="75">
            <template #default="{ row }">{{ row.leverage ? row.leverage + 'x' : '—' }}</template>
          </el-table-column>
          <el-table-column label="名义价值" width="115">
            <template #default="{ row }"><span class="mono">{{ fmt(row.entry_notional) }}</span></template>
          </el-table-column>
          <el-table-column label="保证金" width="105">
            <template #default="{ row }"><span class="mono">{{ fmt(row.entry_margin) }}</span></template>
          </el-table-column>
          <el-table-column label="手续费" width="105">
            <template #default="{ row }"><span class="mono">{{ row.status !== 'open' ? fmt(row.total_fee, 4) : '—' }}</span></template>
          </el-table-column>
          <el-table-column label="毛盈亏" width="105">
            <template #default="{ row }">
              <span class="mono" :class="pnlClass(row.realized_pnl)">{{ row.status !== 'open' ? fmt(row.realized_pnl) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="净盈亏" width="105">
            <template #default="{ row }">
              <span class="mono" :class="pnlClass(row.net_realized_pnl)">{{ row.status !== 'open' ? fmt(row.net_realized_pnl) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="保证金收益率" width="125">
            <template #default="{ row }">
              <span class="mono" :class="pnlClass(row.pnl_pct_on_margin)">
                {{ row.status !== 'open' && row.entry_margin ? fmt(row.pnl_pct_on_margin) + '%' : '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="退出原因" min-width="110">
            <template #default="{ row }">{{ row.status === 'closed' ? tradeExitReasonLabel(row) : '—' }}</template>
          </el-table-column>
        </el-table>
        <div style="display:flex; justify-content:flex-end; margin-top:12px">
          <el-pagination
            background
            layout="total, sizes, prev, pager, next"
            :total="tradeTotal"
            :current-page="currentPage"
            :page-size="page.limit"
            :page-sizes="[25, 50, 100]"
            @current-change="handlePageChange"
            @size-change="handleSizeChange"
          />
        </div>
      </template>

      <el-table v-else-if="tab === 'orders'" :data="orders" stripe height="calc(100vh - 240px)" v-loading="loading">
        <el-table-column label="本地时间" width="180">
          <template #default="{ row }">{{ localTime(row.ts_ms, row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="symbol" label="币种" width="100" />
        <el-table-column label="动作" width="130">
          <template #default="{ row }">
            <el-tag :type="orderKindTag(row.client_kind)" size="small">
              {{ orderActionLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="订单类型" width="150">
          <template #default="{ row }">{{ orderTypeLabel(row.order_type) }}</template>
        </el-table-column>
        <el-table-column label="执行模式" width="120">
          <template #default="{ row }">{{ executionModeLabel(row.execution_mode) }}</template>
        </el-table-column>
        <el-table-column label="流动性" width="90">
          <template #default="{ row }">{{ liquidityLabel(row.liquidity) }}</template>
        </el-table-column>
        <el-table-column label="买卖" width="80">
          <template #default="{ row }">{{ sideLabel(row.side) }}</template>
        </el-table-column>
        <el-table-column label="数量" width="120">
          <template #default="{ row }"><span class="mono">{{ row.qty }}</span></template>
        </el-table-column>
        <el-table-column label="价格" width="110">
          <template #default="{ row }"><span class="mono">{{ row.price }}</span></template>
        </el-table-column>
        <el-table-column label="名义价值" width="110">
          <template #default="{ row }"><span class="mono">{{ fmt(row.notional) }}</span></template>
        </el-table-column>
        <el-table-column label="保证金" width="110">
          <template #default="{ row }"><span class="mono">{{ fmt(row.margin) }}</span></template>
        </el-table-column>
        <el-table-column label="杠杆" width="80">
          <template #default="{ row }">{{ row.leverage ? row.leverage + 'x' : '—' }}</template>
        </el-table-column>
        <el-table-column label="手续费" width="120">
          <template #default="{ row }"><span class="mono">{{ fmt(row.fee, 4) }} {{ row.fee_asset || '' }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag :type="orderStatusTag(row)" size="small">
              {{ orderStatusLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <el-table v-else :data="rejects" stripe height="calc(100vh - 240px)" v-loading="loading">
        <el-table-column label="本地时间" width="180">
          <template #default="{ row }">{{ localTime(row.ts_ms, row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="symbol" label="币种" width="100" />
        <el-table-column label="拒单码" width="180">
          <template #default="{ row }">
            <el-tag type="danger" size="small">{{ rejectCodeLabel(row.code) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="动作" width="110">
          <template #default="{ row }">{{ decisionLabel(row.action) }}</template>
        </el-table-column>
        <el-table-column label="杠杆" width="70">
          <template #default="{ row }">{{ row.leverage }}x</template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="260" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
@media (max-width: 767px) {
  .page :deep(.el-card__header > div),
  .page :deep(.el-card__body > div) {
    max-width: 100%;
  }

  .page :deep(.el-card__body > div[style*="display:flex"]) {
    align-items: stretch !important;
    flex-direction: column;
  }
}
</style>
