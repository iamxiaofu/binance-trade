# 2026-06-10 EXCHANGE_FLAT 竞态防护修复

## 背景

`2026-06-10 12:40:57 CST` 附近，BNBUSDT 一笔策略开空在交易记录中显示为同秒 `EXCHANGE_FLAT` 平仓。该问题已多次出现，若进入 mainnet 会造成交易记录误拆、单笔盈亏展示失真，并可能干扰后续本地持仓管理。

## 现场现象

- BNBUSDT 在 `12:40:57.344` 市价开空成交，数量 `6.55`，成交价约 `585.65`。
- 本地在 `12:40:57.650` 将刚创建的 trade 标为 `EXCHANGE_FLAT`，`exit_order_id=0`，退出价复用入场价。
- `12:41:16` 后的持仓快照显示交易所实际仍有 `BNBUSDT short 6.55 @ 585.65`。
- `12:41:18` 孤儿接管逻辑又把该交易所仓位接管为新 trade，后续于 `13:21:50` 正常平仓。

## 根因

`_enforce_exchange_invariants()` 的 flat 分支存在竞态：

1. 对账开始时交易所 position 还未刷新出刚成交仓位。
2. `has_open_trade()` 在某一瞬间也可能还没看到刚落库的 open trade，于是没有进入二次 flat confirmation。
3. 后续代码仍无条件调用 `reconcile_symbol_flat()`。
4. 此时新 open trade 已经出现，于是被本地误标为 `EXCHANGE_FLAT`。

本质上，`EXCHANGE_FLAT` 是 destructive 账务修复，但旧逻辑没有做到“只修复本轮确认前已经存在且足够老的本地 trade”。

## 代码改造

- Store 层新增 `has_recent_entry_claim()` 和 `has_fresh_open_trade()`，用于识别仍处于开仓/保护挂载窗口内的本地 ownership。
- `reconcile_symbol_flat()` 新增 `opened_before_ms` 和 `min_open_age_ms` 参数，只允许关闭：
  - 本轮 flat 检查开始前已存在的 open trade；
  - 且 open 年龄超过 `60s` 的 trade。
- 开仓流程中，OPEN 成交并落库后 claim 先进入 `protecting`，直到 SL/TP 检查和挂载完成后才终结为 `filled`。
- `EXCHANGE_FLAT` 分支改为：
  - 初始没有 active condition orders 且没有本地 open trade 时直接跳过；
  - flat confirmation 前后都检查 recent claim / fresh trade guard；
  - 只有确认交易所无仓位且 guard 未命中时才调用 Store 侧 flat 修复。

## 验证结果

- 针对性测试：
  - `.venv/bin/python -m pytest tests/test_engine.py tests/test_store.py`
  - 结果：`102 passed`
- 完整测试：
  - `.venv/bin/python -m pytest`
  - 结果：`280 passed, 2 warnings`
- 格式检查：
  - `git diff --check`
  - 结果：通过。

## 线上状态

- 已随提交 `d051784` 推送到 `origin/main`。
- 已于 `2026-06-10 17:47 CST` 依次重启 `binance-trade-frontend.service`、`binance-trade-web.service`、`binance-trade.service`。
- 重启后 `binance-trade.service`、`binance-trade-web.service`、`binance-trade-frontend.service` 均为 `active`。
- 交易主进程启动日志显示 testnet DB 已连接、启动对账完成，并继续处理 BNBUSDT/BTCUSDT 周期。

## 后续注意事项

- mainnet 安全优先：宁愿让本地 open trade 在不确定时多保留一个确认窗口，也不能把刚成交仓位误标为 `EXCHANGE_FLAT`。
- 后续如果日志出现 `exchange-flat reconcile deferred`，表示 guard 正在阻止不安全的本地 flat 修复，应结合交易所持仓快照确认是否为正常延后。
