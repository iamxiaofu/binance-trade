# 2026-06-09 LLM reason 风险换算约束

## 背景

BNBUSDT 决策中，LLM 结构化字段返回：

- `stop_loss_pct=0.012`
- `take_profit_pct=0.02`

系统语义中这两个字段表示相对参考开仓价的价格距离小数，即 1.20% / 2.00%。但 LLM 在 `reason` 文案中写成“止损 0.12%、止盈 0.2%”，造成审计理解混乱。

## 现场现象

- 风控计算使用结构化字段，止损亏损约等于 `notional × stop_loss_pct`。
- BNB 该单文案里“止损亏损约 45U”实际对应 `0.012=1.20%`，但同一句又写“止损 0.12%”。
- 交易引擎不解析 `reason` 下单，但前端和人工复盘会读取 `reason`，因此需要约束 LLM 文案与结构化字段一致。

## 根因

当前 prompt 只说明 `stop_loss_pct / take_profit_pct` 是相对开仓价比例，但没有强制 LLM 在 `reason` 中做小数到百分比的换算，也没有要求写出 SL/TP 触发价、USDT 损益、权益占比和保证金占比。

同时，原 `reason` 上限为 500 字，难以同时容纳行情依据与完整风险换算。

## 代码改造

1. `TradeDecision.reason` 上限从 500 扩展到 1000。
2. `decisions.reason` 模型长度同步为 1000，落库截断同步为 1000。
3. `TradeDecision` tool schema 为 `size_pct`、`stop_loss_pct`、`take_profit_pct`、`reason` 增加字段说明。
4. `SYSTEM_PROMPT` 增加强约束：
   - `0.012` 必须表述为 `1.20%`，不能写成 `0.12%`。
   - `OPEN_LONG/OPEN_SHORT` 的 `reason` 必须同时写清风险换算。
5. `build_user_prompt()` 增加风险换算规则：
   - `entry_ref` 使用最新价估算。
   - 百分比换算固定为 `pct_percent = pct_decimal × 100`。
   - 多/空 SL/TP 触发价公式。
   - `margin_used`、`notional`、`sl_loss`、`tp_profit`、`equity_loss_pct`、`margin_loss_pct`、`R` 公式。
   - 强制紧凑风险块模板。
6. 同步校准 prompt 中单笔保证金硬上限的表达：
   - `size_pct` 仍是“动用可用保证金比例”。
   - 风控硬上限校验对象是 `margin_used=可用保证金×size_pct`，不得超过按账户权益计算的 `max_order_margin_abs`。
   - 可用保证金与账户权益不一致时，不再把 `max_order_margin_abs` 误写成 `max_order_margin_pct × 可用保证金`。

## 验证结果

- 相关测试：`.venv/bin/python -m pytest tests/test_llm_schema.py tests/test_llm_prompt.py tests/test_size_pct_prompt_discipline.py tests/test_store.py`，结果 `54 passed`。
- 语法检查：`.venv/bin/python -m py_compile src/llm/schema.py src/llm/prompt.py src/store/models.py src/store/repo.py`，通过。
- 空白检查：`git diff --check`，通过。
- 完整测试：`.venv/bin/python -m pytest`，结果 `257 passed, 2 warnings`。

## 线上状态

- 已重启 `binance-trade.service` 和 `binance-trade-web.service`。
- `systemctl is-active binance-trade.service binance-trade-web.service` 返回均为 `active`。
- 最终重启时间：`2026-06-09 17:11:57 CST`。
- 启动日志确认交易进程已加载 testnet DB 并启动，Web 进程监听 `127.0.0.1:8000`。

## 后续注意事项

- Prompt 约束能显著降低自然语言错误，但不能从根上保证 LLM 数学永远正确。
- 后续更稳的方案是由交易引擎基于结构化字段生成“系统风险解释”，供前端和人工复盘对照。
- 本次不改变交易语义：`stop_loss_pct/take_profit_pct` 仍表示相对参考开仓价的价格距离小数。
