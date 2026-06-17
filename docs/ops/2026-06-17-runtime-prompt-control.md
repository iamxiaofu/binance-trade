# 2026-06-17 LLM Prompt 动态附加指令

## 背景

LLM Profile 已支持前端动态新增、切换和 fallback，但 Prompt 仍完全由
`src/llm/prompt.py` 固定。生产运行时如果要微调策略风格，只能改代码并重启。

Prompt 同时包含系统硬规则、风控纪律、结构化输出约束和市场上下文模板。直接开放完整模板编辑风险较高，
容易误删风控边界或破坏审计字段。因此本次只开放“附加策略指令”。

## 架构

最终发给 LLM 的 system prompt 分为两层：

- 固定系统硬规则：继续由代码维护，包含 action 语义、size_pct/SL/TP 风控纪律、reason 必填格式。
- 运行期附加策略指令：由前端保存到 `llm_prompt_versions`，追加到 system prompt 末尾。

如果附加指令与固定硬规则冲突，固定硬规则优先。

User Prompt 市场数据模板仍由 `build_user_prompt()` 生成，不开放动态编辑，避免破坏行情、持仓、
风控和 K 线字段注入。

## 改动

- 新增 `llm_prompt_versions` 表，保存 Prompt 附加指令版本。
- `decisions` 新增 `llm_system_prompt` 字段，保存每次 LLM 调用真实生效的 system prompt 快照。
- 新增 Web API：
  - `GET /api/llm/prompt`
  - `POST /api/llm/prompt/preview`
  - `POST /api/llm/prompt/apply`
- `POST /api/llm/prompt/apply` 会：
  - 创建并激活新 Prompt 版本。
  - 写入 `RELOAD_LLM_PROMPT` 命令队列。
  - mainnet 环境要求 `UPDATE_LLM_PROMPT` 确认令牌。
- Engine 消费 `RELOAD_LLM_PROMPT` 后重建 LLM fallback chain。
- `LLMClient` 调用 provider 时使用 `build_system_prompt(addendum)`。
- LLM 配置页新增 Prompt 控制面板，支持编辑、预览、保存并热加载。

## 生效语义

- 当前正在进行的 LLM 请求不会被中断。
- Prompt 修改后，下一次 LLM 决策开始使用新版本。
- engine 会把 `llm.prompt_version`、`llm.prompt_name`、`llm.prompt_source` 写入
  `runtime_settings`，前端据此显示是否已同步。

## 验证

- `tests/test_llm_client.py` 覆盖附加指令进入真实 system prompt trace。
- `tests/test_store.py` 覆盖 Prompt 版本创建、互斥激活和决策快照字段。
- `tests/test_web_status.py` 覆盖决策详情优先展示落库 system prompt。
