# 2026-06-18 持仓逐仓保证金实时展示

## 背景

持仓页面会通过行情 WebSocket 实时更新标记价、未实现盈亏和 ROI，但“保证金”直接读取
REST 投影中的 `isolated_margin`，只能在持仓存在时约每 30 秒变化一次。

Binance 私有流 `ACCOUNT_UPDATE` 会提供逐仓钱包 `iw`，但原持仓归一化没有读取
`isolatedWallet`。私有事件还是稀疏结构，写入时会短暂覆盖 REST 投影中的保证金、
杠杆和强平价字段，直到下一次 REST 对账恢复。

## 数据口径

逐仓持仓当前保证金满足：

```text
当前逐仓保证金 = isolatedWallet + 未实现盈亏
```

- `isolatedWallet` 是逐仓钱包基线，由 REST 和私有流提供。
- 未实现盈亏由行情 WebSocket 按实时标记价计算。
- REST 返回的 `isolatedMargin` 继续作为周期性权威校准值。
- 全仓模式或缺少逐仓钱包时，继续回退到交易所投影的保证金字段。

## 改动

- 持仓归一化新增 `isolated_wallet`。
- `position_snapshots` 和 `live_positions` 自动补充同名列。
- `ACCOUNT_UPDATE.iw` 实时更新逐仓钱包，同时保留最近 REST 提供的杠杆、强平价和保证金明细。
- 前端逐仓保证金使用 `isolated_wallet + 实时未实现盈亏` 计算。

## 验证

- 覆盖 REST 与私有流逐仓钱包归一化。
- 覆盖稀疏 `ACCOUNT_UPDATE` 不清空 REST 字段。
- 覆盖旧 SQLite 自动补列及 live position 对外字段。
- 运行全量后端测试和前端生产构建。

## 线上影响

升级时 SQLite 自动增加一列，不修改现有记录。服务启动后的首次 REST 对账会填充当前
持仓的 `isolated_wallet`；此后页面保证金随实时标记价变化，并由周期 REST 对账校准。
