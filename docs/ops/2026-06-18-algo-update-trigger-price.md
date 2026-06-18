# 2026-06-18 条件单私有事件触发价兼容

## 背景

从 Binance iOS 或网页控制台创建 `STOP_MARKET` / `TAKE_PROFIT_MARKET` 条件单后，
前端曾短暂显示“条件单成功挂出 @ 0.00”，等待下一次 REST 对账后才恢复真实触发价。

Binance 私有流 `ALGO_UPDATE` 对条件市价单返回：

- `p=0`：限价字段，条件市价单为 0 属于正常值。
- `tp=<price>`：真实触发价。
- `X=NEW`：条件单已经成功挂出。

原归一化逻辑只识别 `triggerPrice`、`stopPrice` 和旧事件映射中的 `sp`，没有读取
`tp`，导致实时账户投影先写入 `status=placed`、`trigger_price=0`。REST 快照随后提供
`triggerPrice`，所以问题只在私有事件到 REST 对账之间出现。

## 改动

- `ALGO_UPDATE` 映射将 `tp` 作为触发价兼容字段。
- 条件单通用归一化同时支持 `triggerPrice`、`stopPrice` 和 `tp`。
- 不把 `p=0` 当作触发价，也不改变条件单下发、成交或撤销逻辑。

## 验证

- 归一化测试覆盖真实 `ALGO_UPDATE` 字段形态。
- 账户投影测试确认私有事件会立即把 `72.23` 写入运行态和 `live_orders`。
- 全量后端测试和前端生产构建通过后发布。

## 线上影响

该修复只影响条件单触发价的实时展示和账户投影。交易所中的条件单、触发逻辑、
策略交易和数据库 Schema 均不变，不需要数据迁移。
