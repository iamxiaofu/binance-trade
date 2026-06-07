# 2026-06-07 交易记录按交易组聚合展示

## 背景

Web 交易记录页原来直接展示 `orders` 表流水。

这种展示方式能完整保留下单事件，但不利于复盘一笔合约交易的完整生命周期：

- 开空、平空、空单止盈、空单止损分散在多行。
- 用户需要手动按币种、方向、数量和时间判断哪些订单属于同一笔交易。
- 页面只展示名义价值，缺少保证金、杠杆、已实现盈亏和保证金收益率。
- SL/TP 条件单与最终退出单之间缺少明确聚合视图。

合约交易所通常会把展示拆成两层：

- 订单流水：每一次下单、挂条件单、撤单、成交都是独立订单。
- 仓位/交易记录：按一次开仓到退出的生命周期展示方向、保证金、杠杆和盈亏。

因此本次新增“交易组”模型：保留订单流水，同时在上层提供按仓位生命周期聚合后的交易汇总。

## 设计目标

- 不删除、不重写现有 `orders` 原始流水。
- 新增 `trades` 表作为聚合视图的持久化来源。
- 新订单实时归组，历史订单自动回填归组。
- 页面默认展示交易汇总，保留订单流水和风控拒单入口。
- 展示名义价值、保证金、杠杆、已实现盈亏、保证金收益率、退出原因。
- 兼容旧数据库，服务启动时自动补表和补列。

## 概念边界

“同一订单”在交易所语义里不准确。

开仓单、止盈条件单、止损条件单、平仓单都是不同订单。

本次引入的聚合对象叫“交易组 / 仓位生命周期”，含义是：

1. 一条 `OPEN` 成交订单创建一笔交易组。
2. 后续同币种、同方向、同数量的 `SL` / `TP` 保护条件单挂到该交易组。
3. 后续 `CLOSE` 或触发成交的 `SL` / `TP` 关闭该交易组。
4. `orders` 仍保留每个原始事件，用于审计和排查。

## 数据结构

新增表：`trades`

核心字段：

- `symbol`：币种。
- `direction`：`long` / `short`。
- `status`：`open` / `closed` / `partial` / `unmatched`。
- `dry_run`：模拟或真实。
- `opened_at_ms` / `closed_at_ms`：开仓和平仓时间。
- `entry_order_id` / `exit_order_id`：开仓订单和退出订单的本地订单 id。
- `entry_price` / `exit_price`：开仓价和退出价。
- `qty_opened` / `qty_closed`：开仓和平仓数量。
- `leverage`：杠杆倍数。
- `entry_notional` / `exit_notional`：开仓和平仓名义价值。
- `entry_margin`：开仓保证金，按 `entry_notional / leverage` 估算。
- `realized_pnl`：已实现盈亏，未计手续费和资金费。
- `pnl_pct_on_margin`：保证金收益率，按 `realized_pnl / entry_margin * 100`。
- `exit_reason`：`CLOSE` / `TP` / `SL` / `EMERGENCY` / `CIRCUIT` / `UNKNOWN`。
- `source`：`live` / `backfill`。
- `confidence`：`exact` / `inferred` / `unmatched`。

扩展表：`orders`

新增字段：

- `trade_id`：所属交易组 id。
- `trade_role`：`ENTRY` / `PROTECTION_SL` / `PROTECTION_TP` / `EXIT`。
- `leverage`：订单关联杠杆。
- `margin`：订单关联保证金估算。
- `realized_pnl`：退出订单对应已实现盈亏。

## 迁移策略

项目当前没有 Alembic 迁移框架，原逻辑只使用 `Base.metadata.create_all`。

`create_all` 可以创建新表，但不会给已有表自动增加字段。

因此本次在 `Store.connect()` 中加入轻量 SQLite 幂等迁移：

1. 执行 `Base.metadata.create_all` 创建新 `trades` 表。
2. 通过 `PRAGMA table_info(orders)` 检查 `orders` 已有列。
3. 对缺失列执行 `ALTER TABLE orders ADD COLUMN ... DEFAULT ...`。
4. 执行历史订单回填。

该流程可重复执行，服务多次启动不会重复加列，也不会重复创建已归组交易。

## 实时归组规则

新订单通过 `Store.log_order()` 落库时实时归组：

- `OPEN + filled/partial/dry_run`
  - 创建 `trades`。
  - `buy` 视为 `long`，`sell` 视为 `short`。
  - 记录开仓价、数量、名义价值、杠杆、保证金。
  - 订单标记为 `ENTRY`。

- `SL` / `TP`
  - 优先使用传入的 `trade_id`。
  - 没有 `trade_id` 时查找同币种、同方向、同 dry_run、数量相近的未关闭交易。
  - 订单标记为 `PROTECTION_SL` 或 `PROTECTION_TP`。
  - 仅真实 `filled/partial` 的条件单会关闭交易组。
  - `dry_run` 的 SL/TP 只作为模拟保护单挂入，不关闭交易组。

- `CLOSE + filled/partial/dry_run`
  - 查找同币种、同方向、同 dry_run、数量相近的未关闭交易。
  - 订单标记为 `EXIT`。
  - 关闭交易组并计算盈亏。

## 盈亏计算

