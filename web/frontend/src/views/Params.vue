<script setup>
import { computed, ref, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const cfg = ref(null)
const riskState = ref(null)
const riskForm = ref({})
const riskPreview = ref(null)
const riskLoading = ref(false)
const engineState = ref(null)
const engineForm = ref({})
const enginePreview = ref(null)
const engineLoading = ref(false)
const executionState = ref(null)
const executionForm = ref({})
const executionPreview = ref(null)
const executionLoading = ref(false)
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
const engineFields = [...engineCadenceFields, ...engineTriggerFields, ...engineSnapshotFields]
const engineFieldHelp = {
  cycle_interval_seconds: 'Engine 主循环多久运行一次。每轮刷新行情、检查风控、判断是否调用 LLM；上一轮 LLM 未结束时不会并发启动下一轮。',
  review_flat_seconds: '空仓且没有显著行情变化时，距离上次 LLM 决策超过该秒数后强制复查。',
  review_position_seconds: '已有持仓但未接近退出区时，距离上次 LLM 决策超过该秒数后强制复查。',
  review_near_exit_seconds: '持仓浮盈亏达到“接近退出浮盈亏阈值”后使用的最长复查间隔。',
  review_high_vol_seconds: '成交量、微观 return 或 range 达到高波动条件后使用的最长复查间隔。',
  max_skip_cycles: '连续多少个周期被判定无显著变化后，仍强制调用一次 LLM。',
  price_change_pct: '当前价格相对上次 LLM 决策价格的变化百分比，达到后触发 LLM。',
  pnl_alert_pct: '持仓浮盈亏百分比的绝对值达到该阈值后触发 LLM。',
  near_exit_pnl_pct: '判定接近退出区域的持仓浮盈亏百分比阈值。',
  trigger_on_order_event: '私有流成交、撤单、订单状态变化等事件会触发快速复查。',
  feature_snapshot_enabled: '比较当前技术指标快照和上次 LLM 决策快照；趋势、波动、成交量变化达到阈值时触发。',
  ema_spread_cross_min_pct: 'EMA 快慢线 spread 穿越零轴时，当前 spread 绝对值至少达到该百分比才触发。',
  macd_hist_cross_min_abs: 'MACD histogram 穿越零轴的死区阈值，用于过滤弱噪音。',
  rsi_midline: 'RSI 穿越该中线值时参与快照变化判断，默认通常为 50。',
  boll_bandwidth_low_pct: 'Bollinger bandwidth 被视为低波动区的阈值。',
  boll_bandwidth_expand_pct: 'Bollinger bandwidth 从低波动区扩张的相对百分比阈值。',
  volume_zscore_trigger: '成交量 z-score 上穿该阈值会触发 LLM，也用于高波动判断。',
  micro_return_5m_trigger_pct: '1m 微观数据最近 5 分钟 return 的绝对值阈值。',
  micro_range_5m_trigger_pct: '1m 微观数据最近 5 分钟价格区间百分比阈值。',
}

const executionFields = [
  { key: 'entry_mode', label: '开仓执行模式', type: 'select', options: ['MAKER_FIRST', 'MAKER_ONLY', 'MARKET_TAKER'] },
  { key: 'maker_timeout_seconds', label: '单次 maker 等待超时（秒）', min: 1, max: 120, step: 1 },
  { key: 'maker_poll_seconds', label: 'maker 订单轮询间隔（秒）', min: 0.1, max: 10, step: 0.1 },
  { key: 'maker_max_requotes', label: 'maker 重新挂单次数', min: 0, max: 10, step: 1 },
  { key: 'maker_price_offset_bps', label: 'maker 挂单偏移（bps）', min: 0, max: 100, step: 0.1 },
  { key: 'maker_unfilled_action', label: 'maker 未成交处理', type: 'select', options: ['FALLBACK_MARKET', 'CANCEL'] },
  { key: 'market_slippage_bps', label: '市价兜底滑点上限（bps）', min: 0.1, max: 100, step: 0.1 },
  { key: 'max_order_retries', label: '下单瞬时错误重试次数', min: 0, max: 10, step: 1 },
  { key: 'rate_limit_backoff', label: '限频/网络错误退避倍数', min: 1.01, max: 10, step: 0.1 },
]
const executionFieldHelp = {
  entry_mode: 'MAKER_FIRST 先挂 post-only 限价，未成交按配置兜底；MAKER_ONLY 不兜底；MARKET_TAKER 直接市价。',
  maker_timeout_seconds: '每次 maker 挂单等待成交的最长时间。总最坏等待约等于 (重新挂单次数 + 1) × 该值。',
  maker_poll_seconds: '等待 maker 订单成交时查询订单状态的间隔。太小会增加 API 请求。',
  maker_max_requotes: '首次挂单后允许重新报价的次数。实际 maker 尝试次数 = 该值 + 1。',
  maker_price_offset_bps: '挂单偏移单位 bps，1 bps = 0.01%。买单挂在买一价下方，卖单挂在卖一价上方，以降低 taker 风险。',
  maker_unfilled_action: 'maker 全部未成交时的处理。FALLBACK_MARKET 会先做滑点预检再市价开仓；CANCEL 则放弃开仓。',
  market_slippage_bps: '市价单或 maker 兜底市价前，用盘口估算冲击价，超过该 bps 就拒单。',
  max_order_retries: '仅对限频、网络、DDoS 等瞬时错误重试；交易所业务错误不会重试。实际请求次数 = 1 + 该值。',
  rate_limit_backoff: '瞬时错误重试时的指数退避基数，越大等待越长。',
  market_slippage_bps_per_symbol: '按币种覆盖市价滑点上限。留空使用全局市价兜底滑点上限。',
}
const fixedExecutionHelp = {
  maker_time_in_force: '固定 GTX，即 post-only。改动会改变 maker 语义，不建议前端热调。',
  normal_exit_mode: '普通平仓固定 MARKET_TAKER，退出优先级高于手续费优化。',
  emergency_exit_mode: '紧急退出固定 MARKET_TAKER，熔断/强平保护必须优先成交。',
  partial_fill_action: '部分成交后保护已成交仓位并撤剩余，不允许裸露仓位。',
  attach_sl_tp: '开仓后必须附加 SL/TP 保护。mainnet 不允许关闭。',
  recv_window: 'Binance 请求时间窗口，属于连接层参数，不建议运行期调整。',
  order_type: '旧版兼容字段，当前逻辑使用 entry_mode。',
}

const executionSymbols = computed(() => cfg.value?.symbols || Object.keys(executionForm.value.market_slippage_bps_per_symbol || {}))

async function refreshAll() {
  cfg.value = await api.config().catch(() => null)
  await Promise.all([loadRisk(), loadEngine(), loadExecution()])
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

async function loadExecution() {
  executionState.value = await api.executionSettings().catch(() => null)
  if (!executionState.value) return
  executionForm.value = {
    ...Object.fromEntries(executionFields.map((field) => [
      field.key,
      field.type === 'select'
        ? executionState.value.effective[field.key]
        : Number(executionState.value.effective[field.key]),
    ])),
    market_slippage_bps_per_symbol: {
      ...(executionState.value.effective.market_slippage_bps_per_symbol || {}),
    },
  }
  for (const symbol of cfg.value?.symbols || []) {
    if (executionForm.value.market_slippage_bps_per_symbol[symbol] == null) {
      executionForm.value.market_slippage_bps_per_symbol[symbol] =
        Number(executionState.value.effective.market_slippage_bps)
    }
  }
  executionPreview.value = null
}

function executionPayload() {
  const payload = Object.fromEntries(executionFields.map((field) => [
    field.key,
    field.type === 'select' ? executionForm.value[field.key] : Number(executionForm.value[field.key]),
  ]))
  payload.market_slippage_bps_per_symbol = Object.fromEntries(
    executionSymbols.value.map((symbol) => [
      symbol,
      Number(executionForm.value.market_slippage_bps_per_symbol?.[symbol]),
    ])
  )
  return payload
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
    ElMessage.info('没有风险参数变化')
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
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`应用失败: ${e.message || e}`)
  }
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
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`应用失败: ${e.message || e}`)
  }
}

