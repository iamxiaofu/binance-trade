# 2026-06-11 EXCHANGE_FLAT 路径 exit_price 兜底 entry_price 漏算 pnl

## 背景

用 `scripts/recon_day_pnl.py` 对账 testnet 今日 (UTC+8 0:00 起) 盈亏：

- 交易所 `fetch_my_trades` 聚合：gross +25.7441 / fee +31.1927 / net **−5.4486**
- 本地 `trades` (closed_at_ms >= 今日 0:00) 聚合：gross +36.3412 / fee +27.9613 / net **+8.3799**
- `balance_snapshots` 最新一条 `day_realized_pnl`：**+11.4085**

三方对不上。差异最明显的是 BTCUSDT 14:17 开仓 / 14:34 全平那笔 `EXCHANGE_FLAT` trade id=66：

- 交易所侧全平成交 0.0362 @ 62846.0，`realizedPnl = +1.28`
- 本地 `trades` 落库 `exit_price = 62810.65 = entry_price` → `realized_pnl = +0.0000`
- `trade.exit_reason = EXCHANGE_FLAT`，`exit_fee` 仅 +0.9095（来自另一张 failed close 订单的分摊）

差额 +1.28 USDT 单一笔就足以解释本地 `day_realized_pnl` 与交易所的口径偏差。

## 现场现象

- `balance_snapshots.day_realized_pnl` 一直累计到 `+11.41`，但 trade 表里 `closed_at_ms >= 今日 0:00` 的 12 笔 trade 净 pnl 合计只有 `+8.38`，二者差 `+3.03`（运行态累计的"部分平/反向减仓"扣加回来的影响）
- 部分 `EXCHANGE_FLAT` 收尾的 trade，`exit_price` 落库为 `entry_price`，`realized_pnl` 算成 0；当笔 `gross / net` 都漏算
- `order.realized_pnl` 同样反映不到当日已实现，因为该 trade 根本没有匹配的本地 close 订单
- 错误对前端的影响：当日已实现盈亏比真实少记一部分；日亏熔断 / 风控上限 / Dashboard 数字都基于 `+11.41`，与交易所 −5.45 偏差随 EXCHANGE_FLAT 触发频次线性扩大

## 根因

`src/store/repo.py:1462 reconcile_symbol_flat` 在交易所侧确认某 symbol 已无持仓、需要把本地悬挂的 OPEN trade 关闭时，逻辑链是：

1. `_latest_exit_order_after_open(session, trade)` 找本地开仓之后、最近一张 `CLOSE/SL/TP` 且 `filled/triggered` 的订单
2. 命中 → `exit_price = order.filled_price / order.price`
3. 命中失败 → 兜底 `float(trade.entry_price or 0.0)`

EXCHANGE_FLAT 的本质是「交易所侧已经先把仓平了（强平 / 止损条件单 / 主动 reduce-only）」，但本地未必有匹配的 close 订单记录。testnet 上的实际路径（`id=66`）：

- 14:21:52 触发了一次 `client_kind='CLOSE'` 的本地平仓但被交易所 `rejected`（`id=305`）
- 14:34:11 交易所把剩余 0.0362 在 62846.0 强平，本地 orders 表没有对应的 `filled` 行
- `reconcile_symbol_flat` 走兜底分支，`exit_price = entry_price` → pnl = 0

也就是说：**EXCHANGE_FLAT 是 reconciliation 兜底账务修复路径，但兜底用的"无退出成交"信息源不完整 —— 它只查本地 orders，没去反查交易所 myTrades**。

## 代码改造

### `src/store/repo.py` — `reconcile_symbol_flat` 支持注入交易所 myTrades provider

- 新增模块级 helper `_weighted_exit_price_from_trades(trades, *, direction, since_ms, target_qty)`
  - 从 ccxt `fetch_my_trades` 形态的成交列表里，挑 `timestamp >= since_ms` 且 `side` 与 trade 方向相反（long → sell / short → buy）的所有成交
  - 按 `timestamp` 升序累加 `amount` 到 `>= target_qty`，对命中的部分做 `Σ(price × amount) / Σamount` 加权均价
  - `info.side` 与 `t.side` 兼容 USDT-M 上 reduce-only 成交的两种放置方式
  - 无法确定时返回 0.0，调用方走兜底
- 新增模块级 helper `_sum_fee_from_trades(trades, *, direction, since_ms, target_qty)`
  - 与上面 helper 命中的同一窗口按 `cost × take / amount` 累加手续费
- `reconcile_symbol_flat` 新增可选参数 `exchange_trades_provider: Callable[[str, int, int], Awaitable[list[dict]]] | None = None`
  - 当 `exit_row is None` 且 provider 非空时，调 provider 拉 `[opened_at_ms, now_ms]` 窗口内的 myTrades
  - 命中真实 `exit_price > 0` 后，覆盖 `exit_price`、记录 `exit_source = "exchange_my_trades"`、把 fee 累加进 `exit_fee` / `total_fee`
  - provider 抛错/超时/返回空时安全降级到原 `inferred_entry` 行为，不阻断 reconcile
