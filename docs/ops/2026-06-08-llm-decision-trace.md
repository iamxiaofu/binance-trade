# 2026-06-08 LLM 决策请求追踪展示

## 背景

决策日志此前只保存 `context_json` 和解析后的结构化决策字段。

排查 LLM 判断时，只看 `context_json` 还不够直接：

- 无法确认实际发送给 LLM 的完整 Prompt。
- 无法查看 Anthropic tool-use 请求参数和工具 schema。
- 无法查看 LLM 原始回传，只能看到解析后的 `action/confidence/size_pct` 等字段。
- 页面详情只能看 JSON，不方便按字段理解 LLM 到底看到了哪些数据。

## 根因

旧链路中 `LLMClient.decide()` 在内部构建 `user_prompt` 并调用 Anthropic API，
但方法只返回解析后的 `TradeDecision`。

交易引擎落库时只能拿到：

- `MarketContext` 序列化后的 `context_json`
- 解析后的 `TradeDecision`

因此历史记录可以基于 `context_json` 重建 Prompt，但无法恢复真实原始 response。

## 改造内容

- LLM 客户端新增 `decide_with_trace()`：
  - 保持原 `decide()` 返回值兼容。
  - 返回结构化决策和一次调用 trace。
  - trace 不包含 API key。
- 决策日志表 `decisions` 追加审计字段：
  - `llm_prompt`
  - `llm_request_json`
  - `llm_response_json`
- 交易引擎实际调用 LLM 后，把 trace 随决策一起落库。
- 决策详情接口新增派生字段：
  - `llm_system_prompt`
  - `llm_user_prompt`
  - `llm_request_effective_json`
  - `llm_response_effective_json`
  - `llm_trace_available`
  - `llm_data_items`
- 决策详情弹窗新增标签页：
  - LLM 数据列表
  - Prompt
  - 完整请求 JSON
  - LLM 回传结果
  - Feature Snapshot
  - Context JSON
- Prompt K 线口径升级：
  - 保留 `5m × 20` 原始主周期 K 线。
  - 新增 `1m × 30` 原始微观 K 线。
  - `5m × 100` 主趋势和 `15m/1h/4h/1d × 60` 多周期数据仍只发送计算后的指标。
- LLM 触发层升级：
  - 从只比较 `last_decision_price` 升级为比较 `last_decision_snapshot`。
  - 新增 feature snapshot 持久化。
  - 支持指标状态 diff、防抖阈值和动态最长复查间隔。

## 数据口径

`context_json` 是系统内部保存的完整上下文快照。

真正发送给 LLM 的内容是 Prompt，其中：

- 主周期完整 K 线窗口用于本地计算指标。
- Prompt 中保留最近 20 根主周期原始 K 线。
- Prompt 中新增最近 30 根 1m 微观原始 K 线，用于观察入场节奏。
- 技术指标、趋势特征、成交量指标、多周期指标、持仓和账户风控字段会被展开写入 Prompt。

页面的“LLM 数据列表”按分类展示 Prompt 中的核心数据字段，便于审计。

## LLM 输入数据技术背景

当前发给 LLM 的不是“前端 K 线图画面”，而是交易引擎单独抓取行情后加工成
`MarketContext`，再渲染成 Prompt。

前端 K 线图的时间范围、指标和交互只服务于 Web 看板；LLM 决策链路不读取前端图表状态。

### K线周期与数量

| 用途 | 粒度 | 数量 | 覆盖时间 | 是否原文发给 LLM |
|---|---:|---:|---:|---|
| 主分析周期指标计算 | `5m` | 抓 `100` 根 | 约 8小时20分钟 | 否，只发送计算后的指标 |
| Prompt 原始主周期K线 | `5m` | `20` 根 | 约 1小时40分钟 | 是 |
| Prompt 原始微观K线 | `1m` | `30` 根 | 约 30分钟 | 是 |
| 多周期共振 | `15m` | `60` 根 | 约 15小时 | 否，只发送压缩指标 |
| 多周期共振 | `1h` | `60` 根 | 约 60小时 | 否，只发送压缩指标 |
| 大周期趋势 | `4h` | `60` 根 | 约 10天 | 否，只发送压缩指标 |
| 大周期趋势 | `1d` | `60` 根 | 约 2个月 | 否，只发送压缩指标 |

当前配置：

