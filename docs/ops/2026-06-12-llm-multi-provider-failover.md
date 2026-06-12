# 2026-06-12 LLM 对接源重构：去 keyring + 多 provider + 主备 fallback 链

## 背景

原 LLM 热替换模块（见 `2026-06-08-llm-profile-management.md`）用 `src/llm/keyring_store.py`
（213 行）管理 API key，维护 **keyring（OS libsecret/kwallet）+ Fernet** 双后端 + Unavailable
哨兵 + 探测缓存 + 自定义 ref 格式（`profile://keyring/<name>` / `fernet:v1:<密文>`）。三个痛点：

1. **复杂**：双后端 + 哨兵 + Web 端 `_check_keyring_available`/503/banner；客户端有 `__init__` 与
   `from_profile` 两条构造路径。
2. **不利于迁移**：keyring 后端把 key 存进操作系统钥匙串（机器本地、不在 DB）——把 sqlite 搬到新机器
   key 全丢；Fernet 后端又依赖 `LLM_KEYRING_MASTER_KEY` 主密钥。生产为此还要 `dbus-daemon` +
   `gnome-keyring` + `scripts/start_with_keyring.sh` wrapper。
3. **无跨源兜底**：`decide()` 在单个 provider 内重试 `max_retries` 次后直接降级 HOLD，没有自动切到
   备用源；热切只能手动。

本次目标：
- **API key 明文存进 sqlite `llm_profiles.api_key`**，彻底删除 keyring/Fernet。迁移 = 直接拷 sqlite。
  （单租户自托管，DB 文件权限即边界。）
- **LLM client 抽象成多 provider**：`anthropic`（Messages + tool_use）与 `openai_compatible`
  （chat/completions + function calling），热切可在不同 provider/源之间切换。
- **主备 fallback 链**：主源在自身 `max_retries` 内失败后，同一次决策调用内立即试下一个源，全部失败才
  降级 HOLD。源顺序由 `priority` 升序决定，支持多个备源；下个周期自动从主源重来（自动 failback）。

## 设计

### Provider 抽象（新增 `src/llm/providers/`）

策略模式：**重试 / 超时 / 失败降级 HOLD / `LLMTrace` 审计 / symbol 纠正等安全逻辑全部留在
`LLMClient` 单点**，provider 只负责「构造请求 + 调 SDK + 解析 `TradeDecision`」三件事。

- `base.py`：`LLMProvider` 协议，方法 `request_payload` / `create` / `parse` / `ping` / `close`。
- `anthropic_provider.py`：搬原 client.py 的 `AsyncAnthropic` + tool_use 逻辑，tool 定义保留 `$defs`。
- `openai_provider.py`：`AsyncOpenAI` + `chat.completions` + `tool_choice` 强制 function；解析
  `choices[0].message.tool_calls[0].function.arguments`（JSON 字符串）。
- `_schema.py`：`build_anthropic_tool()`（保留 `$defs`）/ `build_openai_function()`。后者用
  `inline_defs()` 把 `$defs.Action` 内联到 `properties.action`，删顶层 `$defs`/`title` —— **OpenAI
  function `parameters` 对 `$ref/$defs` 支持脆弱（尤其第三方网关），必须内联枚举。**
- `__init__.py`：`build_provider(provider, *, model, base_url, api_key, timeout)` 工厂，懒加载具体
  provider 模块（只用 anthropic 时不强制 import openai SDK），未知 provider 抛 `ValueError`。

### 主备 fallback 链（新增 `src/llm/failover.py`）

`LLMFailoverClient` 持有按 priority 升序的 `[(name, LLMClient), ...]`，接口与 `LLMClient` 完全一致
（`decide_with_trace` / `decide` / `close`），engine 把它当 `self._llm` 用，**决策调用点与 `_llm_lock`
/ version / audit 机制零改动**。

- 复用每个 `LLMClient` 已有的「重试 N 次后降级 HOLD（`trace.status == "degraded"`）」作为失败信号；
  外层只在 degraded 时升级到链中下一个源。
- 「源1 三次失败后切源2」= 源1 自身 `max_retries`（默认 2 → 共 3 次）耗尽降级，链才走到源2。**阈值
  就是每源的 `max_retries`，无需新计数器。**
- `LLMTrace` 新增 `source_name` / `fallback_used`；failover 链路（每源 status/error）写进
  `response_json.failover`，决策详情面板可见本次由哪个源兜底给出。