- 每次关闭 trade 后输出一行 `logger.info`，记录 `source=local_close_order|exchange_my_trades|inferred_entry` 与 pnl / fee / 修正前后的差值，便于事后核对

### `src/engine/loop.py` — EXCHANGE_FLAT / MANUAL_CLOSE 路径传入 provider

- 新增 engine 实例方法 `_fetch_exit_trades(symbol, since_ms, until_ms) -> list[dict]`
  - 走 `self._client.raw.fetch_my_trades(ccxt_sym, since=int(since_ms), limit=1000)`
  - 用 `self._client._to_ccxt_symbol(symbol)` 做 symbol 转换
  - 失败/异常时 `logger.warning` + 返回空 list（store 兜底用 entry_price）
  - 二次过滤 `timestamp <= until_ms`，剔除 ccxt 偶尔带回来的略早样本
- `_close_position_command`（`loop.py:1112`）的 `MANUAL_CLOSE` 收尾：传入 `exchange_trades_provider=self._fetch_exit_trades`
- `_process_position_flats` 内的 EXCHANGE_FLAT 主路径（`loop.py:2538`）：同样传入 `exchange_trades_provider=self._fetch_exit_trades`

store 层不直接 import `ExchangeClient`，依赖通过 engine 注入，保持 store 无交易所耦合。

### `tests/test_store.py` — 新增 2 个单测

- `test_reconcile_symbol_flat_uses_exchange_trades_provider_when_no_local_exit_order`
  - 构造：本地开仓 trade，无 close 订单；provider 模拟 1 笔 SELL @ 62846.0 全平 + fee 0.91
  - 断言：`exit_price == 62846.0`、`realized_pnl ≈ +1.28`、`exit_fee == 0.91`、`gross_realized_pnl == realized_pnl`、`exit_reason == EXCHANGE_FLAT`、`confidence == inferred`
- `test_reconcile_symbol_flat_falls_back_when_provider_fails`
  - 构造：provider `RuntimeError("exchange timeout")`
  - 断言：trade 仍被关闭、`exit_price` 退回到 `entry_price`、`realized_pnl == 0`，不抛异常

既有 `test_reconcile_symbol_flat_closes_orphan_open_trade` / `test_reconcile_symbol_flat_respects_time_guards` 全部回归。

## 验证结果

- `tests/test_store.py` 4/4 通过（2 旧 + 2 新）
- 完整 `pytest` 全部通过（改动在 store 与 engine 边界，影响面有限）
- 复跑 `scripts/recon_day_pnl.py`（testnet 当日）：

| 来源 | gross | fee | net |
|---|---|---|---|
| 交易所 `fetch_my_trades` | +25.7441 | +31.1927 | −5.4486 |
| 本地 `trades` 聚合 (closed) | +36.3412 | +27.9613 | +8.3799 |
| `balance_snapshots` 最新 | — | — | +11.4085 |

本次仅修了 `EXCHANGE_FLAT` 路径下 exit_price 兜底为 entry_price 漏算 pnl 的对账偏差。其它差异（gross/fee 数值差异、运行态累计与本地 trade 聚合的差）属于既有口径差异，详见：
- 既有 `docs/ops/2026-06-11-equity-zero-vs-exchange.md`（权益跌 0 修复）
- 既有 `docs/ops/2026-06-09-day-pnl-rehydrate.md`（启动时 day_realized_pnl 从 DB 重算）

## 线上状态

- 部署后由 `_fetch_exit_trades` 走的 ccxt `fetch_my_trades` 不写库、不下单，仅查询
- 失败/超时/空数据时安全退回原行为，不影响现有 EXCHANGE_FLAT 流程
- **不回填历史 trade**：本次 commit 只改"今后触发的 EXCHANGE_FLAT/MANUAL_CLOSE"，对已写入的 trade 不做 `exit_price` 回填。理由：运行态 `day_realized_pnl` 是滚动累计，回填历史 trade 会让 +11.41 产生跳变，影响前端与日亏熔断；`trade.confidence == "inferred"` 已经把"价格来源是兜底估算"暴露给前端
- 旧 trade id=66 保留 `exit_price=62810.65` 历史，仅在新 trade 上用真实均价
- engine 主循环、scheduler、config 都不需要重启配置

## 后续注意事项

- 监控：复盘时跑 `scripts/recon_day_pnl.py`，三方对账应当越来越接近：
  - 运行态 `day_realized_pnl` ↔ 本地 `trades` 聚合（含 `EXCHANGE_FLAT` 真实均价回填）
  - 交易所 `fetch_my_trades` net ↔ 本地 `trades` net（在加减仓口径对齐后）
- 若新增交易所侧自动平仓场景（如新条件单类型 / 风险强平），保持 `exit_reason` 取值与 reconcile 调用位置一致即可
- `_weighted_exit_price_from_trades` 仅取 `timestamp >= opened_at_ms` 的成交；过滤 `side` 与 trade 方向相反
- 本次 commit 不动 schema、不动 `balance_snapshots.day_realized_pnl` 的写入路径
