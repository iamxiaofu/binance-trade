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
const promptStatus = ref({
  active: null,
  versions: [],
  engine: { version: 0, name: '', source: '' },
  effective_system_prompt: '',
})
const promptForm = ref({
  name: '',
  content: '',
  render_mode: 'full_template',
  system_prompt_template: '',
  user_prompt_template: '',
  notes: '',
})
const promptPreview = ref('')
const promptUserPreview = ref('')
const promptPreviewWarnings = ref([])
const promptPreviewContext = ref('')
const promptLoading = ref(false)
const promptDirty = ref(false)
const promptSelectedId = ref(null)
const promptValidateSymbols = ref(['BTCUSDT'])
const promptValidateResults = ref([])
const promptContentDialog = ref(false)
const promptViewingVersion = ref(null)
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
const promptActive = computed(() => promptStatus.value?.active || null)
const promptEngine = computed(() => promptStatus.value?.engine || { version: 0, name: '', source: '' })
const promptVersions = computed(() => promptStatus.value?.versions || [])
const promptSynced = computed(() => {
  const activeVersion = Number(promptActive.value?.version || 0)
  return Number(promptEngine.value.version || 0) === activeVersion
})

function hydratePromptForm(prompt, { force = false } = {}) {
  if (promptDirty.value && !force) return
  promptForm.value = {
    name: prompt?.name || '',
    content: prompt?.content || '',
    render_mode: prompt?.render_mode || 'full_template',
    system_prompt_template: prompt?.system_prompt_template || promptStatus.value?.default_system_prompt_template || '',
    user_prompt_template: prompt?.user_prompt_template || promptStatus.value?.default_user_prompt_template || '',
    notes: prompt?.notes || '',
  }
  promptSelectedId.value = prompt?.id || null
  promptDirty.value = false
}

function markPromptDirty() {
  promptDirty.value = true
}

