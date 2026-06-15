# 2026-06-15 Binance ACCOUNT_CONFIG_UPDATE 私有事件分类修复

## 背景

主网私有 User Data Stream 收到：

```json
{
  "e": "ACCOUNT_CONFIG_UPDATE",
  "ac": {
    "s": "BTCUSDT",
    "l": 3
  }
}
```

旧逻辑把所有 `ACCOUNT_CONFIG_UPDATE` 与条件单触发拒绝视为同等级未知高风险事件，
立即全局暂停策略并写入：

```text
strategy.pause.reason_code=ACCOUNT_CONFIG_UPDATE
strategy.pause.reason=Binance private event requires review: ACCOUNT_CONFIG_UPDATE
```

这是过度保守的误分类。Binance 会在交易对杠杆改变时发送该事件，而 engine 的正常开仓流程
也会在下单前调用 `set_leverage`，因此正常操作可能反复误触发全局暂停。

## Binance 事件语义

`ACCOUNT_CONFIG_UPDATE` 有两种已知结构：

- `ac.s` + `ac.l`：交易对杠杆配置更新。
- `ai.j`：Multi-Assets Margin Mode 更新。

该事件表示账户配置变化，不代表订单成交、持仓变化或私有流异常。

## 新处理策略

| 事件 | 处理 |
|---|---|
| 杠杆 `1..risk.max_leverage` | 记录事件、记录 INFO、触发 REST 对账，不暂停 |
| 杠杆超过 `risk.max_leverage` | 禁用对应币种、全局暂停、告警、REST 对账 |
| `ai.j=false` | 确认 Multi-Assets 已关闭、REST 对账，不暂停 |
| `ai.j=true` | 不支持的账户模式，全局暂停、告警、REST 对账 |
| 未知/无法解析结构 | fail-closed 全局暂停、告警、REST 对账 |

`CONDITIONAL_ORDER_TRIGGER_REJECT` 仍保留原有高风险行为：暂停、禁用对应币种并进入保护修复流程。

## 安全边界

- 合规杠杆更新不会自动恢复已暂停策略。
- 超限杠杆会使用独立原因码 `ACCOUNT_CONFIG_LEVERAGE_EXCEEDED`。
- Multi-Assets 开启会使用独立原因码 `ACCOUNT_CONFIG_MULTI_ASSETS_ENABLED`。
- 未知结构会使用 `ACCOUNT_CONFIG_UPDATE_UNKNOWN`。
- 所有事件仍完整写入 `exchange_events`，并触发 REST 对账与不变量检查。

## 本次事件结论

- 环境：mainnet
- 币种：BTCUSDT
- 新杠杆：`3x`
- 硬上限：`5x`
- 持仓：0
- 普通挂单：0
- 条件单：0

该事件属于安全杠杆配置更新，不应暂停策略。旧版本已因此暂停 mainnet；部署修复并重启后，
mainnet 会按既有安全设计进入 `MAINNET_RESTART_GUARD`，仍需人工确认后恢复。

## 验证

- 新增 engine 单测：
  - 合规杠杆更新不暂停
  - 超限杠杆暂停并禁用币种
  - Multi-Assets 开关分类
  - 未知账户配置 fail-closed
- 针对性测试：`4 passed`
- 全量测试：`343 passed`
