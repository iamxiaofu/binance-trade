# 2026-06-09 EXCHANGE_FLAT 误判与手续费去重修复

## 背景

ETHUSDT 在 `2026-06-09 19:09:35 CST` 出现本地记录“开空后立即 EXCHANGE_FLAT 平仓”，但交易所实际仍保留空仓：

- 本地 OPEN 订单：`9370602752`
- 本地记录成交：`2.494 @ 1668.89`
- 交易所显示成交额：`4166.17712000 USDT`
- 交易所显示手续费：`1.66647084 USDT`
- 交易所实际均价：`4166.17712000 / 2.494 = 1670.48`

## 根因

1. `_enforce_exchange_invariants()` 对账时只做一次 `fetch_positions`。
   如果交易所短时间没有返回刚成交的仓位，系统会把本地 open trade 直接落成 `EXCHANGE_FLAT`，并取消已有 SL/TP。

2. `create_order` 响应缺少真实均价或成交额时，执行层会回退到下单参考价。
   本次 ETH 前端显示的 `1668.89` 就来自参考价，而不是交易所最终成交均价。

3. `myTrades` 中同一笔手续费可能同时出现在 `trade.info.commission`、`trade.fee.cost`、`trade.fees[]`。
   旧逻辑三处累加，导致 `1.66647084` 被记成约 `4.99941252`。

## 代码改造

- 新增 `_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS`。
  对“交易所侧暂时查不到持仓”进行多次确认；只要确认窗口内持仓重新出现，就不执行本地 `EXCHANGE_FLAT`、不标记条件单 canceled、不撤 SL/TP。

- `_confirm_exchange_flat()` 只在本地仍有 open trade、或交易所仍有 active SL/TP 条件单时触发。
  没有本地持仓也没有挂单的普通空仓 symbol 不等待，避免拖慢周期对账。

- 执行层以 `fetch_order_trades/myTrades` 为准回填：
  - 实际成交数量
  - 实际成交均价
  - 实际成交额
  - 实际手续费

- 手续费去重规则：
  - 每条 trade 优先使用 `info.commission + info.commissionAsset`。
  - 如果没有 `info.commission`，再使用 `fees[]`。
  - 如果没有 `fees[]`，最后使用 `fee`。
  - 有 `myTrades` 时忽略 order 级别 fee，避免重复统计。

## 覆盖测试

- `tests/test_engine.py`
  - 覆盖首次 `fetch_positions` 为空、确认时仓位重新出现的 race。
  - 断言不会调用 `reconcile_symbol_flat`，不会标记条件单 canceled，也不会撤 SL/TP。

- `tests/test_executor.py`
  - 覆盖手续费三重来源去重。
  - 覆盖市价开仓 order 响应缺少均价时，用 `myTrades` 覆盖 entry price、notional、fee。

## 历史数据修复口径

ETHUSDT 当前应以交易所成交数据为准：

- qty: `2.494`
- entry_price: `1670.48`
- entry_notional: `4166.17712000`
- leverage: `5`
- entry_margin: `833.235424`
- entry_fee: `1.66647084 USDT`

本地误落的 `EXCHANGE_FLAT` 记录应置零手续费和已实现盈亏，并从当日 PnL 统计中排除；当前仍 open 的接管 trade 应关联到真实 entry order `9370602752`，继续承载后续 SL/TP 与最终平仓。

## 本次已执行的数据修复

- 备份文件：
  `data/backups/trade-testnet-before-eth-exchange-flat-fee-repair-20260609-201015.db`
- `orders.id=123`
  - `trade_id` 从 `28` 改为 `29`
  - `price/avg_price` 改为 `1670.48`
  - `notional` 改为 `4166.17712`
  - `fee` 改为 `1.66647084 USDT`
  - `margin` 改为 `833.235424`
- `trades.id=29`
  - 作为真实 ETHUSDT entry trade
  - `entry_order_id=123`
  - `entry_fee=total_fee=1.66647084`
  - `confidence=exact`
- `trades.id=28`
  - 保留为可追溯的修复记录
  - `exit_reason=FALSE_EXCHANGE_FLAT`
  - 手续费和 PnL 全部置零
  - `closed_at_ms=0`，不再进入当日 PnL 重算

## 重启后结果

- `binance-trade.service` 重启后识别到 ETHUSDT live short，并同步到历史 SL 条件单。
- 随后交易所条件单触发，ETHUSDT 在 `2026-06-09 20:14:10 CST` 被检测为已平仓。
- 本地 `trades.id=29` 最终状态：
  - `status=closed`
  - `exit_reason=SL`
  - `entry_price=1670.48`
  - `exit_price=1676.46`
  - `gross_realized_pnl=-14.91412000`
  - `net_realized_pnl=-16.58059084`
- 最新持仓快照显示 ETHUSDT `contracts=0`。

## 后续注意

- 本修复阻止后续同类误判；本次 ETH 历史误判记录已经完成一次性 DB 修复。
- 如果交易所连续多次 fetch error，确认结果视为不确定，系统会延后本地 flat reconcile，而不是立即取消保护单。
- 条件单触发后的退出手续费当前仍依赖条件单历史回填，未像主动市价开/平一样从 `myTrades` 精确补齐；如需前端总手续费完全等于交易所最终流水，需要单独增强条件单成交明细回填。
