# 2026-06-15 iOS 手机端看板响应式兼容

## 背景

原前端只针对桌面端布局：

- 固定 `200px` 左侧导航栏。
- 顶部状态栏强制单行展示。
- 总览、盈亏和控制面板大量使用固定 2/4 列栅格。
- 筛选器、弹窗和表单使用桌面固定宽度。
- 页面使用 `100vh`，在 iOS Safari 动态地址栏和底部 Home Indicator 下可能被截断。

这些问题不会影响 API 或交易引擎，但会导致 iPhone 竖屏下内容区域过窄、筛选器溢出、
弹窗超出视口以及底部内容不可点击。

## 设计

### 导航与页面骨架

- 桌面端继续使用左侧导航。
- `max-width: 767px` 时隐藏侧栏，改为底部可横向滚动导航。
- 底部导航使用 `env(safe-area-inset-bottom)`，避开 iPhone Home Indicator。
- 顶部状态栏允许换行，手机端隐藏低优先级的更新时间文本。

### iOS Safari 视口

- viewport 增加 `viewport-fit=cover`。
- 页面高度同时声明 `100vh` 与 `100dvh`，兼容旧浏览器并适应 Safari 动态地址栏。
- 顶部和底部使用 `safe-area-inset-top/bottom`。
- 输入框在手机端使用 `16px` 字体，避免 iOS 聚焦时自动放大页面。

### 内容响应式

- Element Plus 的 `6/8/12/16` 栅格列在手机端统一转为单列。
- 卡片标题、工具栏、操作按钮允许换行。
- 桌面固定宽度的 select、日期范围和数字输入在手机端改为 `100%`。
- 固定视口高度的日志和订单表格改用动态视口高度，并保留横向触摸滚动。
- 弹窗改为接近全屏宽度，限制动态视口高度，内容区独立滚动。
- 描述列表、Tabs、分页和长文本允许横向滚动或自动断行。

## 改动文件

| 文件 | 改动 |
|---|---|
| `web/frontend/index.html` | iOS viewport 与 Web App 元信息 |
| `web/frontend/src/App.vue` | 手机底部导航、头部响应式 class |
| `web/frontend/src/style.css` | 全局移动端断点、安全区、动态视口、表格和弹窗兼容 |
| `views/Chart.vue` | 行情工具栏移动端布局 |
| `views/Control.vue` | 币种新增区移动端单列 |
| `views/Decisions.vue` | 筛选器与详情弹窗移动端布局 |
| `views/LLM.vue` | Profile 表头移动端换行 |
| `views/Orders.vue` | 筛选区移动端单列 |
| `views/Positions.vue` | 持仓状态与操作区移动端换行 |

## 兼容范围

- iPhone Safari 竖屏与横屏
- iOS 添加到主屏幕后的安全区
- Android Chrome 窄屏
- 桌面端现有布局保持不变

复杂数据表格不会在手机端隐藏交易字段，而是保留横向滚动，避免因响应式精简而遗漏风控、
持仓或订单信息。

## 验证

- `npm run build`：通过
- `pytest -q`：`339 passed`
- `git diff --check`：通过
- 部署后验证 testnet/mainnet 静态前端和 API 路由

服务器未安装 Chromium/Playwright，无法自动生成 iOS 设备截图。最终视觉验收应使用真实 iPhone
Safari 检查八个页面、环境切换、底部导航、LLM 弹窗、决策详情弹窗和主网确认框。
