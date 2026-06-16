# 2026-06-16 运行态执行参数与 maker 部分成交修复

## 背景

前端已有动态风险参数和 Engine/LLM 触发参数，但执行层的 maker 策略仍主要依赖
`config.yaml` 静态配置。现网需要在不改 `/etc/binance-trade/*.yaml`、不直接操作交易所的前提下，
动态调整开仓执行模式、maker 挂单偏移、maker 等待超时、市价兜底滑点等参数。

同时代码审查发现 maker 开仓存在一个潜在超开风险：

- 第一次 maker 订单部分成交后，系统会撤掉剩余订单。
- 后续 re-quote attempt 仍按原始目标数量重新挂单。
- 如果后续订单继续成交，累计成交量可能超过原计划仓位。

该问题只影响 maker 开仓路径，不影响市价开仓、平仓和保护单下发。

## 改动

### 1. maker 部分成交按剩余量重挂

`Executor._open_maker_position` 现在使用累计真实成交量计算剩余目标数量：

```text
remaining = target_qty - cumulative_filled
```

每次重新报价前都按 `remaining` 重新执行交易所精度和最小名义价值规整。

新行为：

- 累计成交达到目标数量后立即停止后续 attempt。
- 后续 maker attempt 只请求剩余未成交数量。
- 若剩余数量低于交易所最小下单要求，返回已有累计成交的 partial 结果，不再为了补尾差超量下单。

### 2. 新增运行态 execution settings

新增运行时键：

- `execution.effective`
- `execution.version`

它们存储在各环境独立 SQLite runtime settings 中，和现有：

- `risk.effective/version`
- `engine.effective/version`

保持同一套版本冲突和命令队列模型。

允许动态调整的执行参数：

| 参数 | 说明 |
|---|---|
| `entry_mode` | 开仓执行模式：`MAKER_FIRST` / `MAKER_ONLY` / `MARKET_TAKER` |
| `maker_timeout_seconds` | 单次 maker 挂单等待超时 |
| `maker_poll_seconds` | maker 订单状态轮询间隔 |
| `maker_max_requotes` | maker 重新报价次数，实际尝试次数为 `+1` |
| `maker_price_offset_bps` | maker 挂单偏移，单位 bps |
| `maker_unfilled_action` | maker 全部未成交后的处理：市价兜底或取消 |
| `market_slippage_bps` | 市价单/市价兜底全局滑点上限 |
| `market_slippage_bps_per_symbol` | 按币种覆盖滑点上限 |
| `max_order_retries` | 下单瞬时错误重试次数 |
| `rate_limit_backoff` | 限频/网络错误指数退避倍数 |

固定或不建议热调的参数只展示，不允许前端修改：

- `maker_time_in_force=GTX`
- `normal_exit_mode=MARKET_TAKER`
- `emergency_exit_mode=MARKET_TAKER`
- `partial_fill_action=PROTECT_AND_CANCEL_REST`
- `attach_sl_tp=true`
- `recv_window`
- 旧兼容字段 `order_type`

### 3. Web API

新增接口：

- `GET /api/execution-settings`
- `POST /api/execution-settings/preview`
- `POST /api/execution-settings/apply`

`UPDATE_EXECUTION_SETTINGS` 被加入：

- 允许命令白名单
- mainnet 高风险确认列表
- Engine 命令消费与唤醒逻辑

mainnet 仍需要一次性 `MAINNET` 确认令牌。Web API 只写命令队列，不直接改交易所状态。

### 4. 前端参数控制面板

新增菜单项：`参数控制`。

该页面统一承载：

- 动态风险参数
- 引擎分析与 LLM 调用频率
- 挂单策略与执行参数

原 `操作面板` 只保留运行操作：

- 暂停/恢复策略
- 币种启停和新增/复核
- 撤单平仓、停止引擎、Kill Switch
- 命令历史

执行参数表单支持 iOS/触屏端点击 tooltip 查看中文说明。

## 线上影响

- 已有持仓、已挂普通订单、已挂条件单不会被回写修改。
- execution settings 生效后，只影响后续新订单。
- 服务重启时会从对应环境 SQLite 加载 `execution.effective`，缺省则用 YAML 当前值写入 runtime settings。
- mainnet 重启后仍会进入 `MAINNET_RESTART_GUARD`，需要人工恢复策略。

## 回滚注意事项

如需回滚代码，应同时注意 runtime settings 中可能已经存在 `execution.effective/version`。
旧代码不会读取这两个键，回滚后执行参数会回到 YAML 静态配置。

若要强制恢复默认执行参数，可在新版本前端参数控制中把字段改回 `/etc/binance-trade/config.*.yaml`
对应值，或停机后清理对应环境 SQLite runtime settings 中的 `execution.effective/version`。

## 验证

- maker 部分成交累计测试：确认重挂数量为剩余量，不再按原始数量重挂。
- runtime execution settings 单测：默认值、白名单、币种校验、编码解码、转回 `ExecutionConfig`。
- Engine 命令测试：`UPDATE_EXECUTION_SETTINGS` 版本化、热应用到 executor、睡眠期间唤醒策略循环。
- 全量测试：`.venv/bin/pytest`，`365 passed`。
- 前端构建：`npm --prefix web/frontend run build` 通过。
- Python 编译：`python -m compileall src web tests` 通过。