async function refresh() {
  loading.value = true
  try {
    const [st, pr, prompt] = await Promise.all([api.llmStatus(), api.llmProfiles(), api.llmPrompt()])
    // 用整体赋值即可，ref 默认值已经给出"乐观默认"，不会瞬时变 null。
    status.value = st
    profiles.value = pr.items || []
    promptStatus.value = prompt
    hydratePromptForm(prompt.active)
    if (!promptDirty.value) promptPreview.value = prompt.effective_system_prompt || ''
    if (!promptDirty.value) promptUserPreview.value = ''
    firstLoaded.value = true
  } catch (e) {
    ElMessage.error(`加载失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function promptPayload(extra = {}) {
  return {
    name: promptForm.value.name || '',
    content: promptForm.value.content || '',
    render_mode: promptForm.value.render_mode || 'legacy_append',
    system_prompt_template: promptForm.value.system_prompt_template || '',
    user_prompt_template: promptForm.value.user_prompt_template || '',
    notes: promptForm.value.notes || '',
    symbol: promptValidateSymbols.value[0] || 'BTCUSDT',
    ...extra,
  }
}

function versionPayload(v, extra = {}) {
  return {
    content: v.content || '',
    render_mode: v.render_mode || 'legacy_append',
    system_prompt_template: v.system_prompt_template || '',
    user_prompt_template: v.user_prompt_template || '',
    symbol: promptValidateSymbols.value[0] || 'BTCUSDT',
    ...extra,
  }
}

async function previewPrompt() {
  promptLoading.value = true
  try {
    const r = await api.llmPromptPreview(promptPayload())
    promptPreview.value = r.effective_system_prompt || ''
    promptUserPreview.value = r.effective_user_prompt || ''
    promptPreviewWarnings.value = r.warnings || []
    promptPreviewContext.value = `${r.symbol || ''} · ${r.context_source || ''}`
  } catch (e) {
    ElMessage.error(`预览失败: ${e.message}`)
  } finally {
    promptLoading.value = false
  }
}

async function previewPromptVersion(v) {
  promptLoading.value = true
  try {
    const r = await api.llmPromptPreview(versionPayload(v))
    promptPreview.value = r.effective_system_prompt || ''
    promptUserPreview.value = r.effective_user_prompt || ''
    promptPreviewWarnings.value = r.warnings || []
    promptPreviewContext.value = `${r.symbol || ''} · ${r.context_source || ''}`
  } catch (e) {
    ElMessage.error(`预览失败: ${e.message}`)
  } finally {
    promptLoading.value = false
  }
}

async function loadPromptVersion(v) {
  if (promptDirty.value) {
    try {
      await ElMessageBox.confirm(
        '当前编辑区有未保存内容，加载历史版本会覆盖编辑区但不会删除任何已保存版本。',
        '加载 Prompt 版本',
        { confirmButtonText: '加载', cancelButtonText: '取消', type: 'warning' },
      )
    } catch (_) { return }
  }
  hydratePromptForm(v, { force: true })
  await previewPromptVersion(v)
}

function resetPromptEditor() {
  hydratePromptForm(promptActive.value, { force: true })
  promptPreview.value = promptStatus.value?.effective_system_prompt || ''
  promptUserPreview.value = ''
  promptPreviewWarnings.value = []
  promptValidateResults.value = []
}

async function applyPrompt() {
  try {
    await ElMessageBox.confirm(
      'Prompt 附加指令会影响后续 LLM 交易决策。保存会创建新版本并激活；旧版本保留，可从历史版本回切。',
      '应用 Prompt',
      { confirmButtonText: '保存为新版本并热加载', cancelButtonText: '取消', type: 'warning' },
    )
  } catch (_) { return }
  promptLoading.value = true
  try {
    await api.llmPromptApply(promptPayload())
    ElMessage.success('Prompt 已保存，engine 将热加载')
    promptDirty.value = false
    setTimeout(refresh, 500)
  } catch (e) {
    ElMessage.error(`应用失败: ${e.message}`)
  } finally {
    promptLoading.value = false
  }
}

async function validatePromptWithLLM() {
  if (!promptValidateSymbols.value.length) {
    return ElMessage.warning('请至少选择一个币种')
  }
  promptLoading.value = true
  promptValidateResults.value = []
  try {
    const r = await api.llmPromptValidate(promptPayload({ symbols: promptValidateSymbols.value }))
    promptValidateResults.value = r.results || []
    const failed = promptValidateResults.value.filter((x) => !x.ok).length
    if (failed) {
      ElMessage.warning(`LLM 校验完成，${failed} 个币种未通过`)
    } else {
      ElMessage.success('LLM 校验通过；未写入决策、未下单')
    }
  } catch (e) {
    ElMessage.error(`LLM 校验失败: ${e.message}`)
  } finally {
    promptLoading.value = false
  }
}

function viewPromptVersion(v) {
  promptViewingVersion.value = v
  promptContentDialog.value = true
}

async function activatePromptVersion(v) {
  try {
    const { value } = await ElMessageBox.prompt(
      `即将回切到 Prompt v${v.version}「${v.name || '未命名'}」。此操作会影响后续 LLM 交易决策，请输入 "v${v.version}" 确认。`,
      '回切 Prompt 版本',
      { inputPlaceholder: `v${v.version}`, confirmButtonText: '回切并热加载', cancelButtonText: '取消', type: 'warning' },
    )
    if (value !== `v${v.version}`) return ElMessage.warning('确认词不匹配，已取消')
  } catch (_) { return }
  promptLoading.value = true
  try {
    await api.llmPromptActivate(v)
    ElMessage.success(`已回切到 Prompt v${v.version}，engine 将热加载`)
    promptDirty.value = false
    setTimeout(refresh, 500)
  } catch (e) {
    ElMessage.error(`回切失败: ${e.message}`)
  } finally {
    promptLoading.value = false
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
    Promise.all([api.llmStatus(), api.llmPrompt()]).then(([s, prompt]) => {
      // 局部覆盖，不整体替换，避免 computed 中间态
      status.value = { ...status.value, ...s }
      promptStatus.value = { ...promptStatus.value, ...prompt }
      if (!promptDirty.value) promptPreview.value = prompt.effective_system_prompt || promptPreview.value
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

    <el-card shadow="never" class="prompt-card">
      <template #header>
        <div class="table-header">
          <span>Prompt 控制</span>
          <div class="prompt-status">
            <el-tag :type="promptSynced ? 'success' : 'warning'" size="small" effect="plain">
              {{ promptSynced ? 'engine 已同步' : 'engine 热加载中' }}
            </el-tag>
            <span>DB v{{ promptActive?.version || 0 }} {{ promptActive?.name || '默认空附加指令' }}</span>
            <span>Engine v{{ promptEngine.version || 0 }} {{ promptEngine.name || '默认空附加指令' }}</span>
          </div>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom:12px"
        title="支持完整 System Prompt 与 User Prompt 模板在线编辑。User Prompt 使用白名单占位符渲染动态行情字段；保存会创建新版本，旧版本可回切。"
      />
      <el-form label-width="110px">
        <el-form-item label="版本名称">
          <el-input
            v-model="promptForm.name"
            placeholder="例如 趋势优先 / 震荡少交易 / 保守日内"
            maxlength="80"
            show-word-limit
            @input="markPromptDirty"
          />
        </el-form-item>
        <el-form-item label="渲染模式">
          <el-radio-group v-model="promptForm.render_mode" @change="markPromptDirty">
            <el-radio-button label="full_template">完整模板</el-radio-button>
            <el-radio-button label="legacy_append">兼容附加指令</el-radio-button>
          </el-radio-group>
          <div class="prompt-editor-meta">
            完整模板可编辑 System/User 两段；兼容模式只把附加指令追加到代码默认 System Prompt。
          </div>
        </el-form-item>
        <template v-if="promptForm.render_mode === 'legacy_append'">
          <el-form-item label="附加指令">
            <el-input
              v-model="promptForm.content"
              type="textarea"
              :rows="8"
              maxlength="20000"
              show-word-limit
              placeholder="例如：震荡区间内降低开仓频率；只有多周期方向一致且成交量放大时才提高 confidence。"
              @input="markPromptDirty"
            />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="System模板">
            <el-input
              v-model="promptForm.system_prompt_template"
              type="textarea"
              :rows="12"
              maxlength="60000"
              show-word-limit
              placeholder="完整 System Prompt 模板"
              @input="markPromptDirty"
            />
          </el-form-item>
          <el-form-item label="User模板">
            <el-input
              v-model="promptForm.user_prompt_template"
              type="textarea"
              :rows="16"
              maxlength="60000"
              show-word-limit
              placeholder="可使用 {symbol}、{position_block}、{indicator_block}、{recent_klines_json} 等白名单占位符"
              @input="markPromptDirty"
            />
          </el-form-item>
        </template>
        <el-form-item label="版本备注">
          <el-input
            v-model="promptForm.notes"
            type="textarea"
            :rows="3"
            maxlength="20000"
            show-word-limit
            placeholder="记录本版本调整意图、适用行情和回切判断。"
            @input="markPromptDirty"
          />
          <div class="prompt-editor-meta">
            编辑来源：{{ promptSelectedId ? `版本 ID ${promptSelectedId}` : '代码默认模板草稿' }}
            <el-tag v-if="promptDirty" size="small" type="warning" effect="plain">未保存</el-tag>
          </div>
        </el-form-item>
        <el-form-item label="校验币种">
          <el-checkbox-group v-model="promptValidateSymbols">
            <el-checkbox-button label="BTCUSDT">BTC</el-checkbox-button>
            <el-checkbox-button label="ETHUSDT">ETH</el-checkbox-button>
            <el-checkbox-button label="SOLUSDT">SOL</el-checkbox-button>
            <el-checkbox-button label="BNBUSDT">BNB</el-checkbox-button>
          </el-checkbox-group>
          <div class="prompt-editor-meta">
            LLM 校验会真实请求当前 active LLM profile，只验证返回 schema；不写决策、不下单。
          </div>
        </el-form-item>
        <el-form-item>
          <el-button :loading="promptLoading" @click="previewPrompt">渲染预览</el-button>
          <el-button :loading="promptLoading" type="warning" plain @click="validatePromptWithLLM">发送 LLM 校验</el-button>
          <el-button type="primary" :loading="promptLoading" @click="applyPrompt">保存为新版本并热加载</el-button>
          <el-button :disabled="!promptDirty" @click="resetPromptEditor">重置为当前 active</el-button>
        </el-form-item>
      </el-form>
      <el-alert
        v-if="promptPreviewWarnings.length"
        type="warning"
        :closable="false"
        show-icon
        class="prompt-warning"
        :title="`模板预览警告：${promptPreviewWarnings.join('；')}`"
      />
      <el-collapse v-if="promptPreview || promptUserPreview">
        <el-collapse-item :title="`最终 System Prompt 预览 ${promptPreviewContext ? '(' + promptPreviewContext + ')' : ''}`">
          <pre class="prompt-preview">{{ promptPreview }}</pre>
        </el-collapse-item>
        <el-collapse-item title="最终 User Prompt 预览">
          <pre class="prompt-preview">{{ promptUserPreview }}</pre>
        </el-collapse-item>
      </el-collapse>
      <el-table v-if="promptValidateResults.length" :data="promptValidateResults" stripe class="prompt-validation-table">
        <el-table-column prop="symbol" label="币种" width="100" />
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="耗时ms" width="90" />
        <el-table-column prop="context_source" label="上下文" width="130" />
        <el-table-column label="返回/错误" min-width="260">
          <template #default="{ row }">
            <span v-if="row.ok">{{ row.decision?.action }} · confidence={{ row.decision?.confidence }}</span>
            <span v-else class="test-fail">{{ row.error }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-divider content-position="left">历史版本</el-divider>
      <el-table :data="promptVersions" v-loading="promptLoading" stripe class="prompt-version-table">
        <el-table-column prop="version" label="版本" width="80">
          <template #default="{ row }">v{{ row.version }}</template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="140">
          <template #default="{ row }">{{ row.name || '未命名' }}</template>
        </el-table-column>
        <el-table-column label="模式" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.render_mode === 'full_template' ? '完整模板' : '附加指令' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="150">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small">DB active</el-tag>
            <el-tag
              v-if="Number(row.version) === Number(promptEngine.version || 0)"
              type="primary"
              size="small"
              effect="plain"
              class="tag-gap"
            >engine</el-tag>
            <span v-if="!row.is_active && Number(row.version) !== Number(promptEngine.version || 0)">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" min-width="110" />
        <el-table-column prop="updated_at" label="更新时间 UTC" min-width="160" />
        <el-table-column label="内容摘要" min-width="220">
          <template #default="{ row }">
            <span class="prompt-snippet">
              {{ row.render_mode === 'full_template'
                ? (row.notes || row.system_prompt_template || row.user_prompt_template || '完整模板')
                : (row.content || '空附加指令') }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="320">
          <template #default="{ row }">
            <el-button size="small" @click="viewPromptVersion(row)">查看内容</el-button>
            <el-button size="small" @click="loadPromptVersion(row)">加载到编辑器</el-button>
            <el-button size="small" @click="previewPromptVersion(row)">预览</el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="row.is_active"
              @click="activatePromptVersion(row)"
            >回切并热加载</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="promptContentDialog"
      :title="promptViewingVersion ? `Prompt v${promptViewingVersion.version} 内容` : 'Prompt 内容'"
      width="80%"
    >
      <el-tabs v-if="promptViewingVersion">
        <el-tab-pane label="System Template">
          <pre class="prompt-preview">{{ promptViewingVersion.system_prompt_template || '(兼容模式：使用代码默认 System + 附加指令)' }}</pre>
        </el-tab-pane>
        <el-tab-pane label="User Template">
          <pre class="prompt-preview">{{ promptViewingVersion.user_prompt_template || '(兼容模式：使用代码默认 User Prompt)' }}</pre>
        </el-tab-pane>
        <el-tab-pane label="Legacy Addendum">
          <pre class="prompt-preview">{{ promptViewingVersion.content || '(空)' }}</pre>
        </el-tab-pane>
        <el-tab-pane label="Notes">
          <pre class="prompt-preview">{{ promptViewingVersion.notes || '(空)' }}</pre>
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="promptContentDialog = false">关闭</el-button>
      </template>
    </el-dialog>

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
.prompt-card { margin-top: 4px; }
.prompt-status {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.prompt-preview {
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.prompt-editor-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.prompt-version-table { margin-top: 4px; }
.prompt-validation-table { margin: 10px 0; }
.prompt-warning { margin-bottom: 10px; }
.prompt-snippet {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.tag-gap { margin-left: 6px; }
.test-ok { color: #67c23a; font-size: 12px; margin-left: 8px; }
.test-fail { color: #f56c6c; font-size: 12px; margin-left: 8px; }
code { background: rgba(127,127,127,0.1); padding: 0 4px; border-radius: 3px; }

@media (max-width: 767px) {
  .table-header {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 8px;
  }
  .prompt-version-table {
    overflow-x: auto;
  }
  .prompt-validation-table {
    overflow-x: auto;
  }
}
</style>
