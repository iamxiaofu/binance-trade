# 2026-06-07 生产化执行模式与 Maker-First 改造

## 背景

当前执行器虽然在配置中存在：

```yaml
execution:
  order_type: MARKET
```

但真实开仓逻辑一直固定为市价单：

```text
create_order(symbol, side, qty, "market")
```

这会带来几个问题：

- 配置语义和真实行为不一致，`LIMIT` 不会真正生效。
- 实盘手续费通常区分 maker / taker，市价单固定偏向 taker。
- LLM 不应该直接决定订单撮合方式；LLM 只应该给方向、仓位比例、杠杆、止损止盈等交易意图。
- maker 限价单存在部分成交风险，必须有明确的保护单和撤剩余量策略。
- 后续 mainnet 实盘和当前 testnet 应共享同一套生产执行语义，只通过数据库和交易环境隔离。

因此本次把执行层从“配置里写订单类型”改为“生产执行模式”。

## 官方接口依据

本次实现按 Binance USD-M Futures 官方接口语义设计：

- New Order 支持 `LIMIT`、`MARKET`、`STOP_MARKET`、`TAKE_PROFIT_MARKET`、`timeInForce`、`reduceOnly`、`newClientOrderId`。
- 公共定义中 `GTX` 表示 Post Only。
- 订单更新状态包含 `NEW`、`PARTIALLY_FILLED`、`FILLED`、`CANCELED`、`EXPIRED`。
- 成交明细可按订单查询手续费、成交方向和 maker/taker 标记。

参考：

- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order
- https://developers.binance.com/docs/derivatives/usds-margined-futures/common-definition
- https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Order-Update
- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Account-Trade-List

## 设计目标

- LLM 只输出交易意图，不输出 maker/taker 细节。
- 执行器按配置决定开仓和平仓的执行模式。
- 旧配置 `order_type` 继续兼容，避免旧库/旧配置无法启动。
- 默认开仓切换为 `MAKER_FIRST`。
- 未成交 maker 单默认取消，不自动市价追单。
- 任意已成交数量都必须马上进入后续保护单流程。
- maker 部分成交时取消剩余未成交量，保护已成交数量。
- 紧急平仓、保护失败平仓、熔断平仓、kill-switch 平仓固定使用 `MARKET_TAKER`。
- 数据库记录请求数量、成交数量、剩余数量、限价、执行模式、maker/taker 和手续费。
- 交易汇总展示毛盈亏、手续费、净盈亏。

## 新配置

执行层配置改为：

```yaml
execution:
  entry_mode: MAKER_FIRST
  normal_exit_mode: MARKET_TAKER
  emergency_exit_mode: MARKET_TAKER
  maker_time_in_force: GTX
  maker_timeout_seconds: 8
  maker_poll_seconds: 1
  maker_max_requotes: 2
  maker_price_offset_bps: 1
  maker_unfilled_action: CANCEL
  partial_fill_action: PROTECT_AND_CANCEL_REST
  attach_sl_tp: true
  rate_limit_backoff: 1.5
  max_order_retries: 3
  recv_window: 5000
```

字段含义：

- `entry_mode`
  - `MARKET_TAKER`：直接市价开仓。
  - `MAKER_ONLY`：只挂 maker，未成交就取消。
  - `MAKER_FIRST`：优先 maker，未成交后按 `maker_unfilled_action` 处理。
- `normal_exit_mode`
  - 策略主动平仓模式。
  - 当前默认 `MARKET_TAKER`，避免 maker 平仓部分成交后保护单数量错配。
  - 代码已支持 maker 平仓路径，后续确认保护单 resize 流程稳定后可切换。
- `emergency_exit_mode`
  - 紧急退出模式。
  - 当前强制 `MARKET_TAKER`，禁止配置为 maker。
- `maker_time_in_force`
  - 固定 `GTX`，即 Binance Post Only。
- `maker_timeout_seconds`
  - 单次 maker 挂单等待成交的时间。
- `maker_poll_seconds`
  - 轮询订单状态间隔。
- `maker_max_requotes`
  - 零成交时取消并重新按盘口报价的次数。
- `maker_price_offset_bps`
  - maker 报价相对 best bid/ask 的偏移，单位 bps。
- `maker_unfilled_action`
  - `CANCEL`：未成交即取消，不追价。
  - `FALLBACK_MARKET`：maker 未成交后退回市价，目前不作为默认。
