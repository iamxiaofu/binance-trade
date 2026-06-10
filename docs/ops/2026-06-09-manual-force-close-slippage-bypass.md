# 2026-06-09 手动平仓强制退出语义

## 背景

重启前后日志显示，BNBUSDT 的 `CLOSE_POSITION` 手动平仓命令多次被市价单滑点护栏拒绝：

- `2026-06-09 22:15:53 CST`：`slippage 30.1bps > limit 8.0bps`
- `2026-06-09 22:16:23 CST`：`slippage 39.9bps > limit 8.0bps`
- `2026-06-09 22:16:58 CST`：`slippage 46.9bps > limit 8.0bps`
- `2026-06-09 22:17:27 CST`：`slippage 55.5bps > limit 8.0bps`

手动平仓的操作语义应是用户确认后的强制退出：即使盘口相对 mark 价出现较大偏离，也应优先 reduce-only 退出现有仓位。

## 现场现象

手动 `CLOSE_POSITION` 后端路径会重新拉交易所持仓并校验页面持仓签名，但最终调用执行层 `close_position(..., mode=MARKET_TAKER)`。

执行层在所有市价平仓前统一调用 `_preflight_market_slippage()`，用 mark 价作为参考价、盘口前 20 档估算冲击均价；当估算偏差超过 `market_slippage_bps=8` 时返回 `status=rejected, reason=slippage_exceeded`，不会向交易所提交市价单。

## 根因

市价单滑点护栏原本用于保护普通市价兜底和策略平仓，避免在盘口异常时拿到明显不合理价格。

手动平仓复用了同一条普通市价平仓路径，没有区分“策略自动平仓”和“用户确认后的强制退出”，导致人工强制退出也会被普通 8bps 阈值拦住。

## 代码改造

1. `src/execution/executor.py`
   - `close_position()` 新增 `skip_slippage_guard: bool = False` 参数。
   - `_close_market_position()` 仅在 `skip_slippage_guard=False` 时执行市价滑点预检。
   - 跳过护栏时写 warning 日志，但仍使用 `reduceOnly=True` 下市价反向平仓单。
   - maker-first 平仓的市价 fallback 分支也透传该参数，避免参数被静默丢弃。

2. `src/engine/loop.py`
   - 手动 `CLOSE_POSITION` 路径调用 `close_position(..., mode=MARKET_TAKER, skip_slippage_guard=True)`。
   - 保留交易所持仓重拉、方向/数量/开仓价签名校验、成交后 flat 复核和保护单撤销。
   - 在订单 `raw._local` 写入：
     - `manual_force_close=true`
     - `slippage_guard_skipped=true`

3. `web/frontend/src/views/Positions.vue`
   - 手动平仓 payload 增加 `force: true`。
   - 确认文案改为“强制市价 reduce-only 平仓（绕过滑点保护）”。

## 验证结果

- 已运行：
  - `.venv/bin/python -m pytest tests/test_executor.py::test_close_position_force_bypasses_slippage_guard tests/test_engine.py::test_command_close_position_closes_and_cancels_protection_without_disabling`
  - 结果：`2 passed`
- 完整测试：
  - `.venv/bin/python -m pytest`
  - 结果：`273 passed, 2 warnings`
- 前端构建：
  - `npm run build`
  - 结果：通过；构建器输出依赖包 `@vueuse/core` 的 `/* #__PURE__ */` 注解位置 warning，不影响构建退出码。

## 线上状态

当前仅完成代码修改，尚未重启服务。

## 后续注意事项

- 该改动只影响手动 `CLOSE_POSITION` 命令；策略显式 CLOSE、开仓 FALLBACK_MARKET、普通市价平仓默认仍受 `market_slippage_bps` 保护。
- 手动平仓仍保留 `reduceOnly=True`，不会因强制退出而反向开仓。
- 如果未来需要 kill-switch / emergency close 也具备强制退出语义，应单独评估并显式接入该参数，避免扩大本次改动范围。
