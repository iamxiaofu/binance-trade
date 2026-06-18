# 2026-06-18 Binance 外部/手工交易同步

## 背景

Engine 原先只把自身执行器产生的订单写入 `orders/trades`。Binance 网页、手机端或
其他 API 客户端产生的仓位虽然会通过私有流出现在实时持仓中，但平仓后不会留下本地
交易生命周期，形成审计缺口。

## 架构

外部成交使用独立存储，不修改现有策略交易：

- `exchange_fills`：Binance 权威成交账本，按 `(symbol, exchange_trade_id)` 幂等。
- `external_trades`：纯外部仓位生命周期。
- `external_trade_fills`：成交与外部生命周期的分配关系，支持加仓、部分平仓和反手。

`orders/trades` 继续只保存 Engine 策略交易。外部记录不会参与 LLM 复盘、策略胜率、
策略日盈亏或 SL/TP 自动修复。

## 归属判断

成交按以下优先级分类：

1. `client_order_id` 以 `bt-` 开头：`engine`。
2. Binance order id 或 client id 匹配本地 `orders`：`engine`。
3. 成交时间位于本地 `position_claims` 有效期：匹配 client id 时为 `engine`，
   否则暂记 `unknown`。
4. 成交时间位于本地策略 trade 生命周期：`mixed`。
5. 以上均不匹配：`external`。

`mixed/unknown` 只进入权威成交账本，不生成外部交易，也不修改策略交易。前端交易记录
页会显示待确认数量。

该分类只能证明订单是否由本 Engine 创建，不能百分之百区分 Binance 控制台和其他
API 程序，因此界面名称为“Binance 外部/手工交易”。

## 同步

- 私有流：`ORDER_TRADE_UPDATE` 且 `x=TRADE` 时立即写入。
- REST 补偿：启动及周期对账时调用 `fetch_my_trades`，每分钟最多执行一次，并回看
  最近一分钟以覆盖断流边界；唯一约束负责去重。
- Engine 停止期间不实时同步；重启后从本地成交水位继续补偿。

外部仓位仍执行 `UNMANAGED_LIVE_POSITION` 安全策略：禁用该币种的新开仓，但 Engine
不接管、不补保护单、不主动平仓。

## 30 天回填

先执行 dry-run：

```bash
.venv/bin/python main.py -c config.mainnet.yaml -e /etc/binance-trade/mainnet.env \
  external-backfill --days 30
```

确认 `engine/external/mixed/unknown/duplicates` 统计后正式导入：

```bash
.venv/bin/python main.py -c config.mainnet.yaml -e /etc/binance-trade/mainnet.env \
  external-backfill --days 30 --apply
```

主网 apply 要求输入 `MAINNET`。脚本按小于 Binance 七天限制的窗口分页，最大允许
90天。dry-run 不写成交或交易业务数据。

如果回填窗口内第一笔成交带有 `realizedPnl` 或 `reduceOnly`，说明仓位可能在窗口开始
前已经存在。该记录标为 `carry_in`：

- 保留 Binance 已实现盈亏和手续费；
- 入场价、保证金收益率保持未知；
- 不根据第一笔平仓成交反向伪造新仓。

## 前端

交易记录支持按来源筛选：

- Engine 策略交易
- Binance 外部/手工交易

外部交易展开后显示真实 Binance 成交分配。页面明确提示此类仓位仅同步归档，Engine
未接管止盈止损或主动平仓。

## 验证

- Engine 成交不会生成外部交易。
- 重复私有流/REST 成交只保存一次。
- 外部开仓、加仓、部分平仓、完全平仓和反手正确聚合。
- 策略仓位期间的人工成交只标记 `mixed`。
- carry-in 不伪造入场价和收益率。
- 原 `orders/trades` 行数和内容不被外部同步修改。
- 后端测试与前端生产构建通过。