- `partial_fill_action`
  - 当前支持 `PROTECT_AND_CANCEL_REST`：保护已成交数量，取消剩余量。

旧配置兼容：

```yaml
execution:
  order_type: MARKET
```

启动时会解析为：

```text
entry_mode = MARKET_TAKER
```

旧配置：

```yaml
execution:
  order_type: LIMIT
```

启动时会解析为：

```text
entry_mode = MAKER_FIRST
```

## 开仓执行流程

### MARKET_TAKER

保持旧行为：

1. 规整数量。
2. 设置保证金模式和杠杆。
3. 下 `MARKET` 单。
4. 解析成交数量、成交均价、手续费和 taker 标记。
5. 返回 `filled` 或 `partial`。
6. 引擎按已成交数量挂 SL/TP。

### MAKER_ONLY / MAKER_FIRST

新流程：

1. 从盘口获取 best bid / best ask。
2. 多单按 best bid 下方偏移报价；空单按 best ask 上方偏移报价。
3. 按 tickSize / stepSize 规整价格和数量。
4. 下 `LIMIT + GTX` post-only 单。
5. 按 `maker_poll_seconds` 查询订单状态。
6. 如果完全成交：
   - 返回 `filled`。
   - 记录 maker、手续费、成交均价。
   - 引擎挂对应数量 SL/TP。
7. 如果部分成交：
   - 立即取消剩余未成交量。
   - 返回 `partial`。
   - `qty` 是已成交数量。
   - `remaining_qty` 是未成交且已取消的剩余数量。
   - 引擎只按已成交数量挂 SL/TP。
8. 如果零成交超时：
   - 取消 maker 单。
   - 如果还有 requote 次数，重新按盘口报价。
   - 如果最终仍零成交：
     - `maker_unfilled_action=CANCEL`：记录 `canceled`，不产生仓位。
     - `maker_unfilled_action=FALLBACK_MARKET`：退回市价开仓。

## 部分成交保护原则

原则：

```text
任何已成交仓位，必须立刻有对应数量的保护单。
```

示例：

计划开多 `BTCUSDT 0.05`，maker 单只成交 `0.02`。

正确处理：

1. `0.02` 已经是真实仓位。
2. 立刻取消剩余 `0.03`。
3. 返回开仓结果：

```text
status=partial
qty=0.02
requested_qty=0.05
remaining_qty=0.03
```

4. 引擎按 `0.02` 挂 SL/TP。
5. 如果 SL 未确认，沿用现有保护失败兜底：
   - 禁用该币种。
   - 紧急市价平掉未保护仓位。

这样不会出现“等待整个 maker 单全成期间，已成交部分裸奔”的问题。

## 平仓执行策略

`close_position()` 已支持执行模式参数。

当前生产默认：

```text
normal_exit_mode = MARKET_TAKER
emergency_exit_mode = MARKET_TAKER
```

原因：

- 主动平仓如果使用 maker，可能只部分平仓。
- 部分平仓后原 SL/TP 数量会大于剩余持仓数量。
- 系统必须重新修复保护单数量。

本次已经接入部分平仓处理：

1. 如果 maker 平仓部分成交，不会把本地持仓直接移除。
2. 不会直接取消全部保护单。
3. 会调用现有 `REPAIR_SL_TP` 逻辑，按交易所剩余持仓重建保护单。
4. 如果修复失败：
   - 禁用该币种。
   - 紧急市价平掉剩余未保护仓位。

但为了降低实盘首次切换风险，运行配置仍保持普通策略平仓为 `MARKET_TAKER`。

紧急退出包括：

- 缺失保护单的已接管仓位。
- 熔断。
- cancel-and-flatten。
- kill-switch。

这些场景必须优先降低风险，不追求 maker 手续费，因此固定 `MARKET_TAKER`。

## 数据结构

扩展 `orders` 表：

- `execution_mode`
- `time_in_force`
- `requested_qty`
- `filled_qty`
- `remaining_qty`
- `requested_price`
- `limit_price`
- `avg_price`
- `liquidity`
- `fee`
- `fee_asset`
- `client_order_id`

字段说明：

- `requested_qty`：原始请求数量。
- `filled_qty`：实际成交数量。
- `remaining_qty`：剩余未成交数量。
- `limit_price`：maker 限价。
- `avg_price`：成交均价。
- `liquidity`：`maker` / `taker`。
- `fee`：订单成交手续费。
- `fee_asset`：手续费资产。
- `client_order_id`：本地生成的 Binance `newClientOrderId`。

