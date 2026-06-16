# 2026-06-16 条件单成交时间与移动止损标识修复

## 背景

mainnet 出现一笔 BTCUSDT 多单：

- 开仓：`66024.2`
- 平仓：`66278.3`
- 退出原因：`SL`
- 净盈亏：`+0.24089516 USDT`
- 前端显示：`止损成交`

该单实际不是亏损止损，而是 `ADJUST_SLTP` 后上移的保护止损触发，属于盈利锁定。
用户观察到平仓后短时间内 TP 条件单仍可见。

## 根因

旧逻辑依赖周期快照发现仓位消失后调用 `mark_condition_exit`，再把最近一组 SL/TP
本地订单标记为「触发侧 filled、另一侧 canceled」。

问题有两个：

1. `mark_condition_exit` 没有把 Binance 私有流的真实触发时间写回 `orders.ts_ms`，
   因此 `trades.closed_at_ms` 继承了保护单创建时间，而不是条件单成交时间。
2. 前端把 `exit_reason=SL` 直接翻译为 `止损成交`，没有结合交易盈亏判断移动止损或保本退出。

Binance 私有流显示本次触发链路为：

- `TRADE_LITE`：成交价 `66278.30`，数量 `0.001`
- `ALGO_UPDATE`：`STOP_MARKET` 从 `TRIGGERING` 到 `TRIGGERED/FINISHED`
- `ORDER_TRADE_UPDATE`：`FILLED`，`st=ALGO_CONDITION`，`si=2000001132311409`
- 对应 TP algo 在约 10.9 秒后 `CANCELED`

因此用户看到 TP 在平仓后短暂仍挂着，是交易所条件单触发和配对撤单之间的正常异步窗口。

## 新处理策略

### 私有流为权威触发源

`TradingEngine._apply_private_event` 收到 `ORDER_TRADE_UPDATE` 后：

- 识别 `X=FILLED`
- 识别 `st=ALGO_CONDITION` 或存在 `si`
- 读取 `si` 作为本地条件单 `exchange_order_id`
- 读取 `ap/L/p` 作为成交价
- 读取 `z/l/q` 作为成交数量
- 读取 `n/N` 作为手续费
- 使用 `event.transaction_time_ms` 作为权威成交时间

随后调用 `Store.mark_condition_exit` 立即落库。

### 精确匹配本地保护单

`Store.mark_condition_exit` 现在优先按：

- `exchange_order_id`
- `client_order_id`

定位被触发的本地 SL/TP 行。命中后再取消同一 `trade_id` 下另一侧保护单。
如果没有私有流 id，仍保留旧的「最近一组 SL/TP」兜底路径。

### 成交时间写回交易生命周期

命中触发侧后同步更新：

- `orders.ts_ms`
- `orders.created_at`
- `orders.filled_qty`
- `orders.avg_price`
- `orders.fee`
- `orders.fee_asset`
- `orders.raw_json.filled_at_ms`

随后刷新 trade 聚合，使：

- `trades.closed_at_ms`
- `trades.closed_at`
- `trades.exit_price`
- `trades.exit_reason`
- `trades.net_realized_pnl`

都基于真实成交事件。

### 前端退出原因增强

交易历史仍保留原始枚举 `SL`，筛选语义不变。

显示时新增行级判断：

- `SL` 且出场价相对入场价有利，净盈亏明显为正：`移动止损成交`
- `SL` 且出场价有利但扣费后接近 0：`保本止盈`
- 其它 `SL`：`止损成交`

## 风控边界

- 不改变交易所下单、撤单和仓位管理逻辑。
- 不新增数据库列，只修正既有订单与交易聚合字段。
- 私有流未命中时，周期快照仍会兜底补状态，并写入检测时间，避免继续使用挂单时间。
- 配对 TP 的交易所撤单仍可能比 SL 成交事件晚几秒；这是 Binance 异步事件顺序，不应视为未平仓。

## 验证

- `test_mark_condition_exit_closes_group_with_filled_price`
- `test_mark_condition_exit_marks_triggered_and_cancels_other`
- `test_mark_condition_exit_locates_triggered_algo_order`
- `test_external_close_detected_in_snapshot`
- `test_private_condition_order_update_marks_exact_exit`
- 前端生产构建：`npm run build`

已知前端构建仍会输出 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告；这是既有第三方包注释位置警告，不影响构建产物。
