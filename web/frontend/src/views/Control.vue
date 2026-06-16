<script setup>
import { computed, ref, onMounted, watch } from 'vue'
import { api } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useLiveStore } from '../stores/live'
import { localTime } from '../labels'

const live = useLiveStore()
const cfg = ref(null)
const commands = ref([])
const loading = ref(false)
const symbolLoading = ref({})
const newSymbol = ref('')
const addSymbolLoading = ref(false)
const riskState = ref(null)
const riskForm = ref({})
const riskPreview = ref(null)
const riskLoading = ref(false)
const engineState = ref(null)
const engineForm = ref({})
const enginePreview = ref(null)
const engineLoading = ref(false)
const isTouchDevice = ref(false)
const tooltipTrigger = computed(() => (isTouchDevice.value ? 'click' : 'hover'))
const riskFields = [
  { key: 'max_leverage', label: '最大杠杆', min: 1, max: 125, step: 1 },
  { key: 'max_order_margin_pct', label: '单笔保证金占权益上限 (%)', min: 0.01, max: 100, step: 1, scale: 100 },
  { key: 'max_symbol_margin_pct', label: '单币种保证金占权益上限 (%)', min: 0.01, max: 100, step: 1, scale: 100 },
  { key: 'max_total_margin_pct', label: '总保证金占权益上限 (%)', min: 0.01, max: 100, step: 1, scale: 100 },
  { key: 'max_loss_per_order_margin_pct', label: '单笔止损占订单保证金上限 (%)', min: 0.01, max: 100, step: 1 },
  { key: 'liq_distance_min_pct', label: '最小爆仓距离 (%)', min: 0, max: 100, step: 1 },
  { key: 'daily_max_loss_pct', label: '日亏熔断 (%)', min: 0.01, max: 100, step: 1 },
  { key: 'max_drawdown_pct', label: '回撤熔断 (%)', min: 0.01, max: 100, step: 1 },
  { key: 'min_confidence', label: '最小置信度', min: 0, max: 1, step: 0.05 },
]
const engineCadenceFields = [
  { key: 'cycle_interval_seconds', label: '分析周期/主循环间隔（秒）', min: 60, max: 3600, step: 30 },
  { key: 'review_flat_seconds', label: '空仓最长复查间隔（秒）', min: 30, max: 86400, step: 30 },
  { key: 'review_position_seconds', label: '持仓最长复查间隔（秒）', min: 30, max: 86400, step: 30 },
  { key: 'review_near_exit_seconds', label: '接近退出最长复查间隔（秒）', min: 30, max: 86400, step: 30 },
  { key: 'review_high_vol_seconds', label: '高波动最长复查间隔（秒）', min: 30, max: 86400, step: 30 },
  { key: 'max_skip_cycles', label: '最大连续跳过周期', min: 1, max: 100, step: 1 },
]
const engineTriggerFields = [
  { key: 'price_change_pct', label: '价格变化触发 LLM (%)', min: 0, max: 20, step: 0.05 },
  { key: 'pnl_alert_pct', label: '持仓浮盈亏触发 LLM (%)', min: 0, max: 100, step: 0.1 },
  { key: 'near_exit_pnl_pct', label: '接近退出浮盈亏阈值 (%)', min: 0, max: 100, step: 0.1 },
  { key: 'trigger_on_order_event', label: '订单事件立即触发 LLM', type: 'bool' },
]
const engineSnapshotFields = [
  { key: 'feature_snapshot_enabled', label: '启用快照变化触发', type: 'bool' },
  { key: 'ema_spread_cross_min_pct', label: 'EMA spread 穿越最小幅度 (%)', min: 0, max: 5, step: 0.01 },
  { key: 'macd_hist_cross_min_abs', label: 'MACD histogram 穿越 deadzone', min: 0, max: 1000, step: 0.001 },
  { key: 'rsi_midline', label: 'RSI 中线', min: 1, max: 99, step: 1 },
  { key: 'boll_bandwidth_low_pct', label: 'Boll 低波动带宽 (%)', min: 0, max: 100, step: 0.1 },
  { key: 'boll_bandwidth_expand_pct', label: 'Boll 带宽扩张触发 (%)', min: 0, max: 1000, step: 1 },
  { key: 'volume_zscore_trigger', label: '成交量 z-score 触发', min: 0, max: 20, step: 0.1 },
  { key: 'micro_return_5m_trigger_pct', label: '1m 微观 5m return 触发 (%)', min: 0, max: 100, step: 0.05 },
  { key: 'micro_range_5m_trigger_pct', label: '1m 微观 5m range 触发 (%)', min: 0, max: 100, step: 0.05 },
]
const engineFields = [
  ...engineCadenceFields,
  ...engineTriggerFields,
  ...engineSnapshotFields,
]
const engineFieldHelp = {
  cycle_interval_seconds: 'Engine 主循环多久运行一次，单位秒。每轮会刷新行情、检查风控、判断是否需要调用 LLM。调小会更频繁分析，但上一轮 LLM 未返回时不会并发启动下一轮。',
  review_flat_seconds: '空仓且没有显著行情变化时，距离上次 LLM 决策超过该秒数后强制复查。调小会让空仓状态下更频繁询问 LLM。',
  review_position_seconds: '已有持仓但未接近退出区时，距离上次 LLM 决策超过该秒数后强制复查。调小会让持仓管理更频繁。',
  review_near_exit_seconds: '持仓浮盈亏绝对值达到“接近退出浮盈亏阈值”后使用的复查间隔。适合在接近止盈、止损、保本或移动止损区域时提高检查频率。',
  review_high_vol_seconds: '成交量、微观 return 或微观 range 达到高波动条件后使用的复查间隔。调小会在剧烈波动时更密集调用 LLM。',
  max_skip_cycles: '连续多少个周期被 throttle 判定“无显著变化”后，仍强制调用一次 LLM。数值越小，兜底调用越频繁。',
  price_change_pct: '当前价格相对上次 LLM 决策价格的变化百分比。达到该阈值会触发 LLM。数值越小，对价格变化越敏感。',
  pnl_alert_pct: '持仓浮盈亏百分比的绝对值达到该阈值会触发 LLM。用于盈利扩大或亏损扩大时提前复查。',
  near_exit_pnl_pct: '判定“接近退出区域”的持仓浮盈亏百分比阈值。达到后会使用接近退出复查间隔。',
  trigger_on_order_event: '开启后，成交、撤单、订单状态变化等事件会立即触发 LLM 复查，用于让策略快速响应交易所状态变化。',
  feature_snapshot_enabled: '开启后，Engine 会比较当前技术指标快照和上次 LLM 决策快照；趋势、波动、成交量等变化达到阈值时触发 LLM。',
  ema_spread_cross_min_pct: 'EMA 快慢线 spread 穿越零轴时，当前 spread 绝对值至少达到该百分比才触发。调小会更容易捕捉趋势翻转。',
  macd_hist_cross_min_abs: 'MACD histogram 穿越零轴的死区阈值。小于该绝对值的穿越被忽略，用于过滤弱噪音。',
  rsi_midline: 'RSI 中线阈值。RSI 从下向上或从上向下穿越该值会触发快照变化判断，默认通常为 50。',
  boll_bandwidth_low_pct: 'Bollinger bandwidth 被视为低波动区的阈值。只有先处于低波动区，再明显扩张，才触发波动扩张判断。',
  boll_bandwidth_expand_pct: 'Bollinger bandwidth 从低波动区扩张的相对百分比阈值。数值越小，波动刚开始放大时越容易触发。',
  volume_zscore_trigger: '成交量 z-score 上穿该阈值会触发 LLM，也用于高波动复查判断。数值越小，对放量越敏感。',
  micro_return_5m_trigger_pct: '1m 微观数据中最近 5 分钟 return 的绝对值阈值。达到后触发 LLM，也参与高波动判断。',
  micro_range_5m_trigger_pct: '1m 微观数据中最近 5 分钟价格区间百分比阈值。达到后触发 LLM，也参与高波动判断。',
}
const configCommandIds = new Set()
const CONFIG_COMMANDS = new Set(['PAUSE', 'RESUME', 'RESUME_ALL_SYMBOLS', 'SET_SYMBOL_ENABLED', 'ADD_SYMBOL', 'REVIEW_SYMBOL', 'UPDATE_ENGINE_SETTINGS'])
const SYMBOL_SYNC_ATTEMPTS = 8
const SYMBOL_SYNC_DELAY_MS = 500

