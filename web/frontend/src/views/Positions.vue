<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { useLiveStore } from '../stores/live'
import { orderStatusLabel, utc8DateTime, utc8Time } from '../labels'

const live = useLiveStore()
const repairing = ref({})
const takeoverVisible = ref(false)
const takeoverSubmitting = ref(false)
const takeoverRow = ref(null)
const closeVisible = ref(false)
const closeSubmitting = ref(false)
const closeRow = ref(null)
const closeConfirm = ref(false)
const takeoverForm = ref({
  qty: '',
  sl: '',
  tp: '',
  confirm: false,
})
const positions = computed(() => live.positions || [])
const positionsSource = computed(() => live.summary?.positions_source || 'db_snapshot')
const positionsError = computed(() => live.summary?.positions_error || '')
const conditionOrdersError = computed(() => live.summary?.condition_orders_error || '')
const openOrders = computed(() => live.summary?.open_orders || [])
const openOrdersError = computed(() => live.summary?.open_orders_error || '')
const openOrdersSyncedAtMs = computed(() => live.summary?.open_orders_synced_at_ms || live.summary?.positions_synced_at_ms)
const openOrdersSyncedText = computed(() => {
  const ts = openOrdersSyncedAtMs.value
  return utc8Time(ts)
})
const syncedAtText = computed(() => {
  const ts = live.summary?.positions_synced_at_ms
  return utc8Time(ts)
})
const isExchangeLive = computed(() => positionsSource.value === 'exchange')
const hasMissingProtection = computed(() => positions.value.some((p) =>
  p.protection?.missing_sl || p.protection?.missing_tp
))
// B5：暴露 symbol_enabled 表与「持仓 + 币种已禁用」集合。
const symbolEnabledMap = computed(() => live.summary?.symbol_enabled || {})
const disabledWithPosition = computed(() => new Set(live.summary?.disabled_with_position || []))
function isSymbolDisabled(symbol) {
  return symbolEnabledMap.value[symbol] === false
}
const enabling = ref({})
async function enableSymbol(row) {
  const symbol = row.symbol
  if (!symbol) return
  enabling.value = { ...enabling.value, [symbol]: true }
  try {
    await api.command('SET_SYMBOL_ENABLED', `${symbol}=true`)
    ElMessage.success(`${symbol} 已重新启用，孤儿持仓将在下个周期自动接管并补 SL/TP`)
  } catch (e) {
    ElMessage.error(`启用 ${symbol} 失败: ${e.message}`)
  } finally {
    const next = { ...enabling.value }
    delete next[symbol]
    enabling.value = next
  }
}

function fmt(n, d = 4) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? v.toFixed(d) : '—'
}

function fmtPct(n) {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  return Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : '—'
}

function fmtTime(ts) {
  return utc8DateTime(ts)
}

function margin(row) {
  return Number(row.isolated_margin || row.initial_margin || 0)
}

function estimatedClosePnl(row) {
  const qty = Number(row.contracts || 0)
  const entry = Number(row.entry_price || 0)
  const mark = Number(row.mark_price || 0)
  if (!Number.isFinite(qty) || !Number.isFinite(entry) || !Number.isFinite(mark)) return 0
  if (row.side === 'long') return (mark - entry) * qty
  if (row.side === 'short') return (entry - mark) * qty
  return 0
}

function protection(row, kind) {
  return kind === 'SL' ? row.protection?.sl : row.protection?.tp
}

function needsRepair(row) {
  return Boolean(row.protection?.missing_sl || row.protection?.missing_tp)
}

