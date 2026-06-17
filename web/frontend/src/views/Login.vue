<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authLogin, authState } from '../api'

const route = useRoute()
const router = useRouter()
const form = ref({
  username: '',
  password: '',
})
const loading = ref(false)
const redirect = computed(() => (
  typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/')
    ? route.query.redirect
    : '/dashboard'
))

async function submit() {
  if (!form.value.username || !form.value.password) {
    ElMessage.error('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await authLogin(form.value.username.trim(), form.value.password)
    ElMessage.success('登录成功')
    router.replace(redirect.value)
  } catch (e) {
    ElMessage.error(`登录失败：${e.message}`)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <section class="login-hero">
      <div class="login-brand">
        <div class="login-logo">BT</div>
        <div>
          <h1>Binance-trade</h1>
          <p>合约交易控制台</p>
        </div>
      </div>
      <div class="login-copy">
        <h2>登录后管理 testnet 与 mainnet</h2>
        <p>使用统一账号登录，系统会同时为两个环境建立安全会话；主网高风险操作仍保留 MAINNET 二次确认。</p>
      </div>
    </section>

    <el-card class="login-card" shadow="always">
      <template #header>
        <div class="login-card-header">
          <span>账户登录</span>
          <el-tag type="warning" effect="dark">SESSION</el-tag>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            autocomplete="username"
            placeholder="请输入用户名"
            size="large"
            :disabled="loading"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            size="large"
            show-password
            :disabled="loading"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          class="login-submit"
          :loading="loading"
          @click="submit"
        >
          登录控制台
        </el-button>
      </el-form>

      <el-alert
        class="login-note"
        type="info"
        :closable="false"
        show-icon
        title="登录状态由 HttpOnly Cookie 保存，前端不会保存密码或 token。"
      />
      <div v-if="authState.checked && !authState.authenticated" class="login-status">
        当前未登录或登录已过期
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 420px;
  gap: 48px;
  align-items: center;
  padding: 48px clamp(20px, 7vw, 96px);
  background:
    radial-gradient(circle at 12% 20%, rgba(64, 158, 255, 0.20), transparent 30%),
    radial-gradient(circle at 82% 78%, rgba(245, 108, 108, 0.18), transparent 28%),
    var(--bt-bg);
  color: var(--bt-text);
}

.login-hero {
  max-width: 620px;
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 18px;
  margin-bottom: 64px;
}

.login-logo {
  width: 58px;
  height: 58px;
  display: grid;
  place-items: center;
  border-radius: 18px;
  color: #111827;
  background: linear-gradient(135deg, #facc15, #f59e0b);
  font-weight: 800;
  letter-spacing: 0.04em;
  box-shadow: 0 18px 45px rgba(245, 158, 11, 0.30);
}

.login-brand h1,
.login-copy h2 {
  margin: 0;
}

.login-brand h1 {
  font-size: 32px;
}

.login-brand p,
.login-copy p {
  margin: 8px 0 0;
  color: var(--bt-muted);
}

.login-copy h2 {
  max-width: 560px;
  font-size: clamp(30px, 5vw, 54px);
  line-height: 1.05;
}

.login-copy p {
  max-width: 520px;
  font-size: 16px;
  line-height: 1.8;
}

.login-card {
  width: 100%;
  border-color: color-mix(in srgb, var(--bt-border) 70%, transparent);
  background: color-mix(in srgb, var(--bt-card) 92%, transparent);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}

.login-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 18px;
  font-weight: 650;
}

.login-submit {
  width: 100%;
  margin-top: 4px;
}

.login-note {
  margin-top: 18px;
}

.login-status {
  margin-top: 12px;
  font-size: 13px;
  color: var(--bt-muted);
}

@media (max-width: 860px) {
  .login-page {
    grid-template-columns: 1fr;
    gap: 24px;
    padding: calc(24px + env(safe-area-inset-top)) 16px 24px;
  }

  .login-brand {
    margin-bottom: 28px;
  }

  .login-copy h2 {
    font-size: 30px;
  }
}
</style>