```yaml
llm:
  kline_lookback: 100
  kline_interval: 5m
  prompt_kline_count: 20
  micro_kline_interval: 1m
  micro_kline_lookback: 30
  higher_timeframes: [15m, 1h, 4h, 1d]
  indicators: [ema, rsi, macd, atr, bollinger, volume]
```

### 主周期指标

基于 `5m × 100` 根 K 线计算：

- EMA：`EMA(12)`、`EMA(26)`
- RSI：`RSI(14)`
- MACD：`macd`、`macd_signal`、`macd_hist`
- ATR：`ATR(14)`、`ATR%`
- Bollinger：中轨、上轨、下轨、`Boll%B`、带宽
- 成交量：最新成交量、20均量、量比、量比Δ3、20量Z-score
- 趋势结构：
  - `trend_direction`
  - `trend_score`
  - EMA价差
  - EMA价差 Δ3/Δ6/Δ12
  - 价格相对 EMA12/EMA26
  - 收益率 1/3/6/12 根
  - MACD柱 Δ3/Δ6
  - RSI Δ3/Δ6
  - ATR% Δ6
  - 最新K线振幅
  - 最新K线实体

### 多周期指标

对 `15m`、`1h`、`4h`、`1d` 各抓 60 根，只发压缩结果：

- EMA12
- EMA26
- RSI
- MACD
- MACD Signal
- 趋势：`up/down/flat`

### LLM 触发状态机

旧逻辑只保存 `last_decision_price`，触发条件主要是价格变化、持仓盈亏、订单事件和固定跳过次数。

新逻辑保留价格触发，但新增 `FeatureSnapshot`：

- `last_price` / `mark_price`
- `trend_direction` / `trend_score`
- `ema_spread_pct`
- `macd_hist`
- `rsi`
- `atr_pct`
- `boll_bandwidth_pct`
- `volume_ratio`
- `volume_zscore_20`
- `micro_return_5_pct`
- `micro_return_15_pct`
- `micro_range_5_pct`
- `micro_volume_zscore`
- `higher_trends`
- `higher_rsi`
- `higher_macd`
- BTC leader trend / micro return / volume z-score
- 持仓方向、未实现盈亏、杠杆

每次 LLM 实际决策后，系统保存当时的 `FeatureSnapshot`。
下一轮先构造 `current_feature_snapshot`，再比较：

```text
current_feature_snapshot vs last_decision_snapshot
```

满足以下任一状态变化时触发 LLM：

- EMA spread 符号翻转，且当前绝对值超过最小阈值。
- MACD hist 符号翻转。
- RSI 穿越中线 50。
- Bollinger 带宽从低位开始扩张。
- 成交量 z-score 首次达到放量阈值。
- 主周期趋势状态改变。
- 多周期趋势 map 改变。
- BTC leader 趋势、微观急拉急跌或放量状态变化。
- 1m 微观 5分钟收益率达到阈值。
- 1m 微观 5分钟振幅达到阈值。

为了避免震荡行情打爆 LLM 调用频率，触发层加入阈值和防抖：

```yaml
throttle:
  feature_snapshot_enabled: true
  ema_spread_cross_min_pct: 0.02
  macd_hist_cross_min_abs: 0.0
  rsi_midline: 50
  boll_bandwidth_low_pct: 1.0
  boll_bandwidth_expand_pct: 25
  volume_zscore_trigger: 2.0
  micro_return_5m_trigger_pct: 0.5
  micro_range_5m_trigger_pct: 0.8
```

### 动态复查间隔

旧配置 `max_skip_cycles=6` 等价于所有状态最长约 30 分钟复查一次。
新配置把最终兜底改为 12 次，并增加动态复查间隔：

```yaml
throttle:
  max_skip_cycles: 12
  near_exit_pnl_pct: 0.8
  review_flat_minutes: 60
  review_position_minutes: 15
  review_near_exit_minutes: 5
  review_high_vol_minutes: 5
```

复查策略：

- 无仓、低波动、无信号：最长约 60 分钟。
- 有仓、正常波动：最长约 15 分钟。
- 有仓且未实现盈亏接近预警阈值：最长约 5 分钟。
- 高波动、放量、1m 急拉急跌：最长约 5 分钟。
- 订单事件、关键指标翻转：立即触发。

`max_skip_cycles=12` 是最终兜底，不替代动态复查间隔。

### 引擎加工逻辑

引擎不会把交易所原始数据直接全量塞给 LLM，而是先做加工：

