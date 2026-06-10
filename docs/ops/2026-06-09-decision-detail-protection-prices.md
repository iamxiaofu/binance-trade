# 决策详情展示成交后实际保护价

日期: 2026-06-09

## 背景

LLM 的 `reason` 中会给出基于 `entry_ref` 的预估 SL/TP，但实际保护单由执行层在开仓成交后使用交易所持仓 `entry_price` 重算并下发 `stopPrice`。

因此决策详情需要同时展示：

- LLM 返回的 `stop_loss_pct / take_profit_pct`
- 成交后实际挂出的 SL/TP 触发价
- SL/TP 当前订单状态，便于识别补挂、取消、缺失等情况

## 变更

- `web/status.py`
  - `decision_detail()` 新增 `actual_protection` 派生字段。
  - 后端不新增 DB 字段，实时从 `orders` 表查询。
  - 匹配逻辑：开仓决策后同 symbol/side 的第一笔成交 OPEN -> `trade_id` -> 同一 trade 的最新 SL/TP。
  - SL/TP 的实际触发价使用 `orders.price`，该字段对应执行层传给交易所的 `stopPrice`。

- `web/frontend/src/views/Decisions.vue`
  - 决策详情新增“成交后保护价”一行。
  - 展示入场价、SL、TP、订单状态。
  - 弹窗标题栏新增“刷新保护价”按钮；补挂单完成后可重新拉取详情并展示最新订单。

- `src/llm/prompt.py` / `src/llm/schema.py`
  - 强化 reason 风险换算方向要求。
  - `OPEN_LONG`: `SL=entry_ref×(1-stop_loss_pct)`，`TP=entry_ref×(1+take_profit_pct)`。
  - `OPEN_SHORT`: `SL=entry_ref×(1+stop_loss_pct)`，`TP=entry_ref×(1-take_profit_pct)`。
  - 明确 reason 中的价格只是基于 `entry_ref` 的预估值，实际成交后由执行层按交易所实际 `entry_price` 重算。

## 验证

- `.venv/bin/python -m pytest tests/test_web_status.py tests/test_llm_prompt.py tests/test_llm_schema.py`
- `npm run build`
