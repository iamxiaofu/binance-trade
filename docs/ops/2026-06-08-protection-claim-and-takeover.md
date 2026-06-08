# 2026-06-08 持仓保护竞态修复与接管补单

## 背景

`SOLUSDT` maker-first 开仓时出现过以下时序：

1. 交易所先产生 maker 部分成交，账户出现真实持仓。
2. 周期对账在本地 `trades` open 记录落库前运行。
3. 对账看到“交易所有持仓、本地无 open trade”，误判为未接管持仓。
4. 系统把该币种设为 `enabled=false`。
5. 后续自动保护检查因为币种已停用而跳过。

同时，原 `SL@65.54 qty=0.08` 触发后，交易所剩余 `0.10` 聚合持仓不再有 SL。
用户点击“补止盈止损单”时，历史模板止损价已经高于当前标记价，系统按风控规则拒绝补挂。

## 设计目标

- maker 部分成交在本地 trade 落库前，不再被周期对账误判为外部持仓。
- 任意已成交数量都必须立刻进入保护流程。
- 如果已成交数量太小，交易所不允许挂 reduce-only SL/TP，系统必须平掉本次成交数量。
- SL 是硬保护；SL 未确认时，禁止留下裸仓。
- 已触发但仍有残余持仓的条件单，需要通过交易所条件单历史回填本地订单和交易组状态。
- 前端补单拆分为两类：
  - 历史补单：沿用历史 SL/TP 模板。
  - 接管保护：人工确认后输入或重算新的 SL/TP 触发价。

## 数据结构

新增表：`position_claims`

用途：

```text
在交易所可能出现持仓之前，先声明“本系统正在开仓”。
```

关键字段：

- `symbol`：币种。
- `side`：`long` / `short`。
- `status`：`opening` / `partial` / `filled` / `canceled` / `rejected` / `error`。
- `planned_qty`：计划开仓数量。
- `filled_qty`：最终成交数量。
- `entry_price`：最终成交均价。
- `expires_at_ms`：claim 过期时间。
- `reason` / `raw_json`：审计信息。

该表由 `Store.connect()` 通过 `Base.metadata.create_all` 幂等创建。

同时，`control_commands.arg` 改为文本列，确保 `PROTECT_POSITION` 这类 JSON 命令参数不会被长度截断。

## 开仓 Claim 流程

开仓前：

```text
begin_position_claim(symbol, side, planned_qty)
```

maker-first 成交过程中：

- 交易所可能已经出现持仓。
- 本地 trade 可能尚未落库。
- 周期对账如果看到 active claim，会等待开仓流程完成，不再禁用币种。

开仓结束后：

```text
finish_position_claim(status, filled_qty, entry_price)
```

这样可以覆盖以下窗口：

```text
交易所成交可见 -> 本地 order/trade 落库完成
```

## 部分成交保护失败处理

部分成交后的处理顺序：

1. executor 返回实际成交数量 `result.qty`。
2. engine 用 symbol filters 预校验该数量能否挂 SL/TP。
3. 如果保护单数量或名义价值低于交易所最小限制：
   - 禁用该币种。
   - 对本次成交数量执行 reduce-only `MARKET_TAKER` 平仓。
   - 记录 CLOSE 订单。
   - 发送告警。
4. 如果保护单可挂：
   - 按实际成交数量挂 SL/TP。
   - 如果 SL 未确认，同样进入保护失败平仓流程。

注意：

```text
未成交部分可以撤单；已成交部分不能撤，只能 reduce-only 平仓。
```

## 条件单历史同步

周期对账现在会在持仓仍存在时同步条件单历史：

1. 拉当前 active 条件单。
2. 拉最近条件单历史。
3. 对本地 `orders` 中 `placed/open` 的 SL/TP：
   - 如果不在 active 列表中，但历史明确为 `filled/triggered`，更新为 `filled`。
   - 同步 `filled_qty`、`filled_price`。
   - 刷新对应 `trades` 状态和已实现盈亏。
   - 如果另一侧条件单仍在交易所 active，不直接把本地状态写成 canceled，交给后续陈旧条件单处理。

这样可以处理：

```text
SL 触发减掉系统管理数量，但交易所仍有人工/剩余聚合持仓
```

## 补单模式拆分

### 历史补单

前端按钮：`历史补单`

命令：

```text
REPAIR_SL_TP <SYMBOL>
```

行为：

- 沿用最近本地 SL/TP 模板。
- 触发价必须在当前标记价和开仓价的正确一侧。
- 历史 SL 已经被价格穿过时，继续拒绝补挂。

### 接管保护

前端按钮：`接管保护`

命令：

```text
PROTECT_POSITION <JSON>
```

示例：

```json
{
  "symbol": "SOLUSDT",
  "mode": "manual",
  "qty": 0.1,
  "sl_trigger": 64.5,
  "tp_trigger": 68.0,
  "confirm": true,
  "position": {
    "side": "long",
    "qty": 0.1,
    "entry": 66.34
  }
}
```

后端执行前会重新拉交易所持仓，并校验页面签名：

- 方向是否一致。
- 数量是否一致。
- 开仓价是否一致。

如果页面数据已过期，命令失败并要求刷新后重试。

## 风控规则

接管保护必须满足：