扩展 `trades` 表：

- `entry_fee`
- `exit_fee`
- `total_fee`
- `gross_realized_pnl`
- `net_realized_pnl`
- `net_pnl_pct_on_margin`
- `entry_liquidity`
- `exit_liquidity`

字段说明：

- `realized_pnl` 保持旧语义，仍表示未扣手续费的毛盈亏。
- `gross_realized_pnl` 与 `realized_pnl` 对齐，便于后续明确字段语义。
- `net_realized_pnl = gross_realized_pnl - total_fee`。
- `net_pnl_pct_on_margin = net_realized_pnl / entry_margin * 100`。

## 迁移策略

继续使用项目已有轻量 SQLite 迁移：

1. `Base.metadata.create_all` 创建新表。
2. 通过 `PRAGMA table_info(orders)` 检查订单表列。
3. 对缺失列执行 `ALTER TABLE orders ADD COLUMN ... DEFAULT ...`。
4. 通过 `PRAGMA table_info(trades)` 检查交易组表列。
5. 对缺失列执行 `ALTER TABLE trades ADD COLUMN ... DEFAULT ...`。

该流程幂等：

- 新库直接创建完整字段。
- 旧库启动时自动补列。
- 重复启动不会重复加列。
- 旧数据没有 maker/taker 和手续费时保持默认空值或 0。

## Web 展示

交易记录页保留原结构：

- 交易汇总。
- 展开订单流水。
- 独立订单流水。
- 风控拒单。

新增展示：

- 订单执行模式。
- maker/taker 流动性。
- 订单手续费。
- 交易组手续费。
- 毛盈亏。
- 净盈亏。

旧数据兼容：

- 没有手续费字段的历史交易显示 0 或空值。
- 没有执行模式的历史订单显示空值。
- 原有保证金、杠杆、名义价值展示不变。

## 兼容性

兼容旧配置：

- `order_type: MARKET` -> `entry_mode=MARKET_TAKER`
- `order_type: LIMIT` -> `entry_mode=MAKER_FIRST`

兼容旧数据：

- `orders` 旧记录不会被删除。
- `trades` 旧记录不会被重建。
- 新字段默认值不会影响旧查询。

兼容当前风险边界：

- 未接管人工持仓仍不会被自动平仓。
- 已接管仓位缺失 SL 仍会触发保护失败兜底。
- 动态币种启停逻辑不变。

## 回滚方案

代码回滚：

1. 回退本次提交。
2. 恢复 `config.yaml` 中执行层配置为：

```yaml
execution:
  order_type: MARKET
  attach_sl_tp: true
  rate_limit_backoff: 1.5
  max_order_retries: 3
  recv_window: 5000
```

3. 重启：

```bash
systemctl restart binance-trade.service
systemctl restart binance-trade-web.service
```

数据库回滚：

- 本次只新增列，不删除表和数据。
- SQLite 不需要为了回滚删除新增列。
- 如果必须恢复到变更前快照，使用部署前备份的 `data/backups/*.db` 覆盖对应环境 DB。

运行降级：

如果不回滚代码，只想临时恢复旧市价行为，改配置即可：

```yaml
execution:
  entry_mode: MARKET_TAKER
  normal_exit_mode: MARKET_TAKER
  emergency_exit_mode: MARKET_TAKER
```

然后重启交易服务。

## 验证

已新增/更新测试：

- 市价开仓仍按旧路径执行。
- 市价平仓仍带 `reduceOnly`。
- `order_type=LIMIT` 自动兼容为 `MAKER_FIRST`。
- `order_type` 缺省时自动兼容为 `MARKET_TAKER`。
- `emergency_exit_mode` 禁止配置成 maker。
- maker 开仓部分成交时取消剩余量并返回 `partial`。
- maker 开仓零成交时取消订单，不创建仓位。
- 部分平仓时交易组保持 `partial`。
- 剩余仓位再次平掉后交易组转为 `closed`。
- 手续费累计并计算净盈亏。

验证命令：

```bash
.venv/bin/pytest tests/test_executor.py tests/test_store.py tests/test_config_schema.py tests/test_engine.py
```

结果：

```text
91 passed
```

后续完整发布前还需要执行：

```bash
.venv/bin/pytest
npm --prefix web/frontend run build
```
