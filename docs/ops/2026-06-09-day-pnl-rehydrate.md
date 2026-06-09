# 2026-06-09 启动时 day_realized_pnl 从 DB 重算 + 日界改本地时区 + `+0.00` → `0.00`

## 背景

用户报告「重启之后当日已实现盈亏 +0.00」。排查后定位 3 个独立问题：

1. **`day_realized_pnl` 重启归零**：`RuntimeState.day_realized_pnl` 是内存字段
   （`src/state/runtime.py:32`），进程重启后从 0 起步。设计上没人去从 DB `trades.net_realized_pnl`
   重算。后果：日亏熔断（`risk/manager.py:107`）、前端「当日已实现盈亏」（`Dashboard.vue:103`）失真。
2. **日界用 UTC**：现行 `roll_day_if_needed` 用 `time.gmtime()`（`runtime.py:77`），凌晨 0:00 UTC
   （= 08:00 SHA）滚动。SHA 用户视角下「今天亏的钱」在 08:00 之前不计入。权益曲线（DB 里是 SHA
   `created_at`）与 day pnl 日界错开 8 小时。
3. **`+0.00` 误导**：所有 PnL 渲染用 `value >= 0 ? '+' : ''` 三元式，0 值也加 `+` 符号。

## 设计目标

- 启动时从 DB `trades.net_realized_pnl` 按本地日界聚合，重算 `runtime.day_realized_pnl` 和 `day_key`
- 日界改用本地时区（容器 CST = Asia/Shanghai = UTC+8），与权益曲线 / DB 时间戳一致
- 0 值不再加 `+` 前缀，符号仅在正数时显示

## 改动清单

### Repo 层聚合
`src/store/repo.py` — 新增 `day_realized_pnl_by_local_day()`：
- 查 `TradeRow.closed_at_ms > 0` 的所有 trade
- 用 `time.localtime()` 按本地日界生成 `YYYY-MM-DD` 键
- 返回 `{date: sum_net_realized_pnl}` 字典
- 修复 import 顺序：模块顶部 `import time` + `import time as _t`（之前的 import 混乱修掉）

### Runtime 重算
`src/state/runtime.py` — 新增 `rehydrate_day_pnl(by_day, now=None)`：
- 用 `time.localtime()` 算今天的 `day_key`
- 覆盖 `self.day_key` 与 `self.day_realized_pnl = by_day.get(today, 0.0)`
- 不修改 by_day 里非"今天"的项

### 日界改本地时区
`src/state/runtime.py:75-82` — `roll_day_if_needed` 改用 `time.localtime()`，凌晨 0:00（本地）滚动。
与 DB 时间戳 / 权益曲线口径一致。

### 引擎启动 wiring
`src/engine/loop.py:83-99` — `startup` 在 `_restore_decision_snapshots()` 之后、`roll_day_if_needed()`
之前插入 rehydrate：
```python
try:
    by_day = await self._store.day_realized_pnl_by_local_day()
    self.runtime.rehydrate_day_pnl(by_day)
    logger.info("day pnl rehydrated from db: day={} pnl={:.4f} (history days={})", ...)
except Exception as e:
    logger.warning("day pnl rehydrate failed, fallback to 0: {}", e)
    self.runtime.roll_day_if_needed()
```
- 失败兜底到 `roll_day_if_needed` 路径，不影响其它启动流程
- 启动日志多了 `day pnl rehydrated from db: day=YYYY-MM-DD pnl=...` 一行，方便排查

### 前端 0 不带 `+` 前缀
- `web/frontend/src/views/Dashboard.vue:103` — `dayPnl >= 0 ? '+' : ''` → `dayPnl > 0 ? '+' : ''`
- `web/frontend/src/views/Dashboard.vue:102` — class 同理改为三分支（pos / neg / 空）
- `web/frontend/src/views/Pnl.vue:19` — `pnlClass` 三分支
- `web/frontend/src/views/Chart.vue:305` — `change24h >= 0 ? '+' : ''` → `> 0`
- `web/frontend/src/views/Positions.vue:67` — `fmtPct` 三分支

## 单元测试

- `tests/test_state.py` 新增 4 个：rehydrate 设置 day_key / by_day 缺今日回退 0 / by_day 为空仅初始化 / roll 用本地时区
- `tests/test_store.py` 新增 2 个：聚合只统计已平仓 / 聚合空表返空字典
- `tests/test_engine.py` 新增 2 个：启动 rehydrate 从 DB 拉 / store 异常不崩

总计 253 passed (14 新增)，无回归。

## 兼容性

- DB schema 不变
- config.yaml 无变更
- `runtime.roll_day_if_needed` 的旧测试 `test_day_roll_resets_pnl` / `test_day_roll_noop_same_day` 仍通过
  （用 `now=0`，UTC 与 CST 仍同一天，key 都是 `1970-01-01`）
- 已有 `balance_snapshots` 历史行 `day_realized_pnl` 不回填（只影响启动那一刻的内存值；之后新的
  snapshot 会写正确值）

## 上线效果

测试网 2026-06-09 12:57 SHA 重启后：
- 启动日志：`day pnl rehydrated from db: day=2026-06-09 pnl=0.0000 (history days=4)`
- 前端 Dashboard「当日已实现盈亏」：`0.00`（之前是 `+0.00`）
- 真正的 "今天亏了 1.5" 场景会在重启后立即呈现，不再因重启清零

## 已知限制

- 只重算"今天"。历史日的累计丢失（之前本来也没存）；如有需要可以再写一个 day_pnl_by_day 端点
- 时区完全依赖容器 `TZ` 设置（当前 CST）。若以后部署到其它时区需确认 `time.localtime()` 与用户预期一致
- 启动重算只读 `trades.net_realized_pnl`；未考虑 funding fee / 跨日 funding 结算等（与原行为一致）