- 单元素链 → 行为与改造前完全一致，零开销。

### DB / schema

- `LLMProfileRow`（`models.py`）：`keyring_ref` 弃用（旧列保留不删，停止读写）；新增
  `api_key`(Text 明文) / `priority`(Integer, default 100, index) / `fallback_enabled`(Boolean)。
- `repo.py` 迁移：新增 `_LLM_PROFILE_COLUMNS` 元组，`_upgrade_schema()` 仿现有 4 段补一段
  `PRAGMA table_info(llm_profiles)` + 缺列 ALTER。`create_all` 不补列，存量库靠此补出 3 个新列。
- `repo.py` 方法：对外视图统一走 `_profile_public()`（脱敏，返 `key_present` + `api_key_mask` 末4位，
  **绝不返明文**）；新增 `get_llm_profile_secret(name)`（明文，仅 engine 建链 / web test 内部用）、
  `get_enabled_llm_profiles()`（active 主源 + fallback_enabled 备源，按 `(priority, is_active desc,
  name)` 排序）。`upsert_llm_profile` 参数 `keyring_ref`→`api_key`（留空=不改）+ priority/fallback_enabled；
  `activate_llm_profile` 激活时把主源 `priority` 置 0（恒为链头）。修掉 `get_llm_profile` 里重复的
  `"max_tokens"` 键。
- `schema.py`：`LLMConfig.provider` 与 `LLMProfile.provider` 扩为 `Literal["anthropic",
  "openai_compatible"]`；`LLMProfile.keyring_ref`→`api_key`，加 `priority`/`fallback_enabled`。

### Engine（`src/engine/loop.py`）

- 删所有 keyring import。`_apply_llm_profile`/`_switch_llm_profile` 改为 `_build_llm_chain()` +
  `_apply_llm_chain()`：从 `get_enabled_llm_profiles()` 逐个读明文 key 建 `LLMClient`，包成
  `LLMFailoverClient`。`SWITCH_LLM_PROFILE` 命令触发整条链重建。
- **存量迁移落点**：`_ensure_default_profile_from_yaml` 条件由 `existing is None` 改为
  `existing is None or not <existing api_key>` —— 旧库 active profile 的 api_key 为空（原 keyring
  模式 key 在 OS 钥匙串取不回），建链跳过 → fallback 到此，从 `.env ANTHROPIC_API_KEY` 回填明文。
- `_replace_llm_client` 逻辑不动（锁/version/audit），额外把 `llm.chain` 写进 `runtime_settings`。

### Web（`web/server.py`）

- 删 keyring import / `_keyring_status` / `_check_keyring_available`；写操作不再 503。
- `_LLMProfileUpsert`：provider pattern → `^(anthropic|openai_compatible)$`，api_key max_length
  512→8192，加 `priority`/`fallback_enabled`。
- POST/PUT 直接写明文 `api_key`（留空=不改）；DELETE 不再删 keyring。
- `/api/llm/status` 删 `keyring` 字段、`switching_supported` 恒 True、新增 `chain`（有序源列表）+
  `engine.chain`。
- test 端点改用 `build_provider(...).ping()`（移除对 `AsyncAnthropic` 的直接依赖），从
  `get_llm_profile_secret` 取明文。

### 前端（`web/frontend/src/views/LLM.vue`）

- 删 keyring banner + keyring 状态卡 + 所有 `:disabled="!keyringAvailable"`。
- 状态卡第三格改为「Fallback 链」（展示 engine 实际链顺序）。
- provider 下拉启用并加 `openai_compatible` 选项；表单加 `priority` + 备源开关；base_url 占位文案按
  provider 区分。
- profile 表格加 priority / 备源列。

## 改动文件