- `confirm=true`。
- 当前交易所存在该币种持仓。
- 接管数量大于 0 且不超过当前持仓数量。
- 当前缺少 SL 时，必须提供新的 `sl_trigger`。
- 多单 SL 必须低于当前标记价和开仓价。
- 空单 SL 必须高于当前标记价和开仓价。
- 理论止损亏损不能超过 `risk.max_loss_per_trade_pct`。
- 数量和触发价必须满足交易所 `minQty/minNotional/tickSize/stepSize`。

如果当前本地 managed 数量与接管数量不一致，系统会创建 `source=takeover` 的 open trade，
后续保护单绑定到该接管交易组。

## 前端变化

当前持仓页保护操作拆分为：

- `历史补单`
  - 快速恢复历史模板。
- `接管保护`
  - 打开弹窗。
  - 展示当前交易所持仓方向、数量、开仓价、标记价。
  - 输入接管数量、SL、TP。
  - 支持“按当前价重算”快速填充。
  - 勾选确认后提交。

当前持仓表新增开仓时间展示：

- 杠杆继续优先使用交易所实时持仓返回值。
- 开仓时间来自本地仍 open/partial 的 trade lot，按最早 open trade 展示。
- 当交易所仓位存在但本地没有 open trade 时，开仓时间显示为空，避免伪造数据。

## 2026-06-08 补充修复

### 缺 SL 处理调整

周期对账发现已管理持仓缺少有效 SL 时，处理策略调整为：

1. 禁用该币种交易。
2. 发送错误告警。
3. 不再自动应急平仓。

这样可以避免系统在用户手动处理持仓时强制市价平仓。
注意：开仓刚成交但保护单完全无法挂出的异常路径，仍保留“关闭本次未受保护成交”的安全处理。

### 接管残余仓位

`PROTECT_POSITION` 不再在本地已有 managed 数量时重复创建完整 takeover trade。

示例：

```text
交易所 live qty = 0.10
本地 managed qty = 0.08
```

旧逻辑会创建 `takeover qty=0.10`，导致本地 trade 重复。
新逻辑只创建残余：

```text
takeover qty = 0.10 - 0.08 = 0.02
```

当保护单覆盖的是聚合仓位数量时，不强行绑定到残余 takeover trade，避免后续平仓只关闭残余 trade。

### 聚合退出分摊

交易所是按 symbol/side 聚合持仓；一张 reduce-only `CLOSE` / `SL` / `TP` 成交可能关闭多个本地 trade lot。

本次新增 FIFO 分摊：

1. 按开仓时间从早到晚找到本地 open/partial trade。
2. 将退出成交数量分摊到这些 trade。
3. 分别更新 `qty_closed`、`realized_pnl`、`status`。

这样可以避免一张 0.10 的平仓单只关闭最新 takeover trade，而旧的 0.08 maker 部分成交继续显示“持仓中”。

### 交易所已空兜底对账

当交易所确认某币种无持仓时：

1. 先同步条件单历史。
2. 将交易所已不在 open 列表里的本地条件单标记终态。
3. 关闭仍悬挂的本地 open/partial trade，并标记 `confidence=inferred`。
4. 取消交易所残留的无持仓 reduce-only 条件单，即使该币种当前已禁用。

这用于修复交易所已 flat、但本地旧 trade 仍 open 的账务残留。

### 现场数据修复

变更前再次备份：

```text
data/backups/trade-testnet-before-flat-reconcile-20260608-153926.db
data/backups/trade-mainnet-before-flat-reconcile-20260608-153926.db
```

已对 `SOLUSDT` 执行一次 flat 兜底对账：

```text
trade_id=20 从 open 修正为 closed
exit_reason=EXCHANGE_FLAT
confidence=inferred
```

该历史 trade 没有可安全归属的独立退出成交单，因此用入场价作为 inferred exit price，避免重复计算已由后续 takeover trade 记录过的聚合平仓盈亏。

服务重启后的首次周期对账还发现 `ETHUSDT` 交易所已 flat，但本地仍有 1 条历史 open trade，因此按同一规则修正为 closed：

```text
ETHUSDT trade_id=4
exit_reason=EXCHANGE_FLAT
confidence=inferred
```

`SOLUSDT` 残留的无持仓 TP 条件单 `1000000099101885` 已由新对账逻辑取消，并在本地标记为 canceled。

## 回滚方案

本次变更前已备份：

```text
data/backups/trade-testnet-before-protection-claim-20260608-141622.db
data/backups/trade-mainnet-before-protection-claim-20260608-141622.db
data/backups/trade-before-protection-claim-20260608-141622.db
```

如需回滚：

1. 停止服务：

```bash
systemctl stop binance-trade.service binance-trade-web.service
```

2. 切回上一提交。
3. 用备份 DB 覆盖当前 DB。
4. 重启服务。

新增 `position_claims` 表不影响旧代码读取旧业务表；但完整回滚仍建议恢复备份 DB。

## 验证

已新增/覆盖测试：

- active opening claim 存在时，周期对账不禁用币种。
- 部分成交太小导致保护单低于最小限制时，系统平掉本次成交数量。
- SL 条件单从 active 消失但历史显示 triggered/filled 时，本地订单和交易组回填。
- `PROTECT_POSITION` 使用人工输入的新 SL 接管剩余持仓。
- 原 `REPAIR_SL_TP` 历史补单语义保持不变。

测试命令：

```bash
.venv/bin/python -m pytest tests/test_store.py tests/test_engine.py tests/test_executor.py tests/test_web_server_protection.py
```

结果：

```text
199 passed, 2 warnings
```