function missingProtectionText(row) {
  const missing = []
  if (row.protection?.missing_sl) missing.push('止损')
  if (row.protection?.missing_tp) missing.push('止盈')
  return missing.length ? `缺少${missing.join('、')}` : '保护完整'
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

function isRepairing(row) {
  return Boolean(repairing.value[row.symbol])
}

async function repairProtection(row) {
  const symbol = row.symbol
  if (!symbol) return
  repairing.value = { ...repairing.value, [symbol]: true }
  try {
    const res = await api.command('REPAIR_SL_TP', symbol)
    ElMessage.success(`${symbol} 补止盈止损命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`补单命令下发失败: ${e.message}`)
  } finally {
    const next = { ...repairing.value }
    delete next[symbol]
    repairing.value = next
  }
}

function openTakeover(row) {
  takeoverRow.value = row
  takeoverForm.value = {
    qty: row.contracts || '',
    sl: '',
    tp: row.protection?.tp?.trigger_price || row.protection?.tp?.price || '',
    confirm: false,
  }
  takeoverVisible.value = true
}

function recomputeTakeover() {
  const row = takeoverRow.value
  if (!row) return
  const mark = Number(row.mark_price || 0)
  const entry = Number(row.entry_price || 0)
  if (!Number.isFinite(mark) || mark <= 0 || !Number.isFinite(entry) || entry <= 0) return
  if (row.side === 'long') {
    takeoverForm.value.sl = (mark * 0.99).toFixed(2)
    takeoverForm.value.tp = (entry * 1.02).toFixed(2)
  } else if (row.side === 'short') {
    takeoverForm.value.sl = (mark * 1.01).toFixed(2)
    takeoverForm.value.tp = (entry * 0.98).toFixed(2)
  }
}

async function submitTakeover() {
  const row = takeoverRow.value
  if (!row) return
  if (!takeoverForm.value.confirm) {
    ElMessage.error('请先确认接管当前持仓')
    return
  }
  const sl = Number(takeoverForm.value.sl)
  const qty = Number(takeoverForm.value.qty)
  if (!Number.isFinite(sl) || sl <= 0 || !Number.isFinite(qty) || qty <= 0) {
    ElMessage.error('请输入有效的接管数量和止损触发价')
    return
  }
  const tp = Number(takeoverForm.value.tp)
  const payload = {
    symbol: row.symbol,
    mode: 'manual',
    qty,
    sl_trigger: sl,
    confirm: true,
    position: {
      side: row.side,
      qty: row.contracts,
      entry: row.entry_price,
    },
  }
  if (Number.isFinite(tp) && tp > 0) payload.tp_trigger = tp
  takeoverSubmitting.value = true
  try {
    const res = await api.command('PROTECT_POSITION', JSON.stringify(payload))
    ElMessage.success(`${row.symbol} 接管保护命令已入队 (#${res.id})`)
    takeoverVisible.value = false
  } catch (e) {
    ElMessage.error(`接管保护命令下发失败: ${e.message}`)
  } finally {
    takeoverSubmitting.value = false
  }
}

function openManualClose(row) {
  closeRow.value = row
  closeConfirm.value = false
  closeVisible.value = true
}

async function submitManualClose() {
  const row = closeRow.value
  if (!row) return
  if (!closeConfirm.value) {
    ElMessage.error('请先确认市价平仓')
    return
  }
  const payload = {
    symbol: row.symbol,
    confirm: true,
    force: true,
    position: {
      side: row.side,
      qty: row.contracts,
      entry: row.entry_price,
    },
  }
  closeSubmitting.value = true
  try {
    const res = await api.command('CLOSE_POSITION', JSON.stringify(payload))
    ElMessage.success(`${row.symbol} 手动平仓命令已入队 (#${res.id})`)
    closeVisible.value = false
  } catch (e) {
    ElMessage.error(`手动平仓命令下发失败: ${e.message}`)
  } finally {
    closeSubmitting.value = false
  }
}

const cancelOrderSubmitting = ref({})
function cancelOrderKey(symbol, orderId) {
  return `${symbol}::${orderId}`
}
function isCancelingOrder(symbol, orderId) {
  return Boolean(cancelOrderSubmitting.value[cancelOrderKey(symbol, orderId)])
}
async function cancelOpenOrder(order) {
  if (!order || !order.id) return
  const key = cancelOrderKey(order.symbol, order.id)
  cancelOrderSubmitting.value = { ...cancelOrderSubmitting.value, [key]: true }
  try {
    const payload = {
      symbol: order.symbol,
      order_id: order.id,
    }
    if (order.client_order_id) payload.client_order_id = order.client_order_id
    const res = await api.command('CANCEL_OPEN_ORDER', JSON.stringify(payload))
    ElMessage.success(`${order.symbol} 撤销挂单命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`撤销挂单失败: ${e.message}`)
  } finally {
    const next = { ...cancelOrderSubmitting.value }
    delete next[key]
    cancelOrderSubmitting.value = next
  }
}
async function cancelConditionOrder(order) {
  if (!order || !order.id) return
  const key = cancelOrderKey(order.symbol, order.id)
  cancelOrderSubmitting.value = { ...cancelOrderSubmitting.value, [key]: true }
  try {
    const payload = { symbol: order.symbol, algo_id: order.id }
    if (order.client_algo_id) payload.client_algo_id = order.client_algo_id
    const res = await api.command('CANCEL_CONDITION_ORDER', JSON.stringify(payload))
    ElMessage.success(`${order.symbol} 撤销条件单命令已入队 (#${res.id})`)
  } catch (e) {
    ElMessage.error(`撤销条件单失败: ${e.message}`)
  } finally {
    const next = { ...cancelOrderSubmitting.value }
    delete next[key]
    cancelOrderSubmitting.value = next
  }
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
      <el-alert
        v-if="disabledWithPosition.size > 0"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
      >
        <template #title>
          检测到 {{ disabledWithPosition.size }} 个币种被禁用但仍有交易所持仓
          <span style="margin-left:8px">
            <el-button
              v-for="sym in [...disabledWithPosition]"
              :key="sym"
              size="small"
              type="success"
              :loading="enabling[sym]"
              @click="enableSymbol({ symbol: sym })"
            >
              启用 {{ sym }}
            </el-button>
          </span>
        </template>
      </el-alert>
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
        <el-table-column label="开仓时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.local_opened_at_ms) }}</template>
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
        <el-table-column label="操作" width="340" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons">
              <el-tag v-if="isSymbolDisabled(row.symbol)" type="warning" size="small"
                effect="dark" style="margin-right:6px">
                币种已禁用
              </el-tag>
              <template v-if="needsRepair(row)">
                <el-button
                  type="danger"
                  size="small"
                  :icon="'CirclePlus'"
                  :loading="isRepairing(row)"
                  @click="repairProtection(row)"
                >
                  历史补单
                </el-button>
                <el-button size="small" @click="openTakeover(row)">接管保护</el-button>
              </template>
              <el-button
                v-if="isSymbolDisabled(row.symbol)"
                size="small"
                type="success"
                :loading="enabling[row.symbol]"
                @click="enableSymbol(row)"
              >
                启用并自动接管
              </el-button>
              <el-tag v-else type="success" size="small">{{ missingProtectionText(row) }}</el-tag>
              <el-button type="danger" plain size="small" @click="openManualClose(row)">
                手动平仓
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="takeoverVisible" title="接管保护" width="520px">
      <el-form v-if="takeoverRow" label-width="120px">
        <el-form-item label="币种">
          <span class="mono">{{ takeoverRow.symbol }}</span>
        </el-form-item>
        <el-form-item label="方向/数量">
          <span class="mono">{{ takeoverRow.side }} / {{ fmt(takeoverRow.contracts) }}</span>
        </el-form-item>
        <el-form-item label="开仓价/标记价">
          <span class="mono">{{ fmt(takeoverRow.entry_price, 2) }} / {{ fmt(takeoverRow.mark_price, 2) }}</span>
        </el-form-item>
        <el-form-item label="接管数量">
          <el-input v-model="takeoverForm.qty" class="protect-input" />
        </el-form-item>
        <el-form-item label="止损触发价">
          <el-input v-model="takeoverForm.sl" class="protect-input" />
        </el-form-item>
        <el-form-item label="止盈触发价">
          <el-input v-model="takeoverForm.tp" class="protect-input" />
        </el-form-item>
        <el-form-item>
          <el-button size="small" @click="recomputeTakeover">按当前价重算</el-button>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="takeoverForm.confirm">
            确认用以上触发价接管当前交易所持仓
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="takeoverVisible = false">取消</el-button>
        <el-button type="danger" :loading="takeoverSubmitting" @click="submitTakeover">
          提交接管
        </el-button>
      </template>
    </el-dialog>

    <el-card shadow="never" class="open-orders-card">
      <template #header>
        <div class="positions-header">
          <span>挂单（交易所实时）</span>
          <span class="positions-meta">
            <el-tag :type="openOrdersError ? 'warning' : 'success'" size="small" effect="dark">
              {{ openOrdersError ? '同步失败' : '交易所实时' }}
            </el-tag>
            <span>同步于 {{ openOrdersSyncedText }}</span>
          </span>
        </div>
      </template>
      <el-alert
        v-if="openOrdersError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        :title="`交易所普通挂单同步失败：${openOrdersError}`"
      />
      <el-table
        :data="openOrders"
        stripe
        size="small"
        empty-text="当前无普通挂单"
      >
        <el-table-column prop="symbol" label="标的" width="110" />
        <el-table-column label="方向" width="70">
          <template #default="{ row }">
            <el-tag :type="row.side === 'buy' ? 'success' : 'danger'" size="small">
              {{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <span class="mono">{{ row.order_type || 'LIMIT' }}</span>
            <el-tag v-if="row.reduce_only" type="info" size="small" style="margin-left:4px">reduce-only</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="价格" width="120">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.price || 0).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="数量" width="110">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.qty || 0).toFixed(4) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已成交" width="110">
          <template #default="{ row }">
            <span class="mono">{{ Number(row.filled_qty || 0).toFixed(4) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.status || 'placed' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              type="danger"
              size="small"
              plain
              :loading="isCancelingOrder(row.symbol, row.id)"
              @click="cancelOpenOrder(row)"
            >
              撤销
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="condition-orders-section">
        <div class="section-title">条件单（SL/TP）</div>
        <el-table
          :data="live.summary?.condition_orders || []"
          stripe
          size="small"
          empty-text="当前无 SL/TP 条件单"
        >
          <el-table-column prop="symbol" label="标的" width="110" />
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.kind === 'SL' ? 'danger' : 'success'" size="small">
                {{ row.kind || '—' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="方向" width="70">
            <template #default="{ row }">
              <span class="mono">{{ row.side === 'buy' ? '买' : row.side === 'sell' ? '卖' : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="触发价" width="120">
            <template #default="{ row }">
              <span class="mono">{{ Number(row.trigger_price || row.price || 0).toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="数量" width="110">
            <template #default="{ row }">
              <span class="mono">{{ Number(row.qty || 0).toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small">{{ orderStatusLabel({ client_kind: row.kind, status: row.status || 'placed' }) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <el-button
                type="danger"
                size="small"
                plain
                :loading="isCancelingOrder(row.symbol, row.id)"
                @click="cancelConditionOrder(row)"
              >
                撤销
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-dialog v-model="closeVisible" title="手动平仓" width="520px">
      <el-form v-if="closeRow" label-width="120px">
        <el-form-item label="币种">
          <span class="mono">{{ closeRow.symbol }}</span>
        </el-form-item>
        <el-form-item label="方向/数量">
          <span class="mono">{{ closeRow.side }} / {{ fmt(closeRow.contracts) }}</span>
        </el-form-item>
        <el-form-item label="开仓价/标记价">
          <span class="mono">{{ fmt(closeRow.entry_price, 2) }} / {{ fmt(closeRow.mark_price, 2) }}</span>
        </el-form-item>
        <el-form-item label="杠杆">
          <span class="mono">{{ Number(closeRow.leverage) > 0 ? `${closeRow.leverage}x` : '—' }}</span>
        </el-form-item>
        <el-form-item label="预计盈亏">
          <span class="mono" :class="estimatedClosePnl(closeRow) >= 0 ? 'pnl-pos' : 'pnl-neg'">
            {{ fmt(estimatedClosePnl(closeRow), 2) }} USDT
          </span>
        </el-form-item>
        <el-form-item label="保护单处理">
          <span>平仓确认后撤销该币种关联止盈止损条件单</span>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="closeConfirm">
            确认强制市价 reduce-only 平掉当前交易所持仓（绕过滑点保护）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeVisible = false">取消</el-button>
        <el-button type="danger" :loading="closeSubmitting" @click="submitManualClose">
          强制市价平仓
        </el-button>
      </template>
    </el-dialog>
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

.action-buttons {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.protect-input {
  width: 180px;
}

.open-orders-card {
  margin-top: 16px;
}
.condition-orders-section {
  margin-top: 16px;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}

@media (max-width: 767px) {
  .positions-header,
  .positions-meta {
    align-items: flex-start;
  }

  .positions-meta,
  .action-buttons {
    flex-wrap: wrap;
  }

  .protect-input {
    width: 100%;
  }
}
</style>
