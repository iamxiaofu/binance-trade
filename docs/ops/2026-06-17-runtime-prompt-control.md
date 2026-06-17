# 2026-06-17 LLM Prompt 动态附加指令

## 背景

LLM Profile 已支持前端动态新增、切换和 fallback，但 Prompt 仍完全由
`src/llm/prompt.py` 固定。生产运行时如果要微调策略风格，只能改代码并重启。

Prompt 同时包含系统硬规则、风控纪律、结构化输出约束和市场上下文模板。早期版本只开放
“附加策略指令”；现在升级为支持完整 System Prompt 与 User Prompt 模板在线编辑，同时保留旧版本兼容。

## 架构

最终发给 LLM 的 prompt 有两种渲染模式：

- `legacy_append`：旧兼容模式。固定系统硬规则继续由代码维护，`content` 作为运行期附加策略指令追加到
  system prompt 末尾；User Prompt 仍由代码默认模板生成。
- `full_template`：完整模板模式。`system_prompt_template` 与 `user_prompt_template` 均来自
  `llm_prompt_versions`，由 engine 热加载后渲染生效。

User Prompt 不是静态文本。模板使用白名单占位符渲染动态上下文，例如 `{symbol}`、`{last_price}`、
`{position_block}`、`{indicator_block}`、`{recent_klines_json}`、`{micro_klines_json}`。未知占位符不会执行，
会原样保留并在预览里返回 warning。

无论 Prompt 如何改，执行层的代码风控仍然独立生效：杠杆、保证金、止损、熔断、保护单等限制不会因
Prompt 内容变化而绕过。

## 改动

- `llm_prompt_versions` 表升级为 Prompt 模板版本表，新增：
  - `render_mode`
  - `system_prompt_template`
  - `user_prompt_template`
  - `template_schema_version`
  - `notes`
- `decisions` 新增 `llm_system_prompt` 字段，保存每次 LLM 调用真实生效的 system prompt 快照。
- 新增 Web API：
  - `GET /api/llm/prompt`
  - `POST /api/llm/prompt/preview`
  - `POST /api/llm/prompt/validate`
  - `POST /api/llm/prompt/apply`
  - `POST /api/llm/prompt/{id}/activate`
- `POST /api/llm/prompt/apply` 会：
  - 创建并激活新 Prompt 版本。
  - 写入 `RELOAD_LLM_PROMPT` 命令队列。
  - mainnet 环境要求 `UPDATE_LLM_PROMPT` 确认令牌。
- `POST /api/llm/prompt/{id}/activate` 会：
  - 按历史版本 ID 回切 active Prompt，不创建新版本、不覆盖历史内容。
  - 写入 `RELOAD_LLM_PROMPT` 命令队列。
  - mainnet 环境要求 `ACTIVATE_LLM_PROMPT` 确认令牌，确认 payload 为 `{id, version}`。
- Engine 消费 `RELOAD_LLM_PROMPT` 后重建 LLM fallback chain。
- `LLMClient` 统一使用 `render_prompts()` 渲染最终 System/User Prompt。
- LLM 配置页 Prompt 控制面板支持编辑完整模板、渲染预览、保存新版本并热加载、加载历史版本到编辑器、
  查看历史版本内容、回切历史版本并热加载。
- `POST /api/llm/prompt/validate` 会用当前 active LLM profile 真实请求 LLM，校验返回是否满足
  `TradeDecision` schema；该接口不写入 `decisions`，不触发风控，不下单。

## 前端交互

- 进入页面后，编辑区自动填充当前 DB active Prompt；如果库里没有 active 版本，则填充代码默认完整
  System/User 模板作为未保存草稿，保存后成为第一版。
- 保存按钮语义为“保存为新版本并热加载”，不会覆盖已有版本。
- 历史版本表展示 vN、名称、来源、更新时间、DB active 状态和 engine 生效状态。
- 历史版本默认只展示摘要；点击“查看内容”才展开 System Template、User Template、Legacy Addendum、Notes。
- “加载到编辑器”只覆盖当前编辑区，不改变 DB active，也不影响 engine。
- “回切并热加载”会激活所选历史版本，并通知 engine 下一次 LLM 决策使用该版本。
- “发送 LLM 校验”可勾选 BTC/ETH/SOL/BNB，用最近落库的决策上下文渲染 prompt 后请求 LLM；只校验返回参数。
- 轮询刷新不会覆盖正在编辑但尚未保存的内容；未保存状态会显示在编辑区下方。
- 当前不提供删除版本功能，避免误删生产决策审计材料。

## 生效语义

- 当前正在进行的 LLM 请求不会被中断。
- Prompt 修改后，下一次 LLM 决策开始使用新版本。
- engine 会把 `llm.prompt_version`、`llm.prompt_name`、`llm.prompt_source` 写入
  `runtime_settings`，前端据此显示是否已同步。

## 验证

- `tests/test_llm_client.py` 覆盖附加指令进入真实 system prompt trace。
- `tests/test_store.py` 覆盖 Prompt 版本创建、互斥激活和决策快照字段。
- `tests/test_web_status.py` 覆盖决策详情优先展示落库 system prompt。