async function loadCommands() {
  commands.value = await api.commands(50).catch(() => [])
}
async function loadCfg() {
  cfg.value = await api.config().catch(() => null)
}
async function refreshAll() {
  await Promise.all([loadCfg(), loadCommands(), loadRisk(), loadEngine()])
}

async function loadRisk() {
  riskState.value = await api.riskSettings().catch(() => null)
  if (!riskState.value) return
  riskForm.value = Object.fromEntries(riskFields.map((field) => [
    field.key,
    Number(riskState.value.effective[field.key]) * (field.scale || 1),
  ]))
  riskPreview.value = null
}

function riskPayload() {
  return Object.fromEntries(riskFields.map((field) => [
    field.key,
    Number(riskForm.value[field.key]) / (field.scale || 1),
  ]))
}

async function loadEngine() {
  engineState.value = await api.engineSettings().catch(() => null)
  if (!engineState.value) return
  engineForm.value = Object.fromEntries(engineFields.map((field) => [
    field.key,
    field.type === 'bool'
      ? Boolean(engineState.value.effective[field.key])
      : Number(engineState.value.effective[field.key]),
  ]))
  enginePreview.value = null
}

function enginePayload() {
  return Object.fromEntries(engineFields.map((field) => [
    field.key,
    field.type === 'bool'
      ? Boolean(engineForm.value[field.key])
      : Number(engineForm.value[field.key]),
  ]))
}