- 拉 `ticker`：最新价、标记价、24h涨跌幅。
- 拉 `funding_rate`：资金费率。
- 拉主周期 OHLCV：`5m × 100`。
- 用完整 100 根 K 线计算技术指标和趋势特征。
- 把最近 20 根 `5m` K 线原文写进 Prompt。
- 额外拉最近 30 根 `1m` K 线原文写进 Prompt。
- 拉 `15m/1h/4h/1d` 更高周期 K 线，压缩成多周期指标。
- 构造 `FeatureSnapshot`，用于决定本轮是否需要调用 LLM。
- 对非 BTC 币种注入 BTC leader snapshot，用于识别跨币种联动机会/风险。
- 读取当前持仓：方向、数量、开仓价、未实现盈亏、杠杆。
- 读取账户资金：账户权益、可用保证金。
- 计算风控约束：最大杠杆、单笔保证金上限、单笔理论止损亏损上限。

当前没有发给 LLM 的数据：

- 盘口深度/order book
- 买一卖一
- spread
- 买卖盘累计深度
- open interest
- 多空持仓比
- 恐惧贪婪指数

### 设计取舍

`5m × 20` 作为唯一原始 K 线输入时，对主趋势背景足够，但对最近入场节奏偏粗。
一根 5m K 线会隐藏内部路径，无法区分先拉升后回落、先下探后拉升或短线假突破。

但如果直接替换成 `1m × 20`，原始 K 线视野会从约 100 分钟缩到约 20 分钟，
LLM 更容易被短线噪音影响。

因此当前采用分层口径：

- 主趋势仍由 `5m × 100` 计算。
- Prompt 保留 `5m × 20` 原始主周期结构。
- Prompt 新增 `1m × 30` 微观窗口。
- 大周期趋势由 `4h/1d × 60` 计算后压缩发送。
- LLM 调用频率仍保持 5 分钟，不因 1m 微观窗口而提高调用频率。
- 触发层通过 snapshot diff 和动态复查间隔控制 LLM 调用频率。

这样能兼顾趋势背景、短线入场节奏、token 成本和后续扩展。

## 兼容性

SQLite 使用轻量迁移追加列，旧库启动时自动补列：

```sql
ALTER TABLE decisions ADD COLUMN llm_prompt TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN llm_request_json TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN llm_response_json TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN feature_snapshot_json TEXT NOT NULL DEFAULT '';
```

历史记录兼容策略：

- 如果已有 `context_json`，页面会基于当前 Prompt 模板重建 `llm_user_prompt`。
- 历史记录没有原始 `llm_response_json`，页面会提示“历史未记录原始响应”。
- 历史记录没有 `micro_klines` 时，微观K线列表为空。
- 历史记录没有 `feature_snapshot_json` 时，启动后该币种会按首次决策逻辑触发一次。
- 新产生的非跳过决策会保存真实 request/response trace。

## 涉及文件

- `src/llm/client.py`
- `src/llm/prompt.py`
- `src/llm/schema.py`
- `src/engine/loop.py`
- `src/features/builder.py`
- `src/config/schema.py`
- `src/throttle/feature_snapshot.py`
- `src/throttle/gate.py`
- `src/state/runtime.py`
- `src/store/models.py`
- `src/store/repo.py`
- `web/status.py`
- `web/frontend/src/views/Decisions.vue`
- `config.yaml`
- `config.yaml.example`
- `tests/test_config_schema.py`
- `tests/test_llm_client.py`
- `tests/test_llm_prompt.py`
- `tests/test_store.py`
- `tests/test_web_status.py`
- `tests/test_engine.py`
- `tests/test_throttle.py`
- `tests/test_state.py`

## 验证

执行：

```bash
.venv/bin/python -m pytest tests/test_config_schema.py tests/test_llm_prompt.py tests/test_llm_client.py tests/test_web_status.py tests/test_engine.py tests/test_throttle.py tests/test_state.py tests/test_store.py
cd web/frontend
npm run build
```

期望结果：

```text
134 passed
✓ built
```

构建过程中若出现 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
与既有构建日志一致，不影响产物生成。

## 运维注意

- 本次包含数据库字段追加，建议部署前备份当前 testnet/mainnet DB。
- 部署后需要重启 `binance-trade.service`，让交易引擎使用新的 LLM trace 落库逻辑。
- 部署后需要重启 `binance-trade-web.service`，让详情接口和前端页面加载新逻辑。
- 已打开的浏览器页面需要刷新，才能加载新的前端 chunk。
