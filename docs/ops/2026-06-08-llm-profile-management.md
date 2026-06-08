# 2026-06-08 LLM profile 管理（多源接入 + 热替换）

## 背景

当前 LLM 接入面拆成两段：

- `config.yaml → llm.*`：provider / model / base_url / timeout / max_tokens / max_retries / kline_*。
- `.env → ANTHROPIC_API_KEY`：API key 注入到 systemd 单元的 Environment。

切换 LLM 调用源（例如从第三方中转切到 Anthropic 官方）必须：

1. 编辑 `config.yaml`；
2. 编辑 `.env` 并改密钥；
3. `systemctl restart binance-trade.service`；
4. 复盘期间策略会中断。

类似地，第三方中转偶发故障时我们没办法在不重启的前提下临时切到官方或备用中转。这次改造把"profile"和"运行时"解耦，目标是：

- API key 仍然不落 DB、不进日志、不进响应。
- 切换 LLM 调用源不需要重启交易进程。
- 切换前有 dry-run 校验（ping），失败时旧 profile 保持生效。
- 切换过程有审计（`decisions.action="LLM_SWITCH"` + `runtime_settings.llm.active_*`）。

## 设计目标

- **多 profile**：每个 profile 是一组 `(provider, model, base_url, timeout, max_tokens, max_retries, keyring_ref)`。
- **热替换**：web 下发命令 → 引擎在两次 LLM 调用之间替换 `LLMClient`，期间被一把 `asyncio.Lock` 串行化。
- **密钥保护**：明文走 keyring（OS 级 libsecret/kwallet/secretstorage），降级方案走 Fernet 对称加密（环境变量 `LLM_KEYRING_MASTER_KEY` 提供主密钥）。
- **向后兼容**：旧的 yaml + env 模式仍可工作。首次启动时把 yaml 默认值落成一条 `default` profile 写入 `llm_profiles` 表并标 `is_active=true`。
- **作用域**：本次只把"调用源"那一组参数做成可热替换；K 线周期、prompt 模板长度等"工程参数"仍走 yaml + 重启，避免每次热替换都重新构造整套市场数据结构。

## 数据结构

### 新增 pydantic schema：`LLMProfile`

字段：

- `name`：profile 名（主键，1-64 字符）。
- `provider`：当前固定 `anthropic`。
- `model`、`base_url`（可空，留空走官方）、`timeout`、`max_tokens`、`max_retries`。
- `is_active`：bool，同一时刻只能 1 个为 True。
- `keyring_ref`：指向 keyring 里的密钥（keyring 模式）或 Fernet 密文（fernet 模式）。

### 新增 ORM 表：`llm_profiles`

列：`name` 主键、`provider/model/base_url/timeout/max_tokens/max_retries/is_active/keyring_ref/created_at/updated_at`。

迁移方式：沿用 `Store._upgrade_schema` 的轻量 `create_all + ALTER`，对存量库自动建表；不需要手动 `ALTER TABLE`。

### 新增 `runtime_settings` 字段

- `llm.active_name`：当前 engine 进程用的 profile 名。
- `llm.active_version`：单调递增，每次成功热替换 +1。
- `llm.active_source`：本次来源（`db` / `yaml-default` / `command`）。

`/api/llm/status.engine` 透出这 3 个字段供前端轮询对比"DB 标 active"与"engine 真在用"是否一致。

### 新增模块：`src/llm/keyring_store.py`

抽象 `KeyringStore`（Protocol），单例 `get_keyring_store()` 在模块层做 backend 探测：

1. 优先 `keyring`（依赖 `libsecret-1` / `kwallet` / `secretstorage`，headless server 通常无）。
2. 降级 Fernet（环境变量 `LLM_KEYRING_MASTER_KEY`）。
3. 都不可用 → 返回 `_Unavailable` 哨兵；写操作抛 `KeyringUnavailable`，前端用 banner 提示。

启动时探测结果缓存到 `runtime_settings`（可选），web 端 `/api/llm/status.keyring` 直接返回 `{backend, available, hint}`。

## 接口

### REST（web/server.py，Basic Auth 守护）

| 端点 | 用途 |
|---|---|
| `GET /api/llm/status` | 顶部 banner + 切换状态轮询（keyring, active, engine）。 |
| `GET /api/llm/profiles` | 列表（**不含** key 明文，只有 `key_present`）。 |
| `POST /api/llm/profiles` | 新增；body 含 `api_key` 明文一次。 |
| `PUT /api/llm/profiles/{name}` | 编辑；`api_key` 留空 = 不动旧 key。 |
| `DELETE /api/llm/profiles/{name}` | 删除；active 不允许删。 |
| `POST /api/llm/profiles/{name}/test` | dry-run：拿 keyring 里的 key 真的发一次 `messages.create` 最小 ping；不会切换。 |
| `POST /api/llm/profiles/{name}/activate` | 切 is_active + 入队 `SWITCH_LLM_PROFILE` 命令。 |

