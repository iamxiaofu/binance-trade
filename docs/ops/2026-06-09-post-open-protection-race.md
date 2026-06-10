# 2026-06-09 开仓后保护单确认 race 修复

## 背景

SOLUSDT 在 `2026-06-09 17:37:26 CST` 出现开空后同秒平仓：

- 市价开空成交：`sell 38.66 @ 66.10`
- 挂 SL/TP 前预检查返回：`SOLUSDT 交易所侧已无持仓，跳过 SL/TP`
- 系统按“无法确认保护单”的安全策略立即 reduce-only 平仓

同一时间段 BTCUSDT 也出现过类似保护失败路径，并因普通市价平仓滑点护栏被拒，留下需要后续处理的裸仓风险。

## 现场现象

- LLM 决策不是主动平仓，SOLUSDT 的平仓原因来自执行层保护失败。
- `OPEN filled` 后立即调用 `fetch_positions`，交易所侧短时间未返回新仓位。
- 随后的 reduce-only close 能成交，说明开仓实际存在，前一次 `fetch_positions` 更像是短暂一致性延迟或接口刷新 race。
- symbol 被设置为 disabled 后，原逻辑会跳过周期性保护不变量检查，导致已有 live position 无法继续走自动保护修复。

## 根因

1. 开仓成交后，`_precheck_before_attach_sl_tp()` 只查一次交易所持仓；一旦短暂查不到，就直接判定无法挂保护单。
2. `_should_enforce_position_protection()` 在 symbol disabled 时直接返回，导致“禁开仓”和“保护已有仓位”被混在一起。
3. 已管理仓位在周期对账发现 SL 缺失时，原逻辑只禁用 symbol，不会主动复用已有 `_repair_sl_tp()` 修复。

## 代码改造

1. 增加 `_POST_OPEN_POSITION_CONFIRM_DELAYS_SECONDS`：
   - 在开仓成交后挂 SL/TP 前，对“交易所侧暂未显示持仓”进行多次确认。
   - 默认确认节奏：`0.0s, 0.3s, 0.7s, 1.2s, 2.0s, 3.0s`。
   - 任意一次看到持仓后继续挂 SL/TP；多次确认后仍无持仓才进入保护失败路径。

2. 调整 disabled symbol 处理：
   - disabled 继续表示“禁止新策略开仓”。
   - 但如果交易所已有 live position，仍继续做本地 ownership 检查和保护不变量检查。
   - 已管理 open trade 即使 symbol disabled，也允许进入保护修复流程。

3. 周期对账发现已管理仓位缺 SL 时：
   - 先调用 `_repair_sl_tp(symbol)` 尝试补挂缺失 SL/TP。
   - 补挂成功则不禁用、不平仓。
   - 补挂失败才保留原有禁用路径。

## 验证结果

- 相关测试：`.venv/bin/python -m pytest tests/test_engine.py`，结果 `54 passed`。
- 语法检查：`.venv/bin/python -m py_compile src/engine/loop.py`，通过。
- 空白检查：`git diff --check`，通过。
- 完整测试：`.venv/bin/python -m pytest`，结果 `259 passed, 2 warnings`。

## 线上状态

- 已重启 `binance-trade.service` 和 `binance-trade-web.service`。
- `systemctl is-active binance-trade.service binance-trade-web.service` 返回均为 `active`。
- 最终重启时间：`2026-06-09 18:09:01 CST`。
- 重启后日志确认：
  - BTCUSDT 仍是 `symbol.enabled=false`，但对账进入“continue protection ownership check while keeping new entries disabled”，不再因 disabled 直接跳过。
  - 本地开放订单快照显示 BTCUSDT 已有 reduce-only 条件单：SL `63000.0`、TP `60735.1`。

## 后续注意事项

- 本次未改 emergency close 的滑点护栏逻辑；保护失败后的强制平仓被普通滑点护栏拒绝，仍需单独修。
- 本次未改变 LLM 策略判断，只改执行层保护确认和已有仓位保护修复。
- disabled symbol 后续仍不会主动开新仓；只是不会再阻断已有仓位的保护修复。
