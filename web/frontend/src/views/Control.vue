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
const configCommandIds = new Set()
const CONFIG_COMMANDS = new Set(['PAUSE', 'RESUME', 'RESUME_ALL_SYMBOLS', 'SET_SYMBOL_ENABLED', 'ADD_SYMBOL', 'REVIEW_SYMBOL'])
const SYMBOL_SYNC_ATTEMPTS = 8
const SYMBOL_SYNC_DELAY_MS = 500

async function loadCommands() {
  commands.value = await api.commands(50).catch(() => [])
}
async function loadCfg() {
  cfg.value = await api.config().catch(() => null)
}
async function refreshAll() {
  await Promise.all([loadCfg(), loadCommands()])
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
  }[name] || name
}

const strategyPaused = computed(() => Boolean(cfg.value?.strategy_paused))
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

onMounted(refreshAll)

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
    await loadCfg()
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
            <div style="display:flex; gap:12px">
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
          <div style="display:flex; gap:12px">
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
        <el-table-column label="LLM 调用" width="120">
          <template #default="{ row }">
            <el-tag
              :type="row.strategyPaused ? 'warning' : row.enabled ? 'success' : 'info'"
              size="small"
            >
              {{ row.strategyPaused ? '策略暂停' : row.enabled ? '允许' : '禁止' }}
            </el-tag>
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
</style>
