<script setup>
import { onMounted, onUnmounted, computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useLiveStore } from './stores/live'
import { utc8Time } from './labels'
import { getEnvironment, setEnvironment } from './api'

const live = useLiveStore()
const route = useRoute()
const router = useRouter()
const theme = ref(localStorage.getItem('binance-trade-theme') || 'light')
const environment = ref(getEnvironment())

const menu = [
  { index: '/dashboard', title: '总览', icon: 'Odometer' },
  { index: '/chart', title: 'K线图', icon: 'TrendCharts' },
  { index: '/positions', title: '持仓', icon: 'Wallet' },
  { index: '/decisions', title: '决策日志', icon: 'Document' },
  { index: '/orders', title: '交易记录', icon: 'List' },
  { index: '/pnl', title: '盈亏统计', icon: 'Money' },
  { index: '/control', title: '操作面板', icon: 'SetUp' },
  { index: '/llm', title: 'LLM 配置', icon: 'Cpu' },
]

const lastUpdateText = computed(() =>
  live.lastUpdate ? utc8Time(live.lastUpdate.getTime()) : '—'
)
const activePath = computed(() => route.path)
const isDark = computed(() => theme.value === 'dark')

function onMenuSelect(index) {
  if (route.path !== index) router.push(index)
}

function pauseLiveTransports() {
  live.disconnect()
  window.dispatchEvent(new Event('binance-trade-pause-live'))
}

function resumeLiveTransports() {
  live.connect()
  window.dispatchEvent(new Event('binance-trade-resume-live'))
}

function onVisibilityChange() {
  if (document.hidden) {
    pauseLiveTransports()
  } else {
    resumeLiveTransports()
  }
}

function applyTheme(value) {
  theme.value = value
  document.documentElement.classList.toggle('dark', value === 'dark')
  document.documentElement.dataset.theme = value
  localStorage.setItem('binance-trade-theme', value)
  window.dispatchEvent(new Event('binance-trade-theme-change'))
}

function toggleTheme() {
  applyTheme(isDark.value ? 'light' : 'dark')
}

function switchEnvironment(value) {
  setEnvironment(value)
  live.disconnect()
  live.connect()
}

onMounted(() => {
  applyTheme(theme.value)
  live.connect()
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('pagehide', pauseLiveTransports)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('pagehide', pauseLiveTransports)
  live.disconnect()
})
</script>

<template>
  <el-container class="app-shell" :class="{ 'mainnet-shell': environment === 'mainnet' }">
    <el-aside width="200px" class="app-sidebar">
      <div class="brand-title">
        Binance-trade
      </div>
      <el-menu :default-active="activePath" class="side-menu" @select="onMenuSelect">
        <el-menu-item v-for="m in menu" :key="m.index" :index="m.index">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="app-header">
        <span style="font-size:18px; font-weight:600">{{ route.meta.title || '' }}</span>
        <div class="header-actions">
          <el-select v-model="environment" size="small" style="width:120px" @change="switchEnvironment">
            <el-option label="TESTNET" value="testnet" />
            <el-option label="MAINNET" value="mainnet" />
          </el-select>
          <el-tag :type="environment === 'mainnet' ? 'danger' : 'success'" effect="dark">
            {{ environment.toUpperCase() }}
          </el-tag>
          <el-button
            circle
            size="small"
            :icon="isDark ? 'Sunny' : 'Moon'"
            @click="toggleTheme"
          />
          <el-tag :type="live.connected ? 'success' : 'danger'" size="small" effect="dark">
            {{ live.connected ? (live.transport === 'ws' ? '实时(WS)' : '实时(轮询)') : '已断开' }}
          </el-tag>
          <span style="margin-left:12px">更新于 {{ lastUpdateText }}</span>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view :key="environment" />
      </el-main>
    </el-container>
  </el-container>
</template>
