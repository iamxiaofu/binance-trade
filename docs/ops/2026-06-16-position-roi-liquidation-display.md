# 2026-06-16 持仓 ROI 与强平价格展示修复

## 背景

持仓页 `投资回报率` 和 `强平价格` 显示为 `—`。

排查后确认：

- 前端读取字段为 `roi_pct` 和 `liquidation_price`，字段名正确。
- Binance REST 持仓快照原始数据包含 `initialMargin`、`isolatedMargin`、`liquidationPrice`。
- `normalize_position()` 已经能解析这些字段并计算 ROI。
- 但 `live_positions` 和 `position_snapshots` 表只保存基础字段，导致 `/api/positions` 和
  `/api/summary.positions` 没有返回 ROI、强平价格和保证金字段。

私有流 `ACCOUNT_UPDATE` 通常只带 `s/pa/ep/up/ps`，不包含强平价和完整保证金字段。
因此强平价展示必须依赖 REST 账户快照补全，不能仅靠私有流。

## 改动

### 持仓投影字段扩展

`live_positions` 和 `position_snapshots` 新增字段：

- `initial_margin`
- `isolated_margin`
- `maintenance_margin`
- `roi_pct`
- `liquidation_price`
- `margin_ratio`
- `margin_mode`

启动时沿用现有 SQLite 轻量迁移机制自动补列，兼容既有 testnet/mainnet 数据库。

### 写入逻辑

`upsert_live_position()` 和 `snapshot_positions()` 现在写入 `normalize_position()` 解析出的完整持仓字段。

REST 账户快照提供完整字段时，持仓页会展示：

- ROI：优先交易所/ccxt `percentage`，否则 `unrealized_pnl / initial_margin * 100`
- 强平价格：交易所 REST 返回的 `liquidationPrice`

私有流增量事件不带强平价时，仍只更新其可提供字段；周期 REST 对账会刷新完整字段。

## 线上影响

- API 返回字段增加，前端无需改字段名。
- 已有数据库会在服务启动时自动补列。
- 当前持仓的 ROI/强平价格会在下一次 REST_ACCOUNT_SNAPSHOT 后显示；重启 engine 会触发启动对账并立即补全。
- 不改变交易风控逻辑，不使用本地估算强平价格替代交易所返回值。

## 验证

- `upsert_live_position()` 返回 ROI、强平价格、保证金字段。
- `snapshot_positions()` 和 `latest_positions()` fallback 路径返回 ROI、强平价格。
- 旧 SQLite 表自动补列。
- 针对性测试：`5 passed`。
