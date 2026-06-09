# 2026-06-09 孤儿持仓接管 + MAKER race 修复（B + C 全套）

## 背景

2026-06-08 21:08:36 UTC 引擎为 ETHUSDT 触发 MAKER_FIRST 开仓 5 次重试：

```
attempt 1: id=9346855598 limit buy 1.17 @ 1696.75 → 21:08:36 下单，21:08:37 成交 0.088，
           剩余 1.082 被 GTX 自动撤销（Binance 部分成交后撤单行为）
attempt 2-5: ids=...5608/5620/6194/6983，全部 canceled 0 fill
```

交易所侧 fetchOrders 实证：第 1 单 `status=canceled, filled=0.088`，myTrades 记录 `id=287198547 buy 0.088 @ 1696.75 fee=0.0299`。
引擎侧五层 bug 把这次部分成交丢在交易所 6 小时无 SL/TP，最终在 21:09:14 周期对账里把 ETHUSDT 标成 disabled，
导致后续每 20 秒一行 `ETHUSDT live position detected during periodic, but symbol is disabled; skip auto protection enforcement`，
用户只能手动去交易所平掉。

### 五层 root cause

1. **`_wait_maker_fill` 异常处理**：fetch_order 抛 -2013（OrderNotFound）时，引擎把它当成"完全未成交"，把上一张 `created_order`（filled=0）返回
2. **MAKER_FIRST 5 次 attempt 之间不复盘上一单的成交**：循环里只 `fetch_order` 当前 attempt，丢了 attempt 1 的 0.088
3. **MAKER_FIRST 全失败收尾时不调 `fetch_my_trades` 对账**：5 次都"未成交"时直接 `status=canceled, qty=0` 返回
4. **状态机错误**：周期对账看到 0.088 持仓 + 本地无 trade 行 + 无 active claim，把 ETHUSDT 标 disabled（"外部开仓"假设）
5. **disabled 状态 skip SL/TP enforcement**：disabled 后永远只 warning，不补保护单
6. **快照 `leverage=0`**：ccxt 在 ISOLATED 模式下 `leverage=null`，`normalize_position` 转成 0（实际是 OPEN 时设的 3x）

## 设计目标

- 不再让 MAKER race 留孤儿仓位
- 一旦发现孤儿持仓且与最近一次策略 OPEN 失败关联，自动接管（建 trade + 补 SL/TP）而不是禁用币种
- SL/TP 挂出前对交易所侧实情做最终一致性检查（C2），避免重复挂
- 主要靠 `fetch_my_trades` + `fetch_open_condition_orders` 两个只读 API 做对账

## 改动清单

### B1 `_wait_maker_fill` 异常回退
`src/execution/executor.py:593-...` — `ccxt.OrderNotFound` 不再吞掉后直接 return，
而是先调 `_recover_via_my_trades(order_id)` 拿真实成交数据，构造一份"已成交剩余被取消"的 order dict 返回。
对瞬时错误（RateLimitExceeded / NetworkError / DDoSProtection）走"继续重试到 deadline"；
对其它 ExchangeError（参数/账户）立即 abort，避免后续 attempt 误判。

### C1 maker wait 硬上限
`src/execution/executor.py:...` — `_wait_maker_fill` 在 `maker_timeout_seconds * 2`（下限 5 秒）硬中止。
防止 API hang 把策略循环卡死。

### B2 MAKER_FIRST attempt 间累计
`src/execution/executor.py:...` — `_open_maker_position` 累计跨 attempt 的真实成交（`cumulative_filled` / `cumulative_cost` / `cumulative_fee`）：
- 每次 attempt 拿到 fill_qty > 0 后累加到总额
- 累计达到 `planned_qty` 立即 break，不再发后续 attempt
- 收尾按累计额出 `filled` 或 `partial` 结果

### B3 MAKER_FIRST 全失败兜底
`src/execution/executor.py:...` — 5 次 attempt 全 `fill_qty == 0` 时，再调 `_reconcile_maker_residual_fills` 把所有 `attempt_order_ids` 用 `fetch_order_trades` 查一遍。
命中则按 `partial` 出结果，不命中则按原逻辑返回 `canceled` / 走 FALLBACK_MARKET。