async function previewEngine() {
  engineLoading.value = true
  try {
    enginePreview.value = await api.enginePreview({
      expected_version: engineState.value.version,
      values: enginePayload(),
    })
  } catch (e) {
    ElMessage.error(`Engine 参数校验失败: ${e.message}`)
  } finally {
    engineLoading.value = false
  }
}

async function applyEngine() {
  await previewEngine()
  if (!enginePreview.value || Object.keys(enginePreview.value.changes || {}).length === 0) {
    ElMessage.info('没有 Engine 参数变化')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将立即应用 ${Object.keys(enginePreview.value.changes).length} 项 Engine 参数修改。当前 LLM 请求不会被中断，下一轮开始使用新参数。`,
      '确认应用 Engine 参数',
      { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' },
    )
    const result = await api.engineApply({
      expected_version: engineState.value.version,
      values: enginePayload(),
    })
    ElMessage.success(`Engine 参数更新命令已入队 (#${result.id})`)
    await loadCommands()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`应用失败: ${e.message || e}`)
  }
}

async function previewRisk() {
  riskLoading.value = true
  try {
    riskPreview.value = await api.riskPreview({
      expected_version: riskState.value.version,
      values: riskPayload(),
    })
  } catch (e) {
    ElMessage.error(`参数校验失败: ${e.message}`)
  } finally {
    riskLoading.value = false
  }
}

async function applyRisk() {
  await previewRisk()
  if (!riskPreview.value || Object.keys(riskPreview.value.changes || {}).length === 0) {
    ElMessage.info('没有参数变化')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将立即应用 ${Object.keys(riskPreview.value.changes).length} 项风险参数修改。降低熔断阈值可能立即触发强制平仓。`,
      '确认应用风险参数',
      { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' },
    )
    const result = await api.riskApply({
      expected_version: riskState.value.version,
      values: riskPayload(),
    })
    ElMessage.success(`风险参数更新命令已入队 (#${result.id})`)
    await loadCommands()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`应用失败: ${e.message || e}`)
  }
}