async function previewExecution() {
  executionLoading.value = true
  try {
    executionPreview.value = await api.executionPreview({
      expected_version: executionState.value.version,
      values: executionPayload(),
    })
  } catch (e) {
    ElMessage.error(`执行参数校验失败: ${e.message}`)
  } finally {
    executionLoading.value = false
  }
}

async function applyExecution() {
  await previewExecution()
  if (!executionPreview.value || Object.keys(executionPreview.value.changes || {}).length === 0) {
    ElMessage.info('没有执行参数变化')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将立即应用 ${Object.keys(executionPreview.value.changes).length} 项执行参数修改。后续新订单使用新参数，已挂订单不回改。`,
      '确认应用挂单/执行参数',
      { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' },
    )
    const result = await api.executionApply({
      expected_version: executionState.value.version,
      values: executionPayload(),
    })
    ElMessage.success(`执行参数更新命令已入队 (#${result.id})`)
    await refreshAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(`应用失败: ${e.message || e}`)
  }
}

onMounted(() => {
  isTouchDevice.value = Boolean(
    window.matchMedia?.('(hover: none)').matches || 'ontouchstart' in window
  )
  refreshAll()
})
</script>

<template>
  <div class="page">
    <el-alert
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom:16px"
      title="参数控制说明"
      description="本页参数通过命令队列由交易引擎热应用；mainnet 修改需要输入 MAINNET 二次确认。已存在的持仓和挂单不会被回写修改。"
    />

    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span>动态风险参数</span>
          <el-tag v-if="riskState" type="info">版本 {{ riskState.version }}</el-tag>
        </div>
      </template>
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
      <div class="param-actions">
        <el-button :loading="riskLoading" @click="previewRisk">预览风险影响</el-button>
        <el-button type="primary" :loading="riskLoading" @click="applyRisk">确认并立即应用</el-button>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div class="card-header-row">
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
        description="分析周期控制 Engine 多久跑一轮。Throttle 决定本轮是否调用 LLM；上一轮 LLM 未返回时不会并发调用下一轮。"
      />
      <el-form v-if="engineState" label-width="260px">
        <el-divider content-position="left">分析周期与最长复查</el-divider>
        <el-row :gutter="16">
          <el-col v-for="field in engineCadenceFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label><span class="param-label">{{ field.label }}<el-tooltip :content="engineFieldHelp[field.key]" placement="top" effect="dark" :trigger="tooltipTrigger" popper-class="param-tooltip" :show-after="200"><span class="param-help">?</span></el-tooltip></span></template>
              <el-input-number v-model="engineForm[field.key]" :min="field.min" :max="field.max" :step="field.step" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">LLM 触发条件</el-divider>
        <el-row :gutter="16">
          <el-col v-for="field in engineTriggerFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label><span class="param-label">{{ field.label }}<el-tooltip :content="engineFieldHelp[field.key]" placement="top" effect="dark" :trigger="tooltipTrigger" popper-class="param-tooltip" :show-after="200"><span class="param-help">?</span></el-tooltip></span></template>
              <el-switch v-if="field.type === 'bool'" v-model="engineForm[field.key]" />
              <el-input-number v-else v-model="engineForm[field.key]" :min="field.min" :max="field.max" :step="field.step" style="width:100%" />
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
              <template #label><span class="param-label">{{ field.label }}<el-tooltip :content="engineFieldHelp[field.key]" placement="top" effect="dark" :trigger="tooltipTrigger" popper-class="param-tooltip" :show-after="200"><span class="param-help">?</span></el-tooltip></span></template>
              <el-switch v-if="field.type === 'bool'" v-model="engineForm[field.key]" />
              <el-input-number v-else v-model="engineForm[field.key]" :min="field.min" :max="field.max" :step="field.step" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <el-table v-if="enginePreview && Object.keys(enginePreview.changes || {}).length" :data="Object.entries(enginePreview.changes).map(([key, value]) => ({ key, ...value }))" size="small" style="margin-bottom:12px">
        <el-table-column prop="key" label="参数" />
        <el-table-column prop="before" label="修改前" />
        <el-table-column prop="after" label="修改后" />
      </el-table>
      <el-alert v-if="enginePreview?.impact" type="success" :closable="false" style="margin-bottom:12px"
        :title="`应用后分析周期 ${enginePreview.impact.cycle_interval_seconds}s；最短最长复查 ${enginePreview.impact.shortest_review_seconds}s；LLM 并发调用：否`" />
      <div class="param-actions">
        <el-button :loading="engineLoading" @click="previewEngine">预览 Engine 参数</el-button>
        <el-button type="primary" :loading="engineLoading" @click="applyEngine">确认并立即应用</el-button>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div class="card-header-row">
          <span>挂单策略与执行参数</span>
          <el-tag v-if="executionState" type="info">版本 {{ executionState.version }}</el-tag>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="说明"
        description="这些参数只影响后续新订单。maker 部分成交后会按剩余数量重挂；累计达到目标数量即停止，避免超开。"
      />
      <el-form v-if="executionState" label-width="260px">
        <el-row :gutter="16">
          <el-col v-for="field in executionFields" :key="field.key" :span="12">
            <el-form-item>
              <template #label><span class="param-label">{{ field.label }}<el-tooltip :content="executionFieldHelp[field.key]" placement="top" effect="dark" :trigger="tooltipTrigger" popper-class="param-tooltip" :show-after="200"><span class="param-help">?</span></el-tooltip></span></template>
              <el-select v-if="field.type === 'select'" v-model="executionForm[field.key]" style="width:100%">
                <el-option v-for="opt in field.options" :key="opt" :label="opt" :value="opt" />
              </el-select>
              <el-input-number v-else v-model="executionForm[field.key]" :min="field.min" :max="field.max" :step="field.step" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">按币种市价滑点上限（bps）</el-divider>
        <el-row :gutter="16">
          <el-col v-for="symbol in executionSymbols" :key="symbol" :span="12">
            <el-form-item>
              <template #label><span class="param-label">{{ symbol }} 滑点上限<el-tooltip :content="executionFieldHelp.market_slippage_bps_per_symbol" placement="top" effect="dark" :trigger="tooltipTrigger" popper-class="param-tooltip" :show-after="200"><span class="param-help">?</span></el-tooltip></span></template>
              <el-input-number v-model="executionForm.market_slippage_bps_per_symbol[symbol]" :min="0.1" :max="100" :step="0.1" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">固定项与不建议热调项</el-divider>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item v-for="(value, key) in executionState.fixed" :key="key" :label="key">
            <span class="mono">{{ value ?? '—' }}</span>
            <span class="fixed-help">{{ fixedExecutionHelp[key] }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </el-form>
      <el-table v-if="executionPreview && Object.keys(executionPreview.changes || {}).length" :data="Object.entries(executionPreview.changes).map(([key, value]) => ({ key, ...value }))" size="small" style="margin:12px 0">
        <el-table-column prop="key" label="参数" />
        <el-table-column prop="before" label="修改前" />
        <el-table-column prop="after" label="修改后" />
      </el-table>
      <el-alert v-if="executionPreview?.impact" type="success" :closable="false" style="margin-bottom:12px"
        :title="`maker 尝试 ${executionPreview.impact.maker_attempts} 次；最坏等待 ${executionPreview.impact.worst_maker_wait_seconds}s；未成交市价兜底：${executionPreview.impact.fallback_market ? '是' : '否'}`" />
      <div class="param-actions">
        <el-button :loading="executionLoading" @click="previewExecution">预览执行参数</el-button>
        <el-button type="primary" :loading="executionLoading" @click="applyExecution">确认并立即应用</el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.param-actions {
  display: flex;
  gap: 12px;
  align-items: center;
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

.fixed-help {
  display: block;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}

:global(.param-tooltip) {
  max-width: min(360px, calc(100vw - 32px));
  line-height: 1.5;
  word-break: break-word;
}

@media (max-width: 767px) {
  .param-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .param-actions .el-button {
    width: 100%;
  }

  .param-label {
    max-width: 100%;
    line-height: 1.35;
  }
}
</style>
