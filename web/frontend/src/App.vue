<script setup>
import { onMounted, onUnmounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useLiveStore } from './stores/live'

const live = useLiveStore()
const route = useRoute()
const router = useRouter()

const menu = [
  { index: '/dashboard', title: '总览', icon: 'Odometer' },
  { index: '/chart', title: 'K线图', icon: 'TrendCharts' },
  { index: '/positions', title: '持仓', icon: 'Wallet' },
  { index: '/decisions', title: '决策日志', icon: 'Document' },
  { index: '/orders', title: '交易记录', icon: 'List' },
  { index: '/pnl', title: '盈亏统计', icon: 'Money' },
  { index: '/control', title: '操作面板', icon: 'SetUp' },
]

const lastUpdateText = computed(() =>
  live.lastUpdate ? live.lastUpdate.toLocaleTimeString() : '—'
)
const activePath = computed(() => route.path)
let resumeArmed = false

function onMenuSelect(index) {
  if (route.path !== index) router.push(index)
}

function pauseLiveTransports() {
  live.disconnect()
  window.dispatchEvent(new Event('binance-trade-pause-live'))
}

function disarmResumeOnInteraction() {
  if (!resumeArmed) return
  resumeArmed = false
  window.removeEventListener('pointerdown', resumeLiveAfterInteraction, true)
  window.removeEventListener('keydown', resumeLiveAfterInteraction, true)
}

function resumeLiveAfterInteraction() {
  disarmResumeOnInteraction()
  live.connect()
  window.dispatchEvent(new Event('binance-trade-resume-live'))
}

function armResumeOnInteraction() {
  if (resumeArmed) return
  resumeArmed = true
  window.addEventListener('pointerdown', resumeLiveAfterInteraction, true)
  window.addEventListener('keydown', resumeLiveAfterInteraction, true)
}

function onWindowBlur() {
  pauseLiveTransports()
  armResumeOnInteraction()
}

onMounted(() => {
  live.connect()
  window.addEventListener('blur', onWindowBlur)
  window.addEventListener('pagehide', pauseLiveTransports)
})

onUnmounted(() => {
  disarmResumeOnInteraction()
  window.removeEventListener('blur', onWindowBlur)
  window.removeEventListener('pagehide', pauseLiveTransports)
  live.disconnect()
})
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" style="background:#1f2329; color:#fff">
      <div style="padding:18px 16px; font-size:18px; font-weight:600; color:#fff">
        binance-trade
      </div>
      <el-menu :default-active="activePath" background-color="#1f2329"
               text-color="#cfd3dc" active-text-color="#ffd04b" @select="onMenuSelect">
        <el-menu-item v-for="m in menu" :key="m.index" :index="m.index">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header style="display:flex; align-items:center; justify-content:space-between;
                        background:#fff; border-bottom:1px solid #e5e7eb">
        <span style="font-size:18px; font-weight:600">{{ route.meta.title || '' }}</span>
        <span style="font-size:13px; color:#909399">
          <el-tag :type="live.connected ? 'success' : 'danger'" size="small" effect="dark">
            {{ live.connected ? (live.transport === 'ws' ? '实时(WS)' : '实时(轮询)') : '已断开' }}
          </el-tag>
          <span style="margin-left:12px">更新于 {{ lastUpdateText }}</span>
        </span>
      </el-header>

      <el-main style="background:#f5f7fa">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