| 层 | 文件 | 改动 |
|---|---|---|
| Provider | `src/llm/providers/{__init__,base,anthropic_provider,openai_provider,_schema}.py` | 新增抽象层 + 工厂 |
| Failover | `src/llm/failover.py` | 新增 `LLMFailoverClient` |
| 客户端 | `src/llm/client.py` | 改为持 provider；`LLMTrace` 加 `source_name`/`fallback_used` |
| 密钥 | `src/llm/keyring_store.py` | **删除（213 行）** |
| 模型 | `src/store/models.py` | `LLMProfileRow` 加 `api_key`/`priority`/`fallback_enabled` |
| 存储 | `src/store/repo.py` | 迁移补列；`_profile_public`/`get_llm_profile_secret`/`get_enabled_llm_profiles`；upsert/activate 改造 |
| Schema | `src/config/schema.py` | provider 扩值；`LLMProfile` 字段改造 |
| 引擎 | `src/engine/loop.py` | 去 keyring；建链 + 迁移回填 + `llm.chain` |
| Web | `web/server.py` | 去 keyring；provider ping；chain 状态；priority/fallback 字段 |
| 前端 | `web/frontend/src/views/LLM.vue` | 去 keyring UI；provider 选项 + 链展示 + priority/备源 |
| 依赖 | `requirements.txt` / `pyproject.toml` | 加 `openai`；不再用 keyring/cryptography |
| 测试 | `tests/test_llm_client.py` | 改注入 `FakeProvider` |
| 测试 | `tests/test_llm_profile.py` | 改明文 api_key；加链顺序断言；删 fernet/keyring 用例 |
| 测试 | `tests/test_llm_providers.py` | 新增：两 provider 解析 + `inline_defs` + 工厂 |
| 测试 | `tests/test_llm_failover.py` | 新增：主源 ok / 兜底 / 全失败 HOLD / 单源 / close |

## 迁移与回滚

- **schema 迁移**：`_upgrade_schema` 自动给存量 `llm_profiles` 补 `api_key`/`priority`/
  `fallback_enabled` 三列。已用 `data/trade-testnet.db` 副本实测：3 列补齐、active profile 的旧
  keyring key 不可恢复（api_key 为空），启动建链跳过 → 从 `.env` 回填 `default`。旧 `keyring_ref`
  列 SQLite 无法 DROP，留空无害。
- **存量 key**：原 keyring/Fernet 模式的 key **不会自动迁移**（OS 钥匙串里取不回）。升级后需要在
  Web 端重新填一次各 profile 的 api_key，或依赖 `.env ANTHROPIC_API_KEY` 回填的 `default`。
- **生产清理**：不再需要 `dbus-daemon`/`gnome-keyring`/`scripts/start_with_keyring.sh`；systemd 单元
  `ExecStart` 可改回直接 `.venv/bin/python ...`（本次不强制改 systemd，留作后续运维）。
- **回滚**：回滚 commit → 重启两个 systemd 单元。新增的 3 个 DB 列在旧代码下是孤儿，不影响。

## 风险点

- **明文入库**：DB dump 即泄露全部 key。单租户自托管已接受此权衡；务必保证 `data/*.db` 文件权限与
  备份介质的访问控制。
- **fallback 串行延迟（调用内方案固有）**：主源宕机时每个决策要先付主源 `timeout×(max_retries+1)`
  （如 60s×3=180s）才轮到备源。**建议主源 `max_retries` 调小（如 1）、`timeout` 适中**，使 worst-case
  延迟 Σ各源 `timeout×(retries+1)` < 决策周期，否则拖慢交易循环。
- **OpenAI function schema**：`$defs` 必须内联（已由 `inline_defs` 处理 + 测试覆盖），否则部分兼容网关
  报 schema 错。openai 模型/网关的 max_tokens 上限与 tool_choice 行为请实测。
- **base_url 语义差异**：anthropic 留空=官方；openai_compatible 留空=官方 OpenAI（多数场景需填网关地址）。

## 验证

- 单测：`tests/test_llm_client.py` `test_llm_profile.py` `test_llm_providers.py`
  `test_llm_failover.py` 共 27 例通过；全库 `pytest -q` **303 passed**。
- 迁移：`data/trade-testnet.db` 副本走 `Store.connect()` 实测补列 + 回填路径正常。
- 前端：`npm run build` 通过。
- 手测热切（部署后）：建 anthropic 主源 + openai_compatible 备源（fallback_enabled，priority 10）→
  `/api/llm/status.chain` 显示 [main, backup] → 把主源 key 改错 → 日志出现
  `LLM source main degraded, 尝试下一源` + `LLM failover ok ... 备源 backup`，决策非 HOLD、
  `response_json.failover.source=backup` → 备源也改错 → 降级 HOLD、`all 2 sources failed`、不下单 →
  主源 key 改回 → 下周期自动从主源服务（自动 failback）。
