# 2026-06-10 决策日志与交易记录页面性能优化

## 背景

操作面板打开“决策日志”时出现拉取失败提示和明显卡顿；随后按相同思路优化“交易记录”页面，降低筛选、翻页和首屏加载时的请求堆叠风险。排查时间为 `2026-06-10 CST`。

## 现场现象

- web 日志中 `/api/decisions` 多次返回 `200 OK`，但同一时间段存在密集的 summary / decisions 请求。
- 决策日志筛选会在短时间内发出多次请求，旧请求可能晚于新请求返回。
- `/api/summary` WebSocket 默认每秒推送一次，其中包含 `recent_decisions`。
- 交易记录页首屏同时拉取交易汇总、订单流水、风控拒单；筛选条件变化时也会立即发起交易查询，缺少 debounce、取消和旧响应防覆盖。

## 根因

1. `decisions` 表虽然只有约 6300 行，但列表查询使用 `SELECT *`。
2. 单条决策包含 `context_json`、`llm_prompt`、`llm_request_json`、`llm_response_json`、`feature_snapshot_json` 等大字段。
3. 实测 `/api/decisions` 100 行 JSON 约 4.7MB；`/api/summary` 中 20 条 `recent_decisions` 约占 258KB 审计字段并每秒推送。
4. 按 symbol 过滤时当前只有单列 `symbol` 索引，SQLite 查询计划会出现临时排序。
5. 交易汇总按 `opened_at_ms DESC, id DESC` 排序并展开对应订单明细，既有库可能缺少能覆盖该排序和 `orders.trade_id` 明细查询的组合索引。

## 代码改造

- 保持 `/api/decisions` 默认响应字段不变，不新增轻量列表接口。
- 决策日志前端默认分页从 100 改为 25，页大小选项收敛为 25/50/100。
- 决策日志筛选增加短 debounce，并用 AbortController / 请求序号避免旧请求覆盖新结果。
- 实时刷新遇到已有列表请求时跳过本轮，避免请求堆叠。
- `/api/summary` 的 `recent_decisions` 从 20 条降为 5 条，保留字段结构。
- Store 启动迁移新增幂等复合索引：
  - `ix_decisions_ts_id`
  - `ix_decisions_symbol_ts_id`
  - `ix_trades_opened_id`
  - `ix_trades_symbol_opened_id`
  - `ix_trades_status_opened_id`
  - `ix_orders_trade_ts_id`
- `search_decisions()` 增加慢查询 warning，记录耗时和筛选参数。
- 交易记录前端默认分页从 100 改为 25，页大小选项收敛为 25/50/100。
- 交易汇总筛选增加短 debounce，并用 AbortController / 请求序号避免旧请求覆盖新结果。
- 交易记录首屏只拉当前交易汇总；订单流水和风控拒单切到对应页签或手动刷新时再拉取，单次原始列表限制从 150 降为 100。
- `search_trades()` 增加慢查询 warning，记录耗时、分页、筛选参数、返回交易数和展开订单数。
- 交易明细订单展开查询改为按 `trade_id, ts_ms, id` 排序，便于使用 `ix_orders_trade_ts_id`。

## 验证结果

- 性能测算：
  - `/api/summary` 本地 JSON 体积从约 `492KB` 降到约 `146KB`。
  - `summary.recent_decisions` 从 `20` 条降为 `5` 条。
  - 本地 `data/trade-testnet.db` 中 `trades=46`、`orders=207`；`search_trades(limit=100)` JSON 约 `380KB`，`limit=25` JSON 约 `213KB`。
  - 交易记录首屏不再同时请求订单流水和风控拒单，切换到对应页签时再拉取最近 `100` 条。
- 针对性测试：
  - `.venv/bin/python -m pytest tests/test_web_status.py tests/test_store.py`
  - 结果：`62 passed`
- 完整测试：
  - `.venv/bin/python -m pytest`
  - 结果：`280 passed, 2 warnings`
- 前端构建：
  - `npm run build`
  - 结果：通过；构建器仍输出依赖包 `@vueuse/core` 的 `/* #__PURE__ */` 注解位置 warning，不影响构建退出码。
- 格式检查：
  - `git diff --check`
  - 结果：通过。

## 线上状态

- 已随提交 `d051784` 推送到 `origin/main`。
- 已于 `2026-06-10 17:47 CST` 依次重启 `binance-trade-frontend.service`、`binance-trade-web.service`、`binance-trade.service`。
- 重启后 `binance-trade.service`、`binance-trade-web.service`、`binance-trade-frontend.service` 均为 `active`。
- 新增 SQLite 索引已在交易主进程和 Web 进程本轮 `Store.connect()` 时幂等创建或确认存在。

## 后续注意事项

- 本次按保守要求不改变 `/api/decisions` 响应大字段，因此用户手动选择 100 条时仍会拉取较大响应。
- 如果后续仍卡，下一阶段应新增轻量列表接口或 `summary_only=true`，从根源上避免列表传输审计大字段。
- 交易记录页仍保留展开订单明细；若后续交易数量和每笔订单明细继续膨胀，可再考虑默认只返回交易汇总、展开单笔时按 trade_id 懒加载订单。
