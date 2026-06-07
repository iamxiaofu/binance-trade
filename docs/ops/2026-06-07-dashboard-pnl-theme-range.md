# 2026-06-07 看板时间范围、盈亏统计与主题切换

## 背景

Web 看板原有展示存在几个体验问题：

- 总览页权益曲线只能按最近固定条数查询，不能选择近 1 小时、3 小时、12 小时、1 天、1 周、1 月。
- 盈亏统计页只展示当日已实现盈亏、累计平仓笔数和交易标的数，不支持时间范围筛选。
- 未平仓持仓的未实现盈亏没有进入盈亏统计页。
- 页面只有明亮模式，夜间查看和长时间盯盘不够舒适。
- 浏览器标签页标题仍是 Vite 默认的 `frontend`。
- 左侧导航品牌显示为 `binance-trade`，大小写不符合当前命名要求。

## 设计目标

- 权益曲线支持快捷时间范围筛选：
  - 近 1 小时
  - 近 3 小时
  - 近 12 小时
  - 近 1 天
  - 近 1 周
  - 近 1 月
- 盈亏统计支持同一套快捷范围筛选。
- 盈亏统计展示：
  - 当日已实现盈亏
  - 当日未实现盈亏
  - 范围已实现盈亏
  - 范围交易数
  - 范围平仓笔数
  - 各币种平仓笔数
- 前端支持明亮/暗黑模式切换，并记住用户选择。
- 浏览器标题改为 `Binance-Trade`。
- 左侧品牌改为 `Binance-trade`。
- 不新增数据库表，不改变交易执行逻辑。

## 后端接口

### `/api/equity`

新增查询参数：

```text
range=1h|3h|12h|1d|7d|30d
start_ts_ms=<ms>
end_ts_ms=<ms>
limit=<1..2000>
```

行为：

- 指定 `range` 时，后端按当前时间向前推算起点。
- 指定 `start_ts_ms` 时，优先使用自定义时间范围。
- 不传范围时保持旧行为，返回最近 `limit` 条余额快照。
- 指定范围后按 `balance_snapshots.ts_ms` 升序查询。
- 范围内数据超过 `limit` 时做等距采样，避免前端一次渲染过多点。

### `/api/pnl`

新增查询参数：

```text
range=1h|3h|12h|1d|7d|30d
start_ts_ms=<ms>
end_ts_ms=<ms>
```

返回新增字段：

- `day_unrealized_pnl`
- `unrealized_source`
- `range_close_count`
- `range_trade_count`
- `range_realized_pnl`
- `range_fee`
- `range_net_realized_pnl`
- `trade_by_symbol`
- `range`

兼容字段保留：

- `day_realized_pnl`
- `close_count`
- `trade_count`
- `close_by_symbol`

## 统计口径

### 当日已实现盈亏

来源：

```text
balance_snapshots.day_realized_pnl
```

取最新余额快照。

### 当日未实现盈亏

优先来源：

```text
交易所实时持仓 unrealized_pnl 汇总
```

失败回退：

```text
position_snapshots 最新非零持仓 unrealized_pnl 汇总
```

返回 `unrealized_source`：

- `exchange`
- `db_snapshot`

### 范围平仓笔数

来源：

```text
orders
```

筛选条件：

```sql
client_kind IN ('CLOSE', 'SL', 'TP')
status IN ('filled', 'partial')
ts_ms BETWEEN start AND end
```

### 范围交易数

来源：

```text
trades
```

筛选条件：

```sql
opened_at_ms BETWEEN start AND end
```

### 范围已实现盈亏

来源：

```text
orders.realized_pnl
```

净值：

```text
range_net_realized_pnl = range_realized_pnl - range_fee
```

注意：

- 该统计是看板复盘视角。
- 当日已实现盈亏仍以运行态余额快照为准。
- 精确交易所资金流水对账后续可单独接入 income/userTrades。

## 前端行为

### 总览页

权益曲线卡片右侧新增快捷范围按钮：

```text
1小时 / 3小时 / 12小时 / 1天 / 1周 / 1月
```

默认：

```text
12小时
```

切换后重新请求：

```text
GET /api/equity?range=<range>&limit=800
```

### 盈亏统计页

顶部卡片调整为：

- 当日已实现盈亏
- 当日未实现盈亏
- 范围已实现盈亏
- 范围交易数 / 平仓笔数

柱状图展示：

```text
各币种平仓笔数
```

筛选范围使用同一套快捷按钮。

### 暗黑模式

实现方式：

- 引入 Element Plus 暗黑变量：

```text
element-plus/theme-chalk/dark/css-vars.css
```

- 自定义布局使用 CSS variables：
  - `--bt-bg`
  - `--bt-card`
  - `--bt-header`
  - `--bt-sidebar`
  - `--bt-text`
  - `--bt-muted`
  - `--bt-border`
  - `--bt-primary`

- `html.dark` 控制暗黑模式变量。
- 主题选择写入：

```text
localStorage.binance-trade-theme
```

- 切换主题时广播：

```text
binance-trade-theme-change
```

权益曲线和盈亏柱状图收到事件后重新设置 ECharts 颜色。

## 品牌修正

浏览器标题：

```text
Binance-Trade
```

左侧导航品牌：

```text
Binance-trade
```

## 兼容性

- 不新增数据库表。
- 不修改交易执行逻辑。
- `/api/equity?limit=500` 旧调用继续有效。
- `/api/pnl` 不带参数继续有效。
- 旧前端字段 `close_count`、`close_by_symbol` 继续返回。
- 无持仓时 `day_unrealized_pnl=0`。
- 交易所实时查询失败时，未实现盈亏回退到 DB 快照。

## 涉及文件

- `web/status.py`
- `web/server.py`
- `web/frontend/src/api.js`
- `web/frontend/src/timeRanges.js`
- `web/frontend/src/views/Dashboard.vue`
- `web/frontend/src/views/Pnl.vue`
- `web/frontend/src/App.vue`
- `web/frontend/src/style.css`
- `web/frontend/src/main.js`
- `web/frontend/index.html`
- `tests/test_web_status.py`

## 验证

已验证：

```bash
.venv/bin/pytest tests/test_web_status.py tests/test_web_server_protection.py
```

结果：

```text
21 passed
```

后续发布前仍需执行：

```bash
.venv/bin/pytest
npm --prefix web/frontend run build
```