### B4 状态机：孤儿持仓 + 最近 claim → 接管
`src/engine/loop.py:...` — 新增 `_adopt_orphan_position`。`_should_enforce_position_protection` 在原本
"禁用币种"分支之前先尝试接管：
- 查 `latest_finished_position_claim(symbol, within_ms=900_000)` 是否有最近 15 分钟内收尾的 claim
- 校验 claim 方向 == 交易所持仓方向
- 校验 0.05 ≤ qty/planned_qty ≤ 1.5（避免误把人工外部仓位接管）
- `ensure_takeover_trade` 建 `source=orphan_adoption` 的 trade 行
- 用 `latest_open_decision` 的 `stop_loss_pct` / `take_profit_pct` 直接 `place_protection_orders` 挂 SL/TP
  （如果历史模板不存在则回退 `_repair_sl_tp`）
- 重新启用币种，让 `_enforce_exchange_invariants` 后续周期持续监控

外层 `_enforce_exchange_invariants` 用 `self._just_adopted` 标志，adoption 后重新拉 `active_orders`，
避免后续 `has_stop` 用旧数据误判"SL 缺失 → 禁用币种"。

新增 `latest_finished_position_claim` repo 方法（`src/store/repo.py:...`）。

### B5 前端：禁用币种 + 持仓的可见警示
`web/server.py:...` — `_status_summary` 暴露 `symbol_enabled` 和 `disabled_with_position` 集合。
`web/frontend/src/views/Positions.vue:...` — 顶部新增 warning banner（"X 个币种被禁用但仍有交易所持仓"+ 一键启用按钮），
position 行内对 disabled 的 symbol 显示"币种已禁用"tag + "启用并自动接管"按钮。

### B6 snapshot leverage=null 推导
`src/exchange/positions.py:...` — 新增 `_derive_leverage_from_margin(notional, initial_margin, isolated_margin)`。
交易所明确返回 leverage 时优先使用；ISOLATED 模式 leverage=null 时按 notional/initial_margin 推导出 [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 125] 档位。
ETH 0.088 这种典型场景：notional 146.6 / initial_margin 48.9 ≈ 3.0 → leverage=3。

### C2 attach_sl_tp 二次确认
`src/engine/loop.py:...` — 新增 `_precheck_before_attach_sl_tp`，在下 SL/TP 之前：
1. `fetch_positions` 拉最新持仓，方向不对 / 已无仓 / qty 漂移 → 拒挂（`reason` 落到 `_handle_unprotected_open_failure`）
2. `fetch_open_condition_orders` 查已挂的同方向 STOP_MARKET / TAKE_PROFIT_MARKET → 避免重复挂

通过后才用 `live_qty` / `live_entry`（不是本地 `result["qty"]` / `result["price"]`）构造 SL/TP 触发价。

## 单元测试

- `tests/test_executor.py` 新增 5 个：B1 异常回退、B1 兜底走 unfilled、B3 残余对账、B2 跨 attempt 累加、B2 不足累计 partial、C1 hard cap、B1 myTrades 多笔成交加权均价
- `tests/test_engine.py` 新增 4 个：B4 孤儿接管建 trade + 补 SL/TP、B4 无 claim 走原"禁用"、B4 claim side 不匹配不接管、B4 qty 漂出范围不接管
- `tests/test_exchange_normalizers.py` 新增 3 个：B6 ISOLATED 反推 3x、B6 显式 leverage 优先、B6 缺数据回退 0

总计 239 passed, 2 warnings（pytest 7.4s + ~28s integration），无回归。

## 兼容性

- DB schema 不变
- config.yaml 无变更
- 既有测试 `test_reconcile_disables_enabled_unmanaged_position_without_auto_close` 仍然通过（FakeStore 默认无 finished_claim，行为等同旧实现）
- mainnet 切到实盘前必做的 C 部分（除 C1/C2 已做）剩下：C3 cross-process crash recovery 的端到端验收（建议在 testnet 用一次手动 kill -9 引擎来验）

## 已知限制

- `_adopt_orphan_position` 仅识别"最近 15 分钟内"收尾的 claim；超过 15 分钟的孤儿持仓会被识别成"外部开仓"并按原路径禁用币种（保留旧行为，避免误接管人工仓位）
- SL/TP 触发价依赖 `latest_open_decision` 的 pct；外部开仓（无 LLM 决策历史）仍需走 `REPAIR_SL_TP` 或 `PROTECT_POSITION` 人工命令
- `_precheck_before_attach_sl_tp` 只检查同方向 STOP/TAKE_PROFIT；同方向 LIMIT reduce-only 不在去重范围（属于正常平仓/补仓语义）
