# 2026-06-09 开启 Maker 兜底市价与 3 分钟策略周期

## 背景

当前执行层使用 `MAKER_FIRST`，优先挂 Binance USDT-M Post Only 限价单。此前配置为 `maker_unfilled_action=CANCEL`，当多次 maker 尝试均未成交时只撤单并放弃本次开仓。

用户确认希望开启“maker 全部未成交后的市价兜底”，并将主策略周期从 5 分钟调整为 3 分钟。

## 现场现象

- 当前主循环周期为 `cycle.interval=5m`。
- 当前开仓执行模式为 `entry_mode=MAKER_FIRST`。
- 当前 maker 未成交处理为 `maker_unfilled_action=CANCEL`。
- 代码已支持 `FALLBACK_MARKET` 分支，但运行配置未启用。

## 根因

这是运行策略参数选择问题，不是代码缺陷：

- `ExecutionConfig` 已支持 `MakerUnfilledAction.FALLBACK_MARKET`。
- `Executor._open_maker_position()` 在所有 maker attempt 均 0 成交且 residual myTrades 兜底仍无成交时，会根据 `maker_unfilled_action` 决定取消或转市价。
- 当前配置选择了更保守的 `CANCEL`。

## 代码改造

本次仅调整配置，不修改执行器代码：

1. `config.yaml`
   - `cycle.interval: 5m` 改为 `3m`。
   - `execution.maker_unfilled_action: CANCEL` 改为 `FALLBACK_MARKET`。

2. `config.yaml.example`
   - 同步模板默认值，避免后续复制示例配置后与当前运行策略不一致。

## 验证结果

- 配置加载校验通过：
  - `cycle.interval=3m`
  - `execution.maker_unfilled_action=FALLBACK_MARKET`
- 完整测试通过：
  - `.venv/bin/python -m pytest`
  - `253 passed, 2 warnings`
- 已重启服务：
  - `binance-trade.service` active，启动时间 `2026-06-09 15:07:11 CST`
  - `binance-trade-web.service` active，启动时间 `2026-06-09 15:07:10 CST`
- 主进程启动日志确认：
  - `engine started (mode=testnet, db=./data/trade-testnet.db, equity=5012.88)`
  - 启动对账结果 `0 open positions, 0 open orders`

## 线上状态

变更应用后预期：

- 主策略周期缩短为 3 分钟。
- 当 `MAKER_FIRST` 开仓全部 maker 尝试均 0 成交时，执行层会进入 `MARKET_TAKER` 市价兜底。
- 市价兜底仍受市价滑点护栏保护：
  - 默认 `market_slippage_bps=8`。
  - `SOLUSDT=10`。
- 如果 maker 已经部分成交，当前逻辑仍是保护已成交部分并取消剩余，不会市价补齐剩余仓位。

## 后续注意事项

- 本配置不是热更新项，必须重启交易主进程后才会生效。
- 当前 `maker_max_requotes=4` 且 `maker_timeout_seconds=15`，极端情况下会等待约 75 秒后才进入市价兜底；如果需要更快追单，应单独评估缩短 maker 等待参数。
- 若后续希望“部分成交后市价补齐剩余仓位”，需要单独设计，重点处理剩余数量、均价、手续费、交易组归并、SL/TP 数量一致性和二次风控。
