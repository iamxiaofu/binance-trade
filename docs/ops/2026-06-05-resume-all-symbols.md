# 2026-06-05 开启策略并启用全部币种

## 背景

全局 `RESUME` 只恢复 `strategy.paused=false`，不会自动恢复
`symbol.enabled.*`。在残留条件单和保护单异常处理后，用户容易误以为点击
“恢复策略”会同时开启所有币种，实际仍会出现 `skip-llm: symbol disabled`。

## 改造内容

- 新增交易主进程命令：`RESUME_ALL_SYMBOLS`。
- Web 操作面板新增按钮：“开启策略并启用全部币种”。
- 执行前前端弹确认框，明确该操作会恢复全局策略并启用全部配置币种。
- Web 端仍只写命令队列，不直接访问交易所。

## 交易所预检查

`RESUME_ALL_SYMBOLS` 在交易主进程串行执行，并先严格检查全部配置币种：

1. `fetch_positions(settings.symbols)` 不允许存在任何 live 持仓。
2. `fetch_open_orders(symbol)` 不允许存在普通未完成挂单。
3. `fetch_open_condition_orders(symbol)` 不允许存在 live 条件单。

任一检查失败，命令会标记为 `failed`，并保持当前状态：

- 不修改 `strategy.paused`。
- 不修改任何 `symbol.enabled.*`。
- 在命令结果里写明阻塞项，例如持仓、普通挂单、条件单。

## 通过后的状态

预检查全部通过后，交易主进程在同一事务中写入：

- `strategy.paused=false`
- `symbol.enabled.<全部配置币种>=true`

随后更新进程内状态，并唤醒下一轮策略，让恢复操作尽快进入 LLM 决策。

## 命令消费与页面刷新

- 交易主循环在策略周期睡眠期间每秒检查一次命令队列。
- `RESUME_ALL_SYMBOLS` 执行成功后会立即结束睡眠，触发下一轮策略周期。
- 操作面板的命令历史会合并实时 summary 推送，pending/done/failed 状态不再依赖手动刷新。

## 运维注意

- 该改造需要重启交易主进程和 Web 进程后生效。
- 若交易所 API 查询失败，命令会失败，不会开启交易。
- 若只想恢复全局策略，不启用币种，仍使用原 `RESUME`。
- 若只想开启单个币种，仍使用 `SET_SYMBOL_ENABLED SYMBOL=true`。
