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
    <div class="login-grid-bg" aria-hidden="true"></div>
    <div class="login-orb login-orb-primary" aria-hidden="true"></div>
    <div class="login-orb login-orb-secondary" aria-hidden="true"></div>

    <section class="login-hero">
      <div class="login-brand">
        <div class="login-logo">GQ</div>
        <div>
          <h1>Genius Quant AI</h1>
          <p>AI-Driven Crypto Futures Trading System</p>
        </div>
      </div>
      <div class="login-copy">
        <div class="login-eyebrow">Large Model Quant Platform</div>
        <h2>大模型量化交易平台</h2>
        <p>融合 LLM、Private User Stream、Multi-Timeframe Signals、Dynamic Risk Control 与 Maker-first Execution。</p>
        <div class="tech-tags">
          <span>LLM Strategy Engine</span>
          <span>Binance Private Stream</span>
          <span>Multi-Timeframe Signals</span>
          <span>Dynamic Risk Control</span>
          <span>Maker-first Execution</span>
          <span>Real-time Position Guard</span>
        </div>
      </div>
    </section>

    <el-card class="login-card" shadow="always">
      <template #header>
        <div class="login-card-header">
          <span>进入交易中枢</span>
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
          进入控制台
        </el-button>
      </el-form>

      <el-alert
        class="login-note"
        type="info"
        :closable="false"
        show-icon
        title="HttpOnly Session Cookie · No token stored in frontend"
      />
      <div v-if="authState.checked && !authState.authenticated" class="login-status">
        当前未登录或登录已过期
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 420px;
  gap: 48px;
  align-items: center;
  overflow: hidden;
  padding: 48px clamp(20px, 7vw, 96px);
  background:
    linear-gradient(135deg, rgba(5, 10, 26, 0.98), rgba(13, 20, 45, 0.96) 48%, rgba(6, 11, 30, 0.99)),
    var(--bt-bg);
  color: #e5eefc;
}

.login-grid-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(96, 165, 250, 0.10) 1px, transparent 1px),
    linear-gradient(90deg, rgba(96, 165, 250, 0.10) 1px, transparent 1px);
  background-size: 42px 42px;
  mask-image: radial-gradient(circle at 35% 42%, black, transparent 72%);
  opacity: 0.45;
}

.login-orb {
  position: absolute;
  width: 520px;
  height: 520px;
  pointer-events: none;
  border-radius: 999px;
  filter: blur(16px);
  opacity: 0.45;
}

.login-orb-primary {
  left: -180px;
  top: -140px;
  background: radial-gradient(circle, rgba(59, 130, 246, 0.65), transparent 68%);
}

.login-orb-secondary {
  right: -150px;
  bottom: -180px;
  background: radial-gradient(circle, rgba(245, 158, 11, 0.42), transparent 70%);
}

.login-hero {
  position: relative;
  z-index: 1;
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
  border: 1px solid rgba(250, 204, 21, 0.42);
  border-radius: 20px;
  color: #fef3c7;
  background: linear-gradient(135deg, rgba(250, 204, 21, 0.20), rgba(59, 130, 246, 0.16));
  font-weight: 800;
  letter-spacing: 0.04em;
  box-shadow:
    0 0 40px rgba(250, 204, 21, 0.16),
    inset 0 1px 0 rgba(255, 255, 255, 0.22);
}

.login-brand h1,
.login-copy h2 {
  margin: 0;
}

.login-brand h1 {
  font-size: 32px;
  letter-spacing: -0.03em;
}

.login-brand p,
.login-copy p {
  margin: 8px 0 0;
  color: rgba(203, 213, 225, 0.78);
}

.login-eyebrow {
  display: inline-flex;
  align-items: center;
  margin-bottom: 16px;
  padding: 7px 12px;
  border: 1px solid rgba(96, 165, 250, 0.28);
  border-radius: 999px;
  color: #93c5fd;
  background: rgba(15, 23, 42, 0.55);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.login-copy h2 {
  max-width: 560px;
  font-size: clamp(38px, 6vw, 72px);
  line-height: 1.05;
  letter-spacing: -0.07em;
  background: linear-gradient(135deg, #f8fafc 12%, #93c5fd 52%, #facc15);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.login-copy p {
  max-width: 520px;
  font-size: 16px;
  line-height: 1.8;
}

.tech-tags {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 28px;
  max-width: 620px;
}

.tech-tags span {
  padding: 11px 13px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 14px;
  color: rgba(226, 232, 240, 0.92);
  background: linear-gradient(135deg, rgba(15, 23, 42, 0.72), rgba(30, 41, 59, 0.42));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
  font-size: 13px;
}

.login-card {
  position: relative;
  z-index: 1;
  width: 100%;
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 22px;
  background:
    linear-gradient(180deg, rgba(15, 23, 42, 0.84), rgba(15, 23, 42, 0.68)),
    rgba(15, 23, 42, 0.78);
  box-shadow:
    0 24px 80px rgba(0, 0, 0, 0.45),
    inset 0 1px 0 rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  --el-card-bg-color: transparent;
  --el-card-border-color: transparent;
  color: #e5eefc;
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
  border: 0;
  color: #07111f;
  background: linear-gradient(135deg, #93c5fd, #facc15);
  font-weight: 700;
  box-shadow: 0 16px 34px rgba(59, 130, 246, 0.25);
}

.login-note {
  margin-top: 18px;
}

.login-status {
  margin-top: 12px;
  font-size: 13px;
  color: rgba(203, 213, 225, 0.72);
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

  .tech-tags {
    grid-template-columns: 1fr;
  }
}
</style>
