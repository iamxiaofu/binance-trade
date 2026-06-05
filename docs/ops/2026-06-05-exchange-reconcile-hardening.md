# 2026-06-05 交易所对账与条件单风控改造记录

## 背景

2026-06-05 盘中复盘发现三类状态不一致：

- Binance testnet 交易所侧存在历史 reduceOnly 条件单残留，本地 DB 曾误判为已取消。
- ETH 开仓后 SL/TP 条件单接口持续返回 `-1007`，短时间形成裸仓风险。
- 服务处于 `strategy.paused=true` 后，页面显示“无持仓”，但账户权益和可用保证金仍来自旧余额快照。

本次改造目标是让交易所实时状态成为最终真相，DB/runtime/web 只作为镜像与审计记录，并通过周期性对账避免状态漂移。

## 现场现象

- BTCUSDT / BNBUSDT 曾残留 4 张 reduceOnly 条件单。
- 官方 REST 条件单取消接口出现不一致：
  - `DELETE /fapi/v1/algoOrder` 使用 `algoId` 或 `clientAlgoId` 返回 `-2011 Unknown order sent`。
  - `DELETE /fapi/v1/algoOpenOrders` 按 symbol 返回 `200`，但短时间内查询仍显示 open。
- 重启服务验证时，ETHUSDT 被 LLM 新开空单，随后 SL/TP 下发均返回 `-1007`。
- 手工 reduceOnly 平掉 ETH 后，`balance_snapshots` 最新值仍停留在 ETH 持仓存在时。

## 根因

1. 条件单生命周期没有完整对账。

   旧逻辑只在启动和部分流程中查询普通 open orders，条件单没有作为一等状态持续同步。

2. paused 状态跳过快照。

   `_check_circuit_breaker()` 在 `runtime.halt_new_entries=true` 时返回 `True`，主循环直接 `return`，导致 `_snapshot()` 不执行。

3. 启动时只更新 runtime equity，不写余额快照。

   Web 看板读取 `balance_snapshots` 最新行，因此会显示旧的 available margin。

4. SL/TP 下发后的失败处理不足。

   条件单接口返回 `-1007` 时，系统需要查询交易所确认是否实际创建；如果 SL 无法确认，不能继续持有裸仓。

## 风险处理过程

- 停止交易服务，避免自动动作继续发生。
- 查询实时持仓、普通挂单、条件单。
- 尝试用 ccxt 和手写签名 REST 取消残留条件单。
- API 短时间内无法确认删除时，将对应 symbol 禁用，避免复用残留条件单。
- ETH 裸仓出现后：
  - 停止服务。
  - 尝试补挂 ETH SL/TP，仍返回 `-1007`。
  - 使用 reduceOnly 市价平掉 ETH。
  - 设置 `strategy.paused=true`，禁用 BTCUSDT/ETHUSDT/BNBUSDT。

后续复查显示交易所实时条件单为空，旧条件单在本地 DB 中已同步为 `canceled`。

## 本次代码改造

### 启动与 paused 快照

- 启动时拉取余额后，立即写入 `balance_snapshots`。
- 主循环即使因 paused/熔断跳过策略，也会先执行 `_snapshot()`。
- 解决“零持仓 + 旧可用保证金”的 Web 展示错配。

### 独立后台对账

新增交易所对账循环：

- 空仓时每 30 秒同步一次。
- 有持仓时每 15 秒同步一次。
- 同步内容：
  - 账户余额
  - 持仓
  - 普通 open orders
  - 条件单 / algo orders

该任务独立于 LLM 策略循环，paused 时也继续运行。

### 条件单不变量

对每个 symbol 检查：

- 无持仓时，不应存在 live reduceOnly 条件单。
- 有持仓时，SL 必须存在且匹配当前仓位。
- 保护单必须满足：
  - `reduceOnly=true`
  - side 为平仓方向
  - qty 与当前仓位匹配
  - SL/TP 触发价方向正确

如果发现残留条件单：

- 尝试取消。
- 复查仍存在则禁用 symbol。
- 本地 DB 不再盲目标记 canceled，必须以交易所复查为准。

### SL 缺失应急平仓

OPEN 成交后，如果策略要求 SL，但 SL 没有确认 `placed/dry_run`：

- 禁用 symbol。
- 立即查询实时持仓。
- 使用 reduceOnly 市价平仓。
- 写入 CLOSE 订单记录并记录估算 PnL。

### 本地订单状态同步

新增 `Store.mark_symbol_conditions_not_live()`：

- 无持仓对账时，如果交易所 live 条件单列表里没有某个本地 `placed/open` 条件单，则将本地状态更新为 `canceled`。
- 避免 Web 和后续逻辑继续认为旧条件单还有效。

## 涉及文件

- `src/engine/loop.py`
- `src/store/repo.py`
- `tests/test_engine.py`
- `tests/test_store.py`

本次改造基于前一提交 `fe8b655 修复条件单保护与残留风控` 继续增强。

## 验证

执行：

```bash
.venv/bin/python -m pytest
```

结果：

```text
151 passed
```

线上验证：

- `binance-trade.service` 为 `active`。
- 当前运行态：
  - `strategy.paused=true`
  - `symbol.enabled.BTCUSDT=false`
  - `symbol.enabled.ETHUSDT=false`
  - `symbol.enabled.BNBUSDT=false`
- 最新余额快照：
  - `total_equity=4908.01519267`
  - `available_margin=4908.01519267`
- 实时交易所持仓为空。
- 实时交易所条件单为空。
- 旧 BTC/BNB 条件单本地状态已同步为 `canceled`。

## 后续注意事项

- 恢复交易前必须显式恢复 `strategy.paused=false` 和目标 symbol enabled。
- Binance testnet 仍可能出现 `-1000/-1007/-2011` 等异常，系统应继续以复查结果为准。
- Web 页面展示当前值时，应优先关注快照时间；快照过期时不能当作实时账户状态。
- DB 是审计记录，不应被视为交易所实时状态的最终真相。

