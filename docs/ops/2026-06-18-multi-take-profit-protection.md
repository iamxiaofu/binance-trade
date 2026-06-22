# 2026-06-18 分批止盈与保护单控制权

## 背景

Binance 控制台可以给同一持仓挂多张部分数量的 `TAKE_PROFIT_MARKET`。
原系统虽然能通过账户投影同步这些条件单，但持仓卡片只选择第一张 TP，
LLM 和执行层也只有单值 `take_profit_pct`。

更严重的是，原保护校验要求每一张 SL/TP 的数量都等于整仓数量。外部创建的
分批 TP 会被判定为陈旧条件单；仓位一旦进入 Engine 管理流程，存在误撤风险。

## 数据模型

条件单归一化新增：

- `close_position`：识别 Binance Close-All 条件单。此类订单允许数量为零。
- `origin`：根据 client order ID 判定订单归属。
  - `bt-` 前缀：`ENGINE`
  - 其他或空值：`EXTERNAL`

实时订单表 `live_orders` 新增对应列。既有 SQLite 数据库由 `Store.connect()`
幂等补列，随后 REST 账户重同步会刷新真实值。

持仓保护投影不再只有单个 `sl/tp`，同时输出：

- `sl_orders[]`
- `tp_orders[]`
- `tp_ordered_qty`
- `tp_covered_qty`
- `tp_coverage_pct`
- `runner_qty`
- `authority`
- `mode`
- `status`
- `conflicts[]`

旧的 `protection.sl` 和 `protection.tp` 暂时保留，供旧前端或接口调用兼容。

## 保护控制权

订单同步通道 `rest/stream` 不能代表订单归属。系统只根据 client ID 判定控制权。

- `OBSERVE`：全部保护单来自 Binance 外部。Engine 只展示和告警，不自动撤销。
- `ENGINE`：全部保护单由本 Engine 创建，可以按策略调整。
- `MIXED`：外部与 Engine 条件单同时存在。自动调整被阻止，要求人工处理。

`_stale_protection_orders` 只返回 Engine 订单。外部条件单即使数量或价格与当前仓位
不一致，也不会进入自动撤销路径。

LLM 返回 `ADJUST_SLTP` 时，如果检测到 EXTERNAL/MIXED 保护单，决策会记录为
`SLTP_BLOCKED`，不会新增或替换条件单。

## 人工接管

持仓页“接管保护”支持最多三档 TP，每档填写：

- 触发价
- 当前持仓比例

如果当前保护来自外部，提交参数包含 `replace_external=true`。引擎执行顺序：

1. 重新拉取并校验交易所持仓签名。
2. 校验 SL、每档 TP、比例合计、minQty/minNotional 和价格方向。
3. 先挂出新的 Engine SL/TP。
4. 确认新 SL 成功后，逐张撤销原外部条件单。
5. 旧单撤销失败时禁用该币种新开仓并返回明确错误。

该顺序避免先撤旧单导致裸仓。

## LLM 决策协议 V2

`TradeDecision` 新增：

```json
{
  "take_profit_pct": 0,
  "take_profit_targets": [
    {
      "leg_id": "TP1",
      "price_distance_pct": 0.02,
      "position_pct": 0.5
    },
    {
      "leg_id": "TP2",
      "price_distance_pct": 0.04,
      "position_pct": 0.5
    }
  ]
}
```

约束：

- 最多三档。
- 仓位比例合计不得超过 `1`。
- 距离必须从近到远严格递增。
- 新数组与旧 `take_profit_pct` 互斥。
- 旧单目标格式继续转换为一档 100% TP。

执行层使用 `ProtectionOrderSpec` 为每档保存独立数量、触发价、比例和 leg ID。
每档数量按 stepSize 向下规整；当比例合计为 100% 时，最后一档吸收可交易余量。

决策表新增：

- `decision_schema_version`
- `take_profit_plan_json`

决策日志页面会展示 V1/V2 及完整分批计划。

## 集合级告警

周期对账新增保护集合检查：

- 多张 SL
- TP 总数量超过当前仓位
- TP 只覆盖部分仓位
- Engine/External 混合控制权

状态发生变化时记录 `PROTECTION_ALERT` 审计并发送错误通知。相同状态不会在每个
对账周期重复告警。

## 验证

- Close-All SL 数量为零时仍被识别为有效保护。
- 两张部分数量 TP 按集合计算覆盖率和 runner。
- 外部 TP 不进入自动陈旧单撤销列表。
- 人工接管先挂新单再撤外部单。
- LLM V1 单 TP 保持兼容。
- LLM V2 多 TP 校验、落库和分腿下单通过。
- 相关后端测试及前端生产构建通过。

## 发布注意

- 本变更包含 SQLite 幂等迁移，必须先停止对应环境服务再更新发布副本。
- 启动后确认 REST resync 已刷新 `live_orders.close_position/origin`。
- 主网重新启用当前外部仓位前，先在持仓卡片确认两张 TP、覆盖率和 `OBSERVE` 状态。
- 不要直接对现有外部保护执行 `REPAIR_SL_TP`；需要切换控制权时使用“接管保护”。