本次计算的是毛盈亏，未计手续费和资金费。

多单：

```text
realized_pnl = (exit_price - entry_price) * qty
```

空单：

```text
realized_pnl = (entry_price - exit_price) * qty
```

保证金：

```text
entry_margin = entry_notional / leverage
```

保证金收益率：

```text
pnl_pct_on_margin = realized_pnl / entry_margin * 100
```

对于 SL/TP 触发，优先使用 `raw_json.filled_price` 作为退出价；
没有该字段时退回订单 `price`。

## 历史回填规则

历史回填只处理 `trade_id=0` 的订单，按 `ts_ms, id` 升序扫描。

规则：

1. 遇到 `OPEN` 且状态为 `filled/partial/dry_run`，创建交易组。
2. 遇到 `SL/TP`，优先找同币种、同方向、同 dry_run、数量相近的未关闭交易。
3. 若该保护单已经是取消状态，且当前没有未关闭交易，则允许挂到最近同币种、同方向、同 dry_run、数量相近的已关闭交易，用于补全展开明细。
4. 遇到 `CLOSE` 且状态为 `filled/partial/dry_run`，关闭匹配到的未关闭交易。
5. 杠杆优先读取订单自身字段；历史订单缺失时，从开仓前最近一条同币种 `OPEN_LONG/OPEN_SHORT` 决策日志推断。
6. 回填生成的交易组标记 `source=backfill`、`confidence=inferred`。

在当前生产库副本上验证：

- 历史订单数：69
- 回填交易组：18
- 已平仓交易组：14
- 未关闭交易组：4
- 未归组订单：0

## API

新增接口：

```text
GET /api/trades
```

查询参数：

- `symbol`：可重复传入，按币种筛选。
- `direction`：可重复传入，支持 `long`、`short`。
- `status`：可重复传入，支持 `open`、`closed`、`partial`、`unmatched`。
- `exit_reason`：可重复传入，支持 `CLOSE`、`TP`、`SL`、`EMERGENCY`、`CIRCUIT`、`UNKNOWN`。
- `start_ts_ms`：按开仓时间筛选，毫秒 epoch。
- `end_ts_ms`：按开仓时间筛选，毫秒 epoch。
- `limit`：每页条数，最大 500。
- `offset`：分页偏移量。

响应结构：

```json
{
  "items": [
    {
      "id": 1,
      "symbol": "ETHUSDT",
      "direction": "short",
      "status": "closed",
      "entry_price": 1571.62,
      "exit_price": 1583.02,
      "qty_opened": 2.052,
      "leverage": 5,
      "entry_notional": 3224.96,
      "entry_margin": 644.99,
      "realized_pnl": -23.39,
      "pnl_pct_on_margin": -3.63,
      "exit_reason": "CLOSE",
      "orders": []
    }
  ],
  "total": 0,
  "limit": 100,
  "offset": 0
}
```

## 页面行为

交易记录页改为三个视图：

- 交易汇总
- 订单流水
- 风控拒单

默认展示“交易汇总”。

交易汇总支持：

- 币种筛选。
- 方向筛选。
- 状态筛选。
- 退出原因筛选。
- 开仓时间范围筛选。
- 服务端分页。
- 展开行查看该交易组下的开仓、SL、TP、平仓订单。

订单流水仍展示原始订单事件，并新增：

- 保证金。
- 杠杆。

## 涉及文件

- `src/store/models.py`
- `src/store/repo.py`
- `src/execution/executor.py`
- `src/engine/loop.py`
- `web/status.py`
- `web/server.py`
- `web/frontend/src/api.js`
- `web/frontend/src/labels.js`
- `web/frontend/src/views/Orders.vue`
- `tests/test_store.py`
- `tests/test_web_status.py`

## 验证

执行：

```bash
.venv/bin/python -m pytest
cd web/frontend
npm run build
```

结果：

```text
166 passed
✓ built
```

前端构建仍会出现 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
与既有构建日志一致，不影响产物生成。

## 兼容性

- 旧 `orders` 表不会被删除。
- 旧 `/api/orders` 保持可用。
- 新 `trades` 表由启动时自动创建。
- `orders` 新增列均有默认值，旧记录可自动补齐。
- 历史回填只处理 `trade_id=0` 的记录，重复执行不会重复生成交易组。
- 回填结果使用 `source=backfill` 和 `confidence=inferred` 标识推断来源。

## 已知边界

- 当前模型按“一次开仓、一次退出、两条保护条件单”设计。
- 部分成交、多次加仓、多次减仓后续需要更完整的仓位账本支持。
- 盈亏未计手续费和资金费，因此不能等同于交易所最终净收益。
- 历史杠杆来自最近开仓决策推断，若历史决策缺失则显示为 0 或空值。
- 交易所手工操作产生的订单，如果没有本地 `OPEN` 记录，可能无法完整归组。

## 运维注意

- 本次包含 DB schema 扩展和 Web 后端接口变更。
- 部署后需要重启交易主进程和 Web 进程，确保两边都执行 `Store.connect()` 的迁移和回填。
- 重启前建议备份 `data/trade.db`。
- Web 进程未重启时，前端会请求不到 `/api/trades`，交易汇总页会报错。
- 已打开的浏览器页面需要刷新，才能加载新的前端 chunk。
