<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const loading = ref(false)
// 状态/数据都给出"乐观默认"，避免 onMounted/refresh 时 ref=null 导致黄条闪动。
// 后端首次响应回来后会整体覆盖；轮询用 Object.assign 局部更新，不会瞬时清空。
const status = ref({
  active: null,
  switching_supported: true,
  chain: [],
  engine: { active_name: '', active_version: 0, active_source: '', chain: '' },
})
const profiles = ref([])
const showDialog = ref(false)
const editing = ref(null)  // null = 新增；否则为原 profile
const form = ref(emptyForm())
const testResult = ref({})  // { [name]: { ok, latency_ms, error } }
const pollTimer = ref(null)
// 区分"从未加载过"与"已经加载过"，UI 空白骨架只在首次进入时显示
const firstLoaded = ref(false)

function emptyForm() {
  return {
    name: '',
    provider: 'anthropic',
    model: 'claude-opus-4-6',
    base_url: '',
    timeout: 60,
    max_tokens: 1024,
    max_retries: 2,
    priority: 100,
    fallback_enabled: false,
    api_key: '',
  }
}

const activeName = computed(() => status.value?.active?.name || '—')
const engineActiveName = computed(() => status.value?.engine?.active_name || '—')
const engineVersion = computed(() => status.value?.engine?.active_version ?? 0)
const engineSource = computed(() => status.value?.engine?.active_source || '—')
const engineChain = computed(() => status.value?.engine?.chain || '—')
const engineSynced = computed(
  () => engineActiveName.value !== '—' && engineActiveName.value === activeName.value,
)

