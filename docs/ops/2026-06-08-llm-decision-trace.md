# 2026-06-08 LLM 决策请求追踪展示

## 背景

决策日志此前只保存 `context_json` 和解析后的结构化决策字段。

排查 LLM 判断时，只看 `context_json` 还不够直接：

- 无法确认实际发送给 LLM 的完整 Prompt。
- 无法查看 Anthropic tool-use 请求参数和工具 schema。
- 无法查看 LLM 原始回传，只能看到解析后的 `action/confidence/size_pct` 等字段。
- 页面详情只能看 JSON，不方便按字段理解 LLM 到底看到了哪些数据。

## 根因

旧链路中 `LLMClient.decide()` 在内部构建 `user_prompt` 并调用 Anthropic API，
但方法只返回解析后的 `TradeDecision`。

交易引擎落库时只能拿到：

- `MarketContext` 序列化后的 `context_json`
- 解析后的 `TradeDecision`

因此历史记录可以基于 `context_json` 重建 Prompt，但无法恢复真实原始 response。

## 改造内容

- LLM 客户端新增 `decide_with_trace()`：
  - 保持原 `decide()` 返回值兼容。
  - 返回结构化决策和一次调用 trace。
  - trace 不包含 API key。
- 决策日志表 `decisions` 追加审计字段：
  - `llm_prompt`
  - `llm_request_json`
  - `llm_response_json`
- 交易引擎实际调用 LLM 后，把 trace 随决策一起落库。
- 决策详情接口新增派生字段：
  - `llm_system_prompt`
  - `llm_user_prompt`
  - `llm_request_effective_json`
  - `llm_response_effective_json`
  - `llm_trace_available`
  - `llm_data_items`
- 决策详情弹窗新增标签页：
  - LLM 数据列表
  - Prompt
  - 完整请求 JSON
  - LLM 回传结果
  - Context JSON

## 数据口径

`context_json` 是系统内部保存的完整上下文快照。

真正发送给 LLM 的内容是 Prompt，其中：

- 主周期完整 K 线窗口用于本地计算指标。
- Prompt 中只放最近 20 根 K 线，避免 token 过大。
- 技术指标、趋势特征、成交量指标、多周期指标、持仓和账户风控字段会被展开写入 Prompt。

页面的“LLM 数据列表”按分类展示 Prompt 中的核心数据字段，便于审计。

## 兼容性

SQLite 使用轻量迁移追加列，旧库启动时自动补列：

```sql
ALTER TABLE decisions ADD COLUMN llm_prompt TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN llm_request_json TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN llm_response_json TEXT NOT NULL DEFAULT '';
```

历史记录兼容策略：

- 如果已有 `context_json`，页面会基于当前 Prompt 模板重建 `llm_user_prompt`。
- 历史记录没有原始 `llm_response_json`，页面会提示“历史未记录原始响应”。
- 新产生的非跳过决策会保存真实 request/response trace。

## 涉及文件

- `src/llm/client.py`
- `src/engine/loop.py`
- `src/store/models.py`
- `src/store/repo.py`
- `web/status.py`
- `web/frontend/src/views/Decisions.vue`
- `tests/test_llm_client.py`
- `tests/test_store.py`
- `tests/test_web_status.py`
- `tests/test_engine.py`

## 验证

执行：

```bash
.venv/bin/python -m pytest tests/test_llm_client.py tests/test_store.py tests/test_web_status.py tests/test_engine.py
cd web/frontend
npm run build
```

期望结果：

```text
102 passed
✓ built
```

构建过程中若出现 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
与既有构建日志一致，不影响产物生成。

## 运维注意

- 本次包含数据库字段追加，建议部署前备份当前 testnet/mainnet DB。
- 部署后需要重启 `binance-trade.service`，让交易引擎使用新的 LLM trace 落库逻辑。
- 部署后需要重启 `binance-trade-web.service`，让详情接口和前端页面加载新逻辑。
- 已打开的浏览器页面需要刷新，才能加载新的前端 chunk。
