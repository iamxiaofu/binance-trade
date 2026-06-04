<script setup>
import { computed, ref, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useLiveStore } from '../stores/live'

const live = useLiveStore()
const cfg = ref(null)
const commands = ref([])
const loading = ref(false)
const symbolLoading = ref({})

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
    ElMessage.success(`命令已入队 (#${r.id})，交易进程将在下个周期执行`)
    await Promise.all([loadCommands(), loadCfg()])
  } catch (e) {
    ElMessage.error(`下发失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function statusTag(s) {
  return { pending: 'warning', done: 'success', failed: 'danger' }[s] || 'info'
}

function commandLabel(name) {
  return {
    PAUSE: '暂停策略',
    RESUME: '恢复策略',
    SET_DRY_RUN: '切换下单模式',
    SET_SYMBOL_ENABLED: '切换币种交易',
    REPAIR_SL_TP: '补止盈止损',
    CANCEL_AND_FLATTEN: '撤单+平仓',
    STOP_ENGINE: '停止交易引擎',
    KILL_SWITCH: 'Kill Switch',
  }[name] || name
}

const symbolRows = computed(() => (cfg.value?.symbols || []).map((symbol) => ({
  symbol,
  enabled: cfg.value?.symbol_enabled?.[symbol] !== false,
  hasPosition: (live.positions || []).some((p) =>
    p.symbol === symbol && Number(p.contracts || 0) > 0
  ),
})))

function setSymbolLoading(symbol, val) {
  const next = { ...symbolLoading.value }
  if (val) next[symbol] = true
  else delete next[symbol]
  symbolLoading.value = next
}

async function setSymbolEnabled(symbol, enabled) {
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
    ElMessage.success(`${symbol} ${enabled ? '启用' : '停用'}命令已入队 (#${r.id})`)
    await loadCommands()
  } catch (e) {
    ElMessage.error(`下发失败: ${e.message}`)
  } finally {
    setSymbolLoading(symbol, false)
  }
}

onMounted(refreshAll)
</script>

<template>
  <div class="page">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom:16px"
      title="操作说明"
      description="所有命令写入命令队列，由交易主进程每周期消费执行（最多一个周期延迟）。Web 不直接操作交易所。" />

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>策略状态</template>
          <el-space direction="vertical" :size="14" fill style="width:100%">
            <div style="display:flex; gap:12px">
              <el-button type="warning" :loading="loading" style="flex:1"
                         :icon="'VideoPause'" @click="send('PAUSE')">
                暂停策略
              </el-button>
              <el-button type="success" :loading="loading" style="flex:1"
                         :icon="'VideoPlay'" @click="send('RESUME')">
                恢复策略
              </el-button>
            </div>
          </el-space>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>下单模式</template>
          <div v-if="cfg" style="margin-bottom:12px">
            当前：
            <el-tag :type="cfg.dry_run ? 'info' : 'danger'" effect="dark">
              {{ cfg.dry_run ? 'DRY-RUN（模拟）' : '真实下单' }}
            </el-tag>
            <el-tag size="small" style="margin-left:8px">
              {{ cfg.dry_run_source === 'runtime' ? '运行时持久化' : '配置文件' }}
            </el-tag>
          </div>
          <div style="display:flex; gap:12px">
            <el-button :loading="loading" style="flex:1" @click="send('SET_DRY_RUN', 'true')">
              切到 DRY-RUN
            </el-button>
            <el-button type="danger" :loading="loading" style="flex:1"
                       @click="send('SET_DRY_RUN', 'false', 'LIVE')">
              切到真实下单
            </el-button>
          </div>
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
      <el-table :data="symbolRows" stripe>
        <el-table-column prop="symbol" label="币种" width="120" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '已启用' : '已停用' }}
            </el-tag>
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
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
              {{ row.enabled ? '允许' : '禁止' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="160">
          <template #default="{ row }">
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
              @click="setSymbolEnabled(row.symbol, true)"
            >
              启用交易
            </el-button>
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
        <el-table-column prop="created_at" label="入队时间" width="170" />
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
        <el-table-column prop="executed_at" label="执行时间" width="170" />
      </el-table>
    </el-card>
  </div>
</template>