async function send(name, arg = '', confirmText = null) {
  if (confirmText) {
    try {
      const { value } = await ElMessageBox.prompt(
        `此操作不可逆。请输入 “${confirmText}” 确认执行 ${name}`,
        '危险操作确认',
        { confirmButtonText: '确认执行', cancelButtonText: '取消', inputPlaceholder: confirmText,
          confirmButtonClass: 'el-button--danger' }
      )
      if (value !== confirmText) {
        ElMessage.warning('确认词不匹配，已取消')
        return
      }
    } catch (_) { return /* 用户取消 */ }
  }
  loading.value = true
  try {
    const r = await api.command(name, arg)
    ElMessage.success(`命令已入队 (#${r.id})，交易进程将尽快执行`)
    await Promise.all([loadCommands(), loadCfg()])
  } catch (e) {
    ElMessage.error(`下发失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function mergeCommands(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return
  const byId = new Map(commands.value.map((row) => [row.id, row]))
  rows.forEach((row) => {
    byId.set(row.id, { ...(byId.get(row.id) || {}), ...row })
  })
  commands.value = Array.from(byId.values())
    .sort((a, b) => Number(b.id || 0) - Number(a.id || 0))
    .slice(0, 50)
}

function statusTag(s) {
  return { pending: 'warning', done: 'success', failed: 'danger' }[s] || 'info'
}

function commandLabel(name) {
  return {
    PAUSE: '暂停策略',
    RESUME: '恢复策略',
    RESUME_ALL_SYMBOLS: '开启全部币种策略',
    SET_SYMBOL_ENABLED: '切换币种交易',
    ADD_SYMBOL: '新增币种',
    REVIEW_SYMBOL: '复核币种',
    REPAIR_SL_TP: '补止盈止损',
    CANCEL_AND_FLATTEN: '撤单+平仓',
    STOP_ENGINE: '停止交易引擎',
    KILL_SWITCH: 'Kill Switch',
    CIRCUIT_BREAKER: '自动熔断',
    UPDATE_ENGINE_SETTINGS: '更新 Engine 参数',
    UPDATE_RISK_SETTINGS: '更新风险参数',
  }[name] || name
}

const strategyPaused = computed(() => Boolean(cfg.value?.strategy_paused))
const strategyPause = computed(() => cfg.value?.strategy_pause || {})
const strategyPauseReason = computed(() => strategyPause.value.reason || '')
const strategyPauseReasonCode = computed(() => strategyPause.value.reason_code || '')
const strategyPauseTooltip = computed(() =>
  [
    strategyPauseReasonCode.value,
    strategyPauseReason.value,
    strategyPause.value.source,
  ].filter(Boolean).join(' | ')
)
const streamState = computed(() => cfg.value?.user_stream || live.summary?.stream || {})
const streamTagType = computed(() => ({
  LIVE: 'success',
  RESYNCING: 'warning',
  DEGRADED: 'warning',
  DISCONNECTED: 'danger',
})[streamState.value.status] || 'info')
const configuredSymbols = computed(() => cfg.value?.symbols || [])
const allSymbolsEnabled = computed(() =>
  symbolRows.value
    .filter((row) => !row.needsReview)
    .every((row) => row.enabled)
)

const symbolRows = computed(() => {
  const rows = Array.isArray(cfg.value?.symbols_state) ? cfg.value.symbols_state : []
  const fallback = rows.length > 0
    ? rows
    : (cfg.value?.symbols || []).map((symbol) => ({ symbol, enabled: cfg.value?.symbol_enabled?.[symbol] !== false }))
  return fallback.map((item) => {
    const symbol = item.symbol
    return {
      symbol,
      enabled: cfg.value?.symbol_enabled?.[symbol] !== false && item.enabled !== false,
      strategyPaused: strategyPaused.value,
      strategyPauseReason: strategyPauseReason.value,
      strategyPauseReasonCode: strategyPauseReasonCode.value,
      strategyPauseTooltip: strategyPauseTooltip.value,
      hasPosition: (live.positions || []).some((p) =>
        p.symbol === symbol && Number(p.contracts || 0) > 0
      ),
      needsReview: Boolean(item.needs_review),
      syncStatus: item.sync_status || '',
      source: item.source || '',
      minQty: Number(item.min_qty || 0),
      minNotional: Number(item.min_notional || 0),
      tickSize: Number(item.tick_size || 0),
      stepSize: Number(item.step_size || 0),
      disabledReasonCode: item.disabled_reason_code || '',
      disabledReason: item.disabled_reason || '',
      disabledAt: item.disabled_at || '',
      disabledSource: item.disabled_source || '',
      disabledAction: item.disabled_action || '',
    }
  })
})

function setSymbolLoading(symbol, val) {
  const next = { ...symbolLoading.value }
  if (val) next[symbol] = true
  else delete next[symbol]
  symbolLoading.value = next
}

function sleep(ms) {
  return new Promise((resolve) => { setTimeout(resolve, ms) })
}

function applySymbolEnabled(symbol, enabled) {
  if (!cfg.value) return
  const symbolEnabled = { ...(cfg.value.symbol_enabled || {}), [symbol]: enabled }
  const symbolsState = Array.isArray(cfg.value.symbols_state)
    ? cfg.value.symbols_state.map((row) => (
        row.symbol === symbol ? { ...row, enabled } : row
      ))
    : (cfg.value.symbols || []).map((s) => ({ symbol: s, enabled: symbolEnabled[s] !== false }))
  cfg.value = { ...cfg.value, symbol_enabled: symbolEnabled, symbols_state: symbolsState }
}

function cfgSymbolEnabled(symbol) {
  return cfg.value?.symbol_enabled?.[symbol] !== false
}

function normalizedNewSymbol() {
  return String(newSymbol.value || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
}

async function syncSymbolEnabledResult(commandId, symbol, enabled) {
  for (let i = 0; i < SYMBOL_SYNC_ATTEMPTS; i += 1) {
    await sleep(SYMBOL_SYNC_DELAY_MS)
    const rows = await api.commands(50).catch(() => null)
    if (Array.isArray(rows)) commands.value = rows
    const row = rows?.find((item) => Number(item.id) === Number(commandId))
    if (row && (row.status === 'done' || row.status === 'failed')) {
      await loadCfg()
      if (row.status === 'failed') {
        ElMessage.error(`${symbol} 状态更新失败: ${row.result || '命令执行失败'}`)
      }
      return
    }
  }
  await Promise.all([loadCommands(), loadCfg()])
  if (cfgSymbolEnabled(symbol) !== enabled) {
    ElMessage.warning(`${symbol} 状态尚未确认，请稍后刷新`)
  }
}

async function setSymbolEnabled(symbol, enabled) {
  const row = symbolRows.value.find((item) => item.symbol === symbol)
  if (enabled && row?.needsReview) {
    ElMessage.warning(`${symbol} 需要人工复核后才能启用`)
    return
  }
  if (!enabled && symbolRows.value.find((row) => row.symbol === symbol)?.hasPosition) {
    try {
      await ElMessageBox.confirm(
        `${symbol} 当前仍有持仓。停用后不会继续请求 LLM 管理该币种，但交易所条件单仍会保留。`,
        '确认停用币种交易',
        { confirmButtonText: '确认停用', cancelButtonText: '取消', type: 'warning' }
      )
    } catch (_) { return }
  }
  setSymbolLoading(symbol, true)
  try {
    const r = await api.command('SET_SYMBOL_ENABLED', `${symbol}=${enabled ? 'true' : 'false'}`)
    applySymbolEnabled(symbol, enabled)
    ElMessage.success(`${symbol} ${enabled ? '启用' : '停用'}命令已入队 (#${r.id})`)
    await loadCommands()
    await syncSymbolEnabledResult(r.id, symbol, enabled)
  } catch (e) {
    ElMessage.error(`下发失败: ${e.message}`)
  } finally {
    setSymbolLoading(symbol, false)
  }
}

async function addSymbol() {
  const symbol = normalizedNewSymbol()
  if (!symbol) {
    ElMessage.warning('请输入币种，例如 SOLUSDT')
    return
  }
  if (!symbol.endsWith('USDT')) {
    ElMessage.warning('当前仅支持 USDT-M 合约币种，例如 SOLUSDT')
    return
  }
  addSymbolLoading.value = true
  try {
    const r = await api.command('ADD_SYMBOL', symbol)
    ElMessage.success(`${symbol} 新增命令已入队 (#${r.id})`)
    newSymbol.value = ''
    await loadCommands()
    await syncSymbolEnabledResult(r.id, symbol, false)
  } catch (e) {
    ElMessage.error(`新增失败: ${e.message}`)
  } finally {
    addSymbolLoading.value = false
  }
}

async function reviewSymbol(symbol) {
  setSymbolLoading(symbol, true)
  try {
    const r = await api.command('REVIEW_SYMBOL', symbol)
    ElMessage.success(`${symbol} 复核命令已入队 (#${r.id})`)
    await loadCommands()
    await syncSymbolEnabledResult(r.id, symbol, false)
  } catch (e) {
    ElMessage.error(`复核失败: ${e.message}`)
  } finally {
    setSymbolLoading(symbol, false)
  }
}

async function resumeAllSymbols() {
  const symbols = configuredSymbols.value.join(', ') || '全部配置币种'
  try {
    await ElMessageBox.confirm(
      `将恢复全局策略并启用全部币种：${symbols}。交易进程会先检查交易所无持仓、无普通挂单、无条件单；检查失败则不会开启。`,
      '确认开启全部币种策略',
      {
        confirmButtonText: '确认开启',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--success',
      }
    )
  } catch (_) { return }
  await send('RESUME_ALL_SYMBOLS')
}

onMounted(() => {
  isTouchDevice.value = Boolean(
    window.matchMedia?.('(hover: none)').matches || 'ontouchstart' in window
  )
  refreshAll()
})

watch(
  () => live.summary.recent_commands,
  async (rows) => {
    mergeCommands(rows)
    const shouldRefresh = Array.isArray(rows) && rows.some((row) => {
      if (!row?.id || configCommandIds.has(row.id)) return false
      if (!CONFIG_COMMANDS.has(row.command)) return false
      return row.status === 'done' || row.status === 'failed'
    })
    if (!shouldRefresh) return
    rows.forEach((row) => {
      if (row?.id && CONFIG_COMMANDS.has(row.command) && (row.status === 'done' || row.status === 'failed')) {
        configCommandIds.add(row.id)
      }
    })
    await Promise.all([loadCfg(), loadEngine()])
  },
  { deep: true }
)
</script>

<template>
  <div class="page">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom:16px"
      title="操作说明"
      description="所有命令写入命令队列，由交易主进程快速消费执行。Web 不直接操作交易所。" />

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>策略状态</template>
          <el-space direction="vertical" :size="14" fill style="width:100%">
            <div v-if="cfg">
              当前：
              <el-tag :type="strategyPaused ? 'warning' : 'success'" effect="dark">
                {{ strategyPaused ? '已暂停' : '运行中' }}
              </el-tag>
              <el-tag size="small" style="margin-left:8px">
                {{ cfg.strategy_status_source === 'runtime' ? '运行时持久化' : '默认状态' }}
              </el-tag>
            </div>
            <div class="control-action-row">
              <el-button type="warning" :loading="loading" :disabled="strategyPaused" style="flex:1"
                         :icon="'VideoPause'" @click="send('PAUSE')">
                暂停策略
              </el-button>
              <el-button type="success" :loading="loading" :disabled="!strategyPaused" style="flex:1"
                         :icon="'VideoPlay'" @click="send('RESUME')">
                恢复策略
              </el-button>
            </div>
            <el-button
              type="success"
              plain
              :loading="loading"
              :disabled="!cfg || (!strategyPaused && allSymbolsEnabled)"
              :icon="'CircleCheckFilled'"
              style="width:100%"
              @click="resumeAllSymbols"
            >
              开启策略并启用全部币种
            </el-button>
          </el-space>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>运行环境</template>
          <el-descriptions :column="1" border size="small" v-if="cfg">
            <el-descriptions-item label="交易环境">
              <el-tag :type="cfg.mode === 'mainnet' ? 'danger' : 'success'" effect="dark">
                {{ cfg.mode }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="数据库">
              <span class="mono">{{ cfg.db_path }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="Binance 私有流">
              <el-tag :type="streamTagType" effect="dark">
                {{ streamState.status || 'STARTING' }}
              </el-tag>
              <span v-if="streamState.reason" style="margin-left:8px">
                {{ streamState.reason }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="最近 REST 对账">
              {{ streamState.last_resync_at_ms ? localTime(streamState.last_resync_at_ms) : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="事件状态延迟">
              {{ streamState.event_lag_ms == null ? '—' : `${streamState.event_lag_ms} ms` }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>风险出清</template>
          <el-button
            type="danger"
            plain
            :loading="loading"
            :icon="'CircleCloseFilled'"
            style="width:100%"
            @click="send('CANCEL_AND_FLATTEN', '', 'FLATTEN')"
          >
            撤单 + 平仓
          </el-button>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>引擎控制</template>
          <div class="control-action-row">
            <el-button
              type="warning"
              :loading="loading"
              :icon="'SwitchButton'"
              style="flex:1"
              @click="send('STOP_ENGINE', '', 'STOP')"
            >
              停止交易引擎
            </el-button>
            <el-button
              type="danger"
              :loading="loading"
              :icon="'WarningFilled'"
              style="flex:1"
              @click="send('KILL_SWITCH', '', 'KILL')"
            >
              Kill Switch
            </el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>币种交易开关</template>
      <div class="symbol-add">
        <el-input
          v-model="newSymbol"
          placeholder="输入币种，例如 SOLUSDT"
          clearable
          style="max-width:260px"
          @keyup.enter="addSymbol"
        />
        <el-button
          type="primary"
          :loading="addSymbolLoading"
          :disabled="!normalizedNewSymbol()"
          @click="addSymbol"
        >
          新增币种
        </el-button>
      </div>
      <el-table :data="symbolRows" stripe>
        <el-table-column prop="symbol" label="币种" width="120" />
        <el-table-column label="状态" width="190">
          <template #default="{ row }">
            <el-space>
              <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                {{ row.enabled ? '已启用' : '已停用' }}
              </el-tag>
              <el-tooltip
                v-if="!row.enabled && (row.disabledReason || row.disabledReasonCode)"
                :content="[row.disabledReasonCode, row.disabledReason, row.disabledAt].filter(Boolean).join(' | ')"
                placement="top"
              >
                <el-tag type="danger" size="small">
                  {{ row.disabledReasonCode || 'DISABLED' }}
                </el-tag>
              </el-tooltip>
            </el-space>
          </template>
        </el-table-column>
        <el-table-column label="持仓" width="100">
          <template #default="{ row }">
            <el-tag :type="row.hasPosition ? 'warning' : 'info'" size="small">
              {{ row.hasPosition ? '有持仓' : '无持仓' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="LLM 调用" width="200">
          <template #default="{ row }">
            <el-space>
              <el-tag
                :type="row.strategyPaused ? 'warning' : row.enabled ? 'success' : 'info'"
                size="small"
              >
                {{ row.strategyPaused ? '策略暂停' : row.enabled ? '允许' : '禁止' }}
              </el-tag>
              <el-tooltip
                v-if="row.strategyPaused && (row.strategyPauseReasonCode || row.strategyPauseReason)"
                :content="row.strategyPauseTooltip"
                placement="top"
              >
                <el-tag type="danger" size="small">
                  {{ row.strategyPauseReasonCode || 'PAUSED' }}
                </el-tag>
              </el-tooltip>
            </el-space>
          </template>
        </el-table-column>
        <el-table-column label="同步状态" width="150">
          <template #default="{ row }">
            <el-tag
              :type="row.needsReview ? 'warning' : row.syncStatus === 'confirmed_flat' ? 'success' : 'info'"
              size="small"
            >
              {{ row.needsReview ? '需复核' : row.syncStatus || '未同步' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最小数量" width="110">
          <template #default="{ row }">{{ row.minQty || '-' }}</template>
        </el-table-column>
        <el-table-column label="最小名义价值" width="130">
          <template #default="{ row }">{{ row.minNotional || '-' }}</template>
        </el-table-column>
        <el-table-column label="来源" width="90">
          <template #default="{ row }">{{ row.source || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="160">
          <template #default="{ row }">
            <el-space>
              <el-button
                v-if="row.enabled"
                type="warning"
                size="small"
                :loading="Boolean(symbolLoading[row.symbol])"
                @click="setSymbolEnabled(row.symbol, false)"
              >
                停用交易
              </el-button>
              <el-button
                v-else
                type="success"
                size="small"
                :loading="Boolean(symbolLoading[row.symbol])"
                :disabled="row.needsReview"
                @click="setSymbolEnabled(row.symbol, true)"
              >
                启用交易
              </el-button>
              <el-button
                v-if="row.needsReview"
                type="primary"
                plain
                size="small"
                :loading="Boolean(symbolLoading[row.symbol])"
                @click="reviewSymbol(row.symbol)"
              >
                重新复核
              </el-button>
            </el-space>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>动态风险参数</span>
          <el-tag v-if="riskState" type="info">版本 {{ riskState.version }}</el-tag>
        </div>
      </template>
      <el-alert type="warning" :closable="false" style="margin-bottom:12px"
        title="参数由交易引擎通过命令队列原子应用；mainnet 需要输入 MAINNET 二次确认。" />
      <el-form v-if="riskState" label-width="245px">
        <el-row :gutter="16">
          <el-col v-for="field in riskFields" :key="field.key" :span="12">
            <el-form-item :label="field.label">
              <el-input-number v-model="riskForm[field.key]" :min="field.min" :max="field.max"
                :step="field.step" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <el-table v-if="riskPreview && Object.keys(riskPreview.changes || {}).length" :data="Object.entries(riskPreview.changes).map(([key, value]) => ({ key, ...value }))" size="small" style="margin-bottom:12px">
        <el-table-column prop="key" label="参数" />
        <el-table-column prop="before" label="修改前" />
        <el-table-column prop="after" label="修改后" />
      </el-table>
      <el-alert v-if="riskPreview?.impact?.would_trigger_daily_loss || riskPreview?.impact?.would_trigger_drawdown"
        type="error" :closable="false" show-icon style="margin-bottom:12px"
        title="应用后将立即触发熔断并强制平仓" />
      <div class="risk-actions">
        <el-button :loading="riskLoading" @click="previewRisk">预览影响</el-button>
        <el-button type="primary" :loading="riskLoading" @click="applyRisk">确认并立即应用</el-button>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>引擎分析与 LLM 调用频率</span>
          <el-tag v-if="engineState" type="info">版本 {{ engineState.version }}</el-tag>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="说明"
        description="分析周期控制 Engine 多久跑一轮。Throttle 决定本轮是否调用 LLM；上一轮 LLM 未返回时不会并发调用下一轮，当前请求结束后才进入下一轮。"
      />
      <el-form v-if="engineState" label-width="260px">
        <el-divider content-position="left">分析周期与最长复查</el-divider>
        <el-row :gutter="16">
          <el-col v-for="field in engineCadenceFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label>
                <span class="param-label">
                  {{ field.label }}
                  <el-tooltip
                    :content="engineFieldHelp[field.key]"
                    placement="top"
                    effect="dark"
                    :trigger="tooltipTrigger"
                    popper-class="engine-param-tooltip"
                    :show-after="200"
                  >
                    <span class="param-help">?</span>
                  </el-tooltip>
                </span>
              </template>
              <el-input-number
                v-model="engineForm[field.key]"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                style="width:100%"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">LLM 触发条件</el-divider>
        <el-row :gutter="16">
          <el-col v-for="field in engineTriggerFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label>
                <span class="param-label">
                  {{ field.label }}
                  <el-tooltip
                    :content="engineFieldHelp[field.key]"
                    placement="top"
                    effect="dark"
                    :trigger="tooltipTrigger"
                    popper-class="engine-param-tooltip"
                    :show-after="200"
                  >
                    <span class="param-help">?</span>
                  </el-tooltip>
                </span>
              </template>
              <el-switch v-if="field.type === 'bool'" v-model="engineForm[field.key]" />
              <el-input-number
                v-else
                v-model="engineForm[field.key]"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                style="width:100%"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">快照变化判断阈值</el-divider>
        <el-alert
          type="warning"
          :closable="false"
          style="margin-bottom:12px"
          title="快照变化触发包含 EMA/MACD/RSI/Boll/成交量/微观波动/BTC leader 趋势等变化。阈值越低，LLM 调用越频繁。"
        />
        <el-row :gutter="16">
          <el-col v-for="field in engineSnapshotFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label>
                <span class="param-label">
                  {{ field.label }}
                  <el-tooltip
                    :content="engineFieldHelp[field.key]"
                    placement="top"
                    effect="dark"
                    :trigger="tooltipTrigger"
                    popper-class="engine-param-tooltip"
                    :show-after="200"
                  >
                    <span class="param-help">?</span>
                  </el-tooltip>
                </span>
              </template>
              <el-switch v-if="field.type === 'bool'" v-model="engineForm[field.key]" />
              <el-input-number
                v-else
                v-model="engineForm[field.key]"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                style="width:100%"
              />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <el-table
        v-if="enginePreview && Object.keys(enginePreview.changes || {}).length"
        :data="Object.entries(enginePreview.changes).map(([key, value]) => ({ key, ...value }))"
        size="small"
        style="margin-bottom:12px"
      >
        <el-table-column prop="key" label="参数" />
        <el-table-column prop="before" label="修改前" />
        <el-table-column prop="after" label="修改后" />
      </el-table>
      <el-alert
        v-if="enginePreview?.impact"
        type="success"
        :closable="false"
        style="margin-bottom:12px"
        :title="`应用后分析周期 ${enginePreview.impact.cycle_interval_seconds}s；最短最长复查 ${enginePreview.impact.shortest_review_seconds}s；LLM 并发调用：否`"
      />
      <div class="risk-actions">
        <el-button :loading="engineLoading" @click="previewEngine">预览 Engine 参数</el-button>
        <el-button type="primary" :loading="engineLoading" @click="applyEngine">确认并立即应用</el-button>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div class="card-header-row">
          <span>命令历史</span>
          <el-button size="small" :icon="'Refresh'" @click="refreshAll">刷新</el-button>
        </div>
      </template>
      <el-table :data="commands" stripe max-height="340">
        <el-table-column label="入队时间" width="180">
          <template #default="{ row }">{{ localTime(row.created_at_ms) }}</template>
        </el-table-column>
        <el-table-column label="命令" width="150">
          <template #default="{ row }">{{ commandLabel(row.command) }}</template>
        </el-table-column>
        <el-table-column prop="arg" label="参数" width="90" />
        <el-table-column prop="source" label="来源" width="140" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="result" label="结果" min-width="200" show-overflow-tooltip />
        <el-table-column label="执行时间" width="180">
          <template #default="{ row }">{{ localTime(row.executed_at_ms) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.symbol-add {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}

.control-action-row,
.risk-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.risk-actions {
  flex-wrap: wrap;
}

.param-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: normal;
}

.param-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #eef5ff;
  color: #409eff;
  cursor: help;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  flex: 0 0 auto;
}

:global(.engine-param-tooltip) {
  max-width: min(340px, calc(100vw - 32px));
  line-height: 1.5;
  word-break: break-word;
}

@media (max-width: 767px) {
  .symbol-add {
    align-items: stretch;
    flex-direction: column;
  }

  .control-action-row,
  .risk-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .control-action-row .el-button,
  .risk-actions .el-button {
    width: 100%;
  }

  .param-label {
    max-width: 100%;
    line-height: 1.35;
  }
}
</style>
