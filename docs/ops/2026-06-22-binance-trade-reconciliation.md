# Binance 外部成交核对与版本化修复

## 问题

Binance `userTrades` 不返回可靠的 `clientOrderId`、条件单类型和实际
`reduceOnly` 元数据。条件单触发后，成交使用新的实际 MARKET 订单 ID，本地保存的
则是 algoId。旧逻辑无法匹配订单时，又以“当时是否存在 Engine 持仓”推断成交归属，
导致 Engine 止盈/止损成交被错误标记为 `mixed` 或 `external`。

旧外部交易重建器还存在两个边界错误：

- 把成交归属和完整交易周期归属混为一谈；
- 反手成交按数量比例拆分 Binance `realizedPnl`，但该值只属于平仓部分。

## 修复设计

交易记录页新增“一键核对修复”，采用预览和应用两阶段：

1. 按一天窗口读取 Binance income，发现核对范围内所有交易币种；
2. 按六天窗口读取 Binance `userTrades`，与本地成交 ID、订单 ID、方向、数量、
   价格、手续费和已实现盈亏逐笔比对；
3. 按实际订单 ID 查询 Binance order，恢复 `clientOrderId` 与 `reduceOnly`；
4. 结合本地 `ALGO_UPDATE` / `ORDER_TRADE_UPDATE`，恢复实际订单与 algoId 的关联，
   精确识别 TP、SL 和手工只减仓；
5. 用单向持仓状态机重放全部 Engine/External 成交。单笔成交只允许
   `engine` 或 `external`，完整周期可为 `external` 或 `mixed`；
6. 校验数量、手续费、已实现盈亏守恒，校验只减仓不能反手，并将最终重建仓位与
   Binance 当前仓位逐币种比较；
7. 预览通过后生成不可变版本。主网应用时再次拉取数据、校验预览哈希、自动备份
   SQLite，最后在单个事务中更新成交解析元数据并切换活动版本。

任何一步不一致都会拒绝应用。旧 `external_trades` 不删除，活动版本通过
`runtime_settings.binance.trade_cycles.active_run_id` 切换，便于审计和回滚。

## 数据表

- `exchange_reconcile_runs`：预览/已应用/已替代的核对版本；
- `binance_trade_cycles`：版本化的外部参与交易周期；
- `binance_trade_cycle_fills`：成交到周期的数量、手续费和盈亏分配；
- `exchange_fills.resolved_*`：Binance 订单和私有事件恢复出的权威元数据。

## 回滚

数据库备份位于数据库同目录的 `backups/`。逻辑回滚可把
`binance.trade_cycles.active_run_id` 改回前一已保留版本，并将对应 run 标记为
`applied`；物理回滚可在停止交易和 Web 服务后恢复自动备份。

不要删除旧版本或旧外部交易表，除非已完成独立审计与备份。