async function refresh() {
  loading.value = true
  try {
    const [st, pr] = await Promise.all([api.llmStatus(), api.llmProfiles()])
    // 用整体赋值即可，ref 默认值已经给出"乐观默认"，不会瞬时变 null。
    status.value = st
    profiles.value = pr.items || []
    firstLoaded.value = true
  } catch (e) {
    ElMessage.error(`加载失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = emptyForm()
  showDialog.value = true
}
function openEdit(p) {
  editing.value = p
  form.value = {
    name: p.name,
    provider: p.provider,
    model: p.model,
    base_url: p.base_url || '',
    timeout: p.timeout,
    max_tokens: p.max_tokens,
    max_retries: p.max_retries,
    priority: p.priority ?? 100,
    fallback_enabled: !!p.fallback_enabled,
    api_key: '',  // 留空 = 不改
  }
  showDialog.value = true
}
async function submit() {
  if (!form.value.name.trim()) return ElMessage.warning('请填写 profile 名')
  if (!form.value.model.trim()) return ElMessage.warning('请填写 model')
  if (!editing.value && !form.value.api_key) {
    return ElMessage.warning('新建 profile 必须填写 API key')
  }
  loading.value = true
  try {
    if (editing.value) {
      await api.llmUpdate(editing.value.name, form.value)
      ElMessage.success('已更新')
    } else {
      await api.llmCreate(form.value)
      ElMessage.success('已创建')
    }
    showDialog.value = false
    await refresh()
  } catch (e) {
    ElMessage.error(`提交失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function activate(p) {
  try {
    const { value } = await ElMessageBox.prompt(
      `即将把 LLM 切换到 "${p.name}"。此操作会让交易进程热替换 LLMClient，请输入 "${p.name}" 确认`,
      '切换 LLM profile',
      { inputPlaceholder: p.name, confirmButtonText: '切换', cancelButtonText: '取消' },
    )
    if (value !== p.name) return ElMessage.warning('确认词不匹配，已取消')
  } catch (_) { return /* 用户取消 */ }
  loading.value = true
  try {
    await api.llmActivate(p.name)
    ElMessage.success(`已入队，engine 将热替换为 ${p.name}`)
    // 立刻刷新一次，然后让轮询继续
    setTimeout(refresh, 500)
  } catch (e) {
    ElMessage.error(`切换失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function testConn(p) {
  testResult.value = { ...testResult.value, [p.name]: { loading: true } }
  try {
    const r = await api.llmTest(p.name)
    testResult.value = {
      ...testResult.value,
      [p.name]: { ok: true, latency_ms: r.latency_ms },
    }
    ElMessage.success(`${p.name} 连通 (${r.latency_ms}ms)`)
  } catch (e) {
    testResult.value = {
      ...testResult.value,
      [p.name]: { ok: false, error: e.message },
    }
    ElMessage.error(`${p.name} 失败: ${e.message}`)
  }
}

async function remove(p) {
  if (p.is_active) {
    return ElMessage.warning('active profile 不能直接删除，请先切换到其他 profile')
  }
  try {
    const { value } = await ElMessageBox.prompt(
      `将删除 profile "${p.name}"，请输入 "${p.name}" 确认`,
      '删除 LLM profile',
      { inputPlaceholder: p.name, confirmButtonText: '删除', cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger' },
    )
    if (value !== p.name) return ElMessage.warning('确认词不匹配，已取消')
  } catch (_) { return }
  loading.value = true
  try {
    await api.llmDelete(p.name)
    ElMessage.success('已删除')
    await refresh()
  } catch (e) {
    ElMessage.error(`删除失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refresh()
  // 切换命令入队后 1.5s 起轮询 /api/llm/status，捕获 engine 热替换完成的 version 变化
  pollTimer.value = setInterval(() => {
    api.llmStatus().then((s) => {
      // 局部覆盖，不整体替换，避免 computed 中间态
      status.value = { ...status.value, ...s }
    }).catch(() => {})
  }, 2000)
})
onUnmounted(() => { if (pollTimer.value) clearInterval(pollTimer.value) })
</script>

<template>
  <div class="llm-page">
    <el-row :gutter="16" class="status-row">
      <el-col :span="8">
        <el-card shadow="never">
          <div class="stat-label">DB active</div>
          <div class="stat-value">{{ activeName }}</div>
          <div class="stat-hint">llm_profiles.is_active=true（主源/链头）</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <div class="stat-label">Engine 生效</div>
          <div class="stat-value">
            {{ engineActiveName }}
            <el-tag v-if="engineSynced" type="success" size="small" effect="plain">已同步</el-tag>
            <el-tag v-else-if="engineActiveName !== '—'" type="warning" size="small" effect="plain">热替换中</el-tag>
          </div>
          <div class="stat-hint">version={{ engineVersion }} · source={{ engineSource }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <div class="stat-label">Fallback 链</div>
          <div class="stat-value" style="font-size:14px">{{ engineChain }}</div>
          <div class="stat-hint">主源失败按 priority 升序兜底</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="table-card">
      <template #header>
        <div class="table-header">
          <span>LLM profiles</span>
          <el-button type="primary" @click="openCreate">
            新增 profile
          </el-button>
        </div>
      </template>

      <el-table :data="profiles" v-loading="loading" stripe>
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="provider" label="provider" width="140" />
        <el-table-column prop="model" label="model" min-width="160" />
        <el-table-column label="base_url" min-width="180">
          <template #default="{ row }">
            <span :title="row.base_url">{{ row.base_url || '(官方)' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="timeout" label="timeout" width="80" />
        <el-table-column prop="priority" label="priority" width="80" />
        <el-table-column label="备源" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.fallback_enabled" type="warning" size="small">是</el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="key" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.key_present" type="success" size="small">已存</el-tag>
            <el-tag v-else type="info" size="small">未设</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success">active</el-tag>
            <el-tag v-else type="info" effect="plain">inactive</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="280">
          <template #default="{ row }">
            <el-button
              size="small" :disabled="row.is_active"
              @click="activate(row)"
            >激活</el-button>
            <el-button
              size="small" :disabled="!row.key_present"
              :loading="testResult[row.name]?.loading"
              @click="testConn(row)"
            >测试</el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              size="small" type="danger" :disabled="row.is_active"
              @click="remove(row)"
            >删除</el-button>
            <span v-if="testResult[row.name]?.ok" class="test-ok">
              ✓ {{ testResult[row.name].latency_ms }}ms
            </span>
            <span v-else-if="testResult[row.name]?.ok === false" class="test-fail">
              ✗ {{ testResult[row.name].error }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="showDialog"
      :title="editing ? `编辑 ${editing.name}` : '新增 LLM profile'"
      width="520"
    >
      <el-form :model="form" label-width="100px">
        <el-form-item label="名称">
          <el-input v-model="form.name" :disabled="!!editing" placeholder="例如 default / official / ikuncode" />
        </el-form-item>
        <el-form-item label="provider">
          <el-select v-model="form.provider">
            <el-option label="anthropic (Messages + tool_use)" value="anthropic" />
            <el-option label="openai_compatible (chat/completions)" value="openai_compatible" />
          </el-select>
        </el-form-item>
        <el-form-item label="model">
          <el-input v-model="form.model" placeholder="claude-opus-4-6 / gpt-4o ..." />
        </el-form-item>
        <el-form-item label="base_url">
          <el-input
            v-model="form.base_url"
            :placeholder="form.provider === 'openai_compatible'
              ? '兼容网关地址，留空 = 官方 OpenAI'
              : '留空 = 官方 api.anthropic.com'"
          />
        </el-form-item>
        <el-form-item label="timeout (s)">
          <el-input-number v-model="form.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-form-item label="priority">
          <el-input-number v-model="form.priority" :min="0" :max="10000" />
          <div style="font-size:12px; color:var(--el-text-color-placeholder); margin-left:8px">
            升序优先；激活时主源自动置 0
          </div>
        </el-form-item>
        <el-form-item label="备源">
          <el-switch v-model="form.fallback_enabled" />
          <div style="font-size:12px; color:var(--el-text-color-placeholder); margin-left:8px">
            开启后并入 fallback 链，主源失败时按 priority 兜底
          </div>
        </el-form-item>
        <el-form-item label="max_tokens">
          <el-input-number v-model="form.max_tokens" :min="64" :max="512000" :step="64" />
          <div
            v-if="form.max_tokens > 8192"
            class="hint-warn"
            style="font-size:12px; color:#e6a23c; margin-top:4px"
          >
            ⚠ 已超过多数模型默认上限 8192；少数模型（如部分长上下文变体）支持更大值，请确认你的模型/中转实际支持
          </div>
        </el-form-item>
        <el-form-item label="max_retries">
          <el-input-number v-model="form.max_retries" :min="0" :max="5" />
        </el-form-item>
        <el-form-item :label="editing ? '新 API key' : 'API key'">
          <el-input
            v-model="form.api_key" type="password" show-password
            :placeholder="editing ? '留空 = 不修改现有 key' : 'sk-ant-...'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showDialog = false">取消</el-button>
        <el-button type="primary" :loading="loading" @click="submit">
          {{ editing ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.llm-page { display: flex; flex-direction: column; gap: 12px; }
.banner { margin-bottom: 4px; }
.status-row .stat-label { font-size: 12px; color: var(--el-text-color-secondary); }
.status-row .stat-value { font-size: 18px; font-weight: 600; margin-top: 4px; }
.status-row .stat-hint { font-size: 12px; color: var(--el-text-color-placeholder); margin-top: 2px; }
.table-card { margin-top: 4px; }
.table-header { display: flex; justify-content: space-between; align-items: center; }
.test-ok { color: #67c23a; font-size: 12px; margin-left: 8px; }
.test-fail { color: #f56c6c; font-size: 12px; margin-left: 8px; }
code { background: rgba(127,127,127,0.1); padding: 0 4px; border-radius: 3px; }

@media (max-width: 767px) {
  .table-header {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 8px;
  }
}
</style>
