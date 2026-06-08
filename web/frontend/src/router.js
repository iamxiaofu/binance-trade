import { createRouter, createWebHashHistory } from 'vue-router'

// 用 hash 路由：纯静态托管下无需服务端 rewrite，刷新不 404。
const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue'), meta: { title: '总览' } },
  { path: '/chart', name: 'chart', component: () => import('./views/Chart.vue'), meta: { title: 'K线图' } },
  { path: '/positions', name: 'positions', component: () => import('./views/Positions.vue'), meta: { title: '持仓' } },
  { path: '/decisions', name: 'decisions', component: () => import('./views/Decisions.vue'), meta: { title: '决策日志' } },
  { path: '/orders', name: 'orders', component: () => import('./views/Orders.vue'), meta: { title: '交易记录' } },
  { path: '/pnl', name: 'pnl', component: () => import('./views/Pnl.vue'), meta: { title: '盈亏统计' } },
  { path: '/control', name: 'control', component: () => import('./views/Control.vue'), meta: { title: '操作面板' } },
  { path: '/llm', name: 'llm', component: () => import('./views/LLM.vue'), meta: { title: 'LLM 配置' } },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