所有写操作在 keyring 不可用时返回 503 + 明确 hint。响应模型中**不包含** `api_key` 字段，logs/响应/DB 都不会出现明文。

### 命令队列（engine/web 解耦）

新增命令名：`SWITCH_LLM_PROFILE`，参数为 profile name。web 端 `enqueue_command` → engine `_exec_command` → 读 profile → 拿 keyring key → `LLMClient.from_profile` 工厂 → 加锁替换 → 落 `runtime_settings.llm.active_*` 与一条 `decisions` 审计。

白名单 `_ALLOWED_COMMANDS` 在 `web/server.py` 已加 `SWITCH_LLM_PROFILE`。

### 前端

新增 `/llm` 路由（`web/frontend/src/views/LLM.vue`）：

- 顶部 banner：keyring 不可用时禁用所有写操作并提示安装方式。
- 三块状态卡：DB active / Engine 生效（带"已同步" / "热替换中" tag）/ keyring backend。
- profile 表格：name / provider / model / base_url / timeout / max_tokens / key（"已存"/"未设" tag）/ 状态 / 操作（激活/测试/编辑/删除）。
- 弹窗：新增/编辑表单，name 不可改（编辑时禁用），`api_key` 字段编辑时留空 = 不修改。
- 2 秒轮询 `/api/llm/status`，捕获 engine 端 `active_version` 变化，UI 上把"DB active"和"Engine 生效"一致时打"已同步"，否则打"热替换中"。

## 改动文件

| 层 | 文件 | 改动 |
|---|---|---|
| Schema | `src/config/schema.py` | 新增 `LLMProfile` |
| 模型 | `src/store/models.py` | 新增 `LLMProfileRow`（表 `llm_profiles`） |
| 存储 | `src/store/repo.py` | 5 个新方法：`list_llm_profiles / get_llm_profile / upsert_llm_profile / delete_llm_profile / activate_llm_profile / get_active_llm_profile` |
| 密钥 | `src/llm/keyring_store.py` | 新模块，keyring + Fernet 双 backend + 不可用哨兵 |
| 客户端 | `src/llm/client.py` | 新增 `_LLMRuntime` 视图 + `LLMClient.from_profile` 工厂；保留原 `__init__` 不破 |
| 引擎 | `src/engine/loop.py` | `__init__` 加 `_llm_lock` / `_llm_version` / `_llm_profile_name`；`decide_with_trace` 加锁；新增 `_bootstrap_llm_profile / _apply_llm_profile / _replace_llm_client / _switch_llm_profile / _ensure_default_profile_from_yaml`；`_exec_command` 新分支 `SWITCH_LLM_PROFILE` |
| Web | `web/server.py` | `_ALLOWED_COMMANDS` + 7 个 `/api/llm/...` 端点；pydantic upsert 模型；错误码 503/409/404/502 |
| 前端 | `web/frontend/src/router.js` | 加 `/llm` 路由 |
| 前端 | `web/frontend/src/App.vue` | 菜单加 "LLM 配置" |
| 前端 | `web/frontend/src/api.js` | 7 个 `api.llm.*` 方法 |
| 前端 | `web/frontend/src/views/LLM.vue` | 新视图 |
| 测试 | `tests/test_llm_profile.py` | 5 个新测试 |

## 备份与回滚

- 改造不删除旧表/旧列。`llm_profiles` 是新增表，旧库自动迁移；旧 `LLMConfig` 与 `decisions` 表不受影响。
- 回滚：回滚 commit → 重启两个 systemd 单元即可。`runtime_settings.llm.active_*` 三个 key 在回滚后只是孤儿，不影响。
- 备份策略（DB）：`data/backups/*before-llm-profiles-$ts.db`，与既有备份策略一致。本次未产生 schema 破坏性变更，**未强制要求备份**；如需补：`cp data/trade-testnet.db data/backups/manual-before-llm-profiles-$(date +%s).db`。

## 风险点

- **keyring 不可用**：web 端写操作 503，前端 banner 提示。生产环境建议至少配 `LLM_KEYRING_MASTER_KEY`。
- **Fernet 主密钥丢失**：所有 Fernet 模式 profile 全部失效。备份里**不**含主密钥；建议把 `LLM_KEYRING_MASTER_KEY` 同步到团队密码管理器。
- **热替换中请求被丢弃**：替换窗口由 `_llm_lock` 串行化（一个完整 `decide_with_trace` 时长，通常 1-5s），不会出现两个 LLMClient 同时调用。
- **第三方中转 base_url**：用户自填，文档明确"中转等同把 key 托管给第三方"。
- **审计**：web 端 `commands.result` 字段不写 key；`decisions.action="LLM_SWITCH"` 只记 profile 名。
