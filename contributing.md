# binance-trade 技术架构与贡献指南

本文档面向首次参与项目的开发者和长期维护人员，描述当前代码已经落地的真实架构、
关键安全边界和贡献流程。实现细节以代码为准；部署操作参见
[docs/DEPLOY.md](docs/DEPLOY.md)，日常运维参见
[docs/RUNBOOK.md](docs/RUNBOOK.md)，历史变更背景参见 `docs/ops/`。

## 1. 项目定位与设计原则

`binance-trade` 是一个面向 Binance USD-M 永续合约的自托管交易系统。LLM 负责输出
结构化交易建议，Python 代码负责账户状态、硬风控、订单执行、保护单、对账和审计。

核心原则：

- LLM 不是最终决策边界。任何开仓建议必须经过代码风控和交易所规则校验。
- 风控失败、LLM 超时、结构化输出错误、私有流异常时默认 fail-closed。
- testnet 与 mainnet 使用相同代码，但配置、密钥、数据库、日志和服务实例完全隔离。
- Binance 私有 User Data Stream 是账户状态主通道，REST 用于启动基线、恢复和审计。
- Web 进程不直接下单；所有交易操作通过 SQLite 命令队列交给 Engine 串行执行。
- 策略交易、外部成交和交易所实时投影分开存储，避免污染策略统计。
- `/data0/binance-trade` 是唯一源码仓库，`/opt/binance-trade` 只是发布副本。

## 2. 技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| 运行时 | Python 3.11+、asyncio | Engine、Web API、交易所和 LLM 异步调用 |
| 配置 | Pydantic、PyYAML、python-dotenv | 配置强校验、环境凭据加载 |
| 交易所 | ccxt async、原生 Binance WebSocket | REST 下单/查询、私有账户事件 |
| LLM | Anthropic SDK、OpenAI SDK | Messages tool-use、Chat Completions function calling |
| 数据 | SQLAlchemy async、aiosqlite、SQLite | 运行态投影、审计、命令和配置持久化 |
| Web | FastAPI、Uvicorn、WebSocket | 看板 API、认证、命令控制和实时推送 |
| 前端 | Vue 3、Vue Router、Element Plus、Vite | 双环境控制台 |
| 计算 | pandas、NumPy、pandas-ta | K 线与技术指标 |
| 测试 | pytest、pytest-asyncio | 单元、集成和回归测试 |
| 部署 | systemd、nginx | 双环境进程托管、TLS 和反向代理 |

## 3. 运行拓扑

### 3.1 目录职责

| 路径 | 职责 | 约束 |
|---|---|---|
| `/data0/binance-trade` | Git 源码、开发和测试 | 所有代码修改从这里开始 |
| `/opt/binance-trade` | root 管理的发布副本 | 不允许直接修改；从源码目录单向同步 |
| `/etc/binance-trade` | 环境配置和密钥 | 不进入 Git；限制文件权限 |
| `/var/lib/binance-trade/{testnet,mainnet}` | 分环境 SQLite 数据库 | 不得跨环境复制或混用 |
| `/var/log/binance-trade/{testnet,mainnet}` | 分环境日志 | 由 `trader` 写入 |

### 3.2 服务实例

| 服务 | 进程职责 | 监听/数据 |
|---|---|---|
| `binance-trade@testnet` | testnet Engine | testnet 配置和数据库 |
| `binance-trade@mainnet` | mainnet Engine | mainnet 配置和数据库 |
| `binance-trade-web@testnet` | testnet FastAPI + 静态前端 | `127.0.0.1:8000` |
| `binance-trade-web@mainnet` | mainnet FastAPI | `127.0.0.1:8001` |

四个服务均以 `trader` 用户运行。nginx 将单份前端静态资源交给 testnet Web 实例，并将
`/api/testnet/`、`/api/mainnet/` 和对应 WebSocket 路径转发到不同后端。

mainnet Engine 每次启动后保持新开仓暂停，必须在完成私有流、REST 对账和人工检查后
从控制面恢复。`SIGTERM` 只优雅停止进程，不会撤单或平仓；撤单和平仓必须显式执行
kill-switch。

## 4. 后端模块边界

| 模块 | 主要职责 | 不应承担的职责 |
|---|---|---|
| `src/config` | YAML、环境变量、强类型和范围校验 | 运行时热更新和业务逻辑 |
| `src/engine` | 生命周期、周期调度、命令消费和模块编排 | 重复实现风控或交易所精度规则 |
| `src/exchange` | ccxt 封装、私有流、事件和响应标准化 | 策略决策和业务风控 |
| `src/state` | 单写入账户投影和可重建进程内运行态 | 长期审计数据 |
| `src/features` | 市场、仓位和指标上下文构建 | 下单和持久化 |
| `src/throttle` | 判断本周期是否需要调用 LLM | 修改账户或交易状态 |
| `src/llm` | Prompt 渲染、Provider、重试、结构化解析和故障转移 | 绕过代码风控直接执行 |
| `src/risk` | 开仓硬约束和拒单原因 | 网络 IO 和下单 |
| `src/execution` | 精度规整、订单执行、重试、SL/TP 和平仓 | 重新解释 LLM 策略意图 |
| `src/store` | Schema、迁移、审计、投影和命令持久化 | 直接调用交易所 |
| `src/notify` | Telegram 告警 | 决定交易行为 |
| `src/backtest` | 历史 K 线重放和风控验证 | 模拟完整交易所撮合 |
| `web` | 查询、认证、受控命令、行情和前端托管 | 直接提交交易订单 |

依赖方向应保持为“编排层调用能力层”。底层模块不应反向依赖 Web，风控和执行也不应
依赖 Engine 的具体实例。

## 5. Engine 生命周期与账户状态

### 5.1 启动流程

`main.py run` 加载配置和凭据后构造 `TradingEngine`。启动过程依次完成：

1. 建表、执行兼容迁移并加载持久化运行参数。
2. 启动 `AccountStateCoordinator`，重放未完成的交易所事件。
3. 加载交易所市场和每个 symbol 的过滤器。
4. 恢复最近决策特征快照和当日已实现盈亏。
5. 启动 Binance 私有流，等待连接并记录流健康状态。
6. 提交 REST 账户快照，回放启动期间缓存的私有事件。
7. 同步交易所成交，执行持仓、订单和保护单对账。
8. 从 SQLite 加载 LLM Profile fallback 链和 active Prompt。
9. 启动周期对账任务并进入策略循环。

任一账户模式、私有流或启动对账关键步骤失败时，新开仓会被暂停，而不是带着不完整状态运行。

### 5.2 单写入账户投影

`AccountStateCoordinator` 通过队列串行处理 `ExchangeEvent`：

- 原始事件先写入 `exchange_events` 幂等 inbox。
- `ACCOUNT_UPDATE`、`ORDER_TRADE_UPDATE`、`ALGO_UPDATE` 和 REST 快照更新内存投影。
- 最新余额、持仓和挂单同步写入 `live_balances`、`live_positions`、`live_orders`。
- REST 快照与私有流投影不一致时记录 `exchange_state_drifts`。
- Engine 和 Executor 从同一个账户快照读取状态，避免多个任务各自维护一套事实。

私有流状态为 `DISCONNECTED` 或 `RESYNCING` 时阻止新开仓。重连后必须先完成 REST
对账，再恢复由流故障造成的技术暂停。

## 6. 决策与执行数据流

一个策略周期的顺序固定如下：

| 阶段 | 输入 | 处理 | 输出 |
|---|---|---|---|
| 命令消费 | `control_commands` | 串行执行暂停、恢复、平仓、参数更新等命令 | 更新运行态和命令状态 |
| 市场刷新 | 注册的 symbols | 获取 K 线、盘口和价格 | 最新市场快照 |
| 全局闸门 | 权益、当日盈亏、回撤 | 检查 kill-switch、日亏和回撤熔断 | 继续或暂停新开仓 |
| 特征与节流 | 市场、持仓、挂单、上次快照 | 构建指标并判断是否需要调用 LLM | 跳过心跳或 LLM 上下文 |
| LLM 决策 | Prompt、市场上下文、持仓上下文 | 强制工具调用并校验 `TradeDecision` | HOLD、开仓、平仓或调整保护单 |
| 硬风控 | 决策、账户权益、保证金和价格 | 校验置信度、杠杆、保证金、止损和强平距离 | `Verdict` 或拒单 |
| 交易所规整 | 数量、价格、symbol 过滤器 | 应用 tickSize、stepSize、minQty、minNotional | 可执行订单参数 |
| 执行 | 订单参数、执行模式 | 下单、等待确认、处理部分成交并挂 SL/TP | 标准化订单结果 |
| 审计 | 决策、拒单、订单和快照 | 写入 SQLite，必要时发送告警 | 可复盘记录 |

循环会在周期等待期间每秒轮询控制命令。恢复策略、启用币种或修改执行参数等命令可以
提前唤醒下一个策略周期。

## 7. LLM 子系统

### 7.1 Provider 边界

支持两类 Provider：

| Provider | API | 结构化输出 |
|---|---|---|
| `anthropic` | Messages API | `tool_use` 强制调用 `submit_decision` |
| `openai_compatible` | Chat Completions | function calling 强制调用 `submit_decision` |

Provider 只负责构造请求、调用 SDK、解析响应和连通性测试。重试、超时、审计、
symbol 纠正和失败降级统一留在 `LLMClient`。OpenAI 请求使用
`max_completion_tokens`；内部配置和 Anthropic 协议继续使用 `max_tokens` 语义。

### 7.2 安全语义

- `TradeDecision` 的 Pydantic Schema 是唯一允许的输出结构。
- 网络错误、超时、无工具调用、JSON 错误和字段越界都会降级为安全 HOLD。
- API Key 不会进入请求审计；Prompt、响应、耗时和错误会写入决策记录。
- Prompt 可使用版本化完整模板或兼容的附加指令模式。
- Prompt 热更新只影响下一次 LLM 调用，不中断正在进行的请求。
- Prompt 无法绕过后续硬风控。

### 7.3 Fallback 链

启用的 Profile 按 `priority` 升序组成调用内 fallback 链。每个源先耗尽自己的
`max_retries`，仍为 degraded 才尝试下一个源；全部失败才返回 HOLD。下一周期始终从
主源重新开始，实现自动 failback。

LLM Profile 和 Prompt 版本存储在环境独立的 SQLite 中。Profile 对外 API 只返回
`key_present` 和脱敏尾号；Engine 和 Provider 测试端点才读取完整 API Key。

## 8. 风控与执行边界

### 8.1 硬风控

开仓前至少检查：

- kill-switch 和全局暂停状态。
- 当日亏损和账户回撤。
- 最低置信度。
- 最大杠杆；超限直接拒单，不截断。
- 单笔、单 symbol 和全账户保证金占权益比例。
- 止损触发时的理论亏损占本订单保证金比例。
- 强平价距离和市场数据新鲜度。
- 残留条件单和交易所状态冲突。

`CLOSE`、保护性平仓和 kill-switch 不应被开仓额度阻止，但仍需遵守交易所数量和精度限制。

### 8.2 执行

执行层支持 MARKET_TAKER、MAKER_ONLY 和 MAKER_FIRST。普通退出与紧急退出可使用不同
模式；紧急退出固定为市价。执行层负责：

- client order id 和模糊结果恢复。
- 限频、网络错误的指数退避。
- maker 超时、重新报价和市价 fallback。
- 部分成交后的剩余量处理。
- 开仓后的 SL/TP 创建和保护单修复。
- 市价单滑点预估与拒绝。

执行层只接收通过风控的标准化决策，不自行放宽风险限制。

## 9. 策略持仓与外部成交

### 9.1 数据隔离

| 数据域 | 表 | 用途 |
|---|---|---|
| 策略订单与交易 | `orders`、`trades` | Engine 创建并管理的生命周期 |
| Binance 权威成交 | `exchange_fills` | 私有流和 REST 成交幂等账本 |
| 外部交易 | `external_trades`、`external_trade_fills` | Binance 网页、手机端或其他 API 产生的纯外部生命周期 |
| 开仓所有权 | `position_claims` | 处理交易所成交早于本地订单落库的竞态 |

成交按 `engine`、`external`、`mixed`、`unknown` 分类。`mixed` 和 `unknown` 只进入权威
账本，不生成外部交易，也不修改策略交易。外部交易不参与 LLM 复盘、策略胜率、策略
日盈亏或自动保护单修复。

### 9.2 未管理持仓

交易所存在持仓但本地没有 open trade 时：

- 若能匹配最近的策略 position claim，视为 Engine 开仓竞态产生的孤儿持仓，可建立
  `orphan_adoption` trade 并补保护单。
- 若没有可信的策略 claim，视为未管理外部持仓，禁用该 symbol 的新开仓。
- 未管理外部持仓不会被自动接管、补保护单或主动平仓。

私有流成交会立即写入权威账本；REST 在启动和周期对账时补偿断流缺口。历史数据可通过
`external-backfill` 先 dry-run，再显式 `--apply` 回填。

## 10. SQLite 数据域

SQLite 同时承担业务审计、实时投影、运行配置和进程间命令队列。主要表按职责分组：

| 分组 | 主要表 |
|---|---|
| 决策审计 | `decisions`、`rejects` |
| 策略交易 | `orders`、`trades`、`position_claims` |
| 外部成交 | `exchange_fills`、`external_trades`、`external_trade_fills` |
| 周期快照 | `position_snapshots`、`balance_snapshots`、`open_orders` |
| 实时账户投影 | `live_balances`、`live_positions`、`live_orders` |
| 交易所事件 | `exchange_events`、`exchange_stream_sessions`、`exchange_state_drifts` |
| 动态配置 | `symbols`、`runtime_settings`、`llm_profiles`、`llm_prompt_versions` |
| 控制面 | `control_commands` |

`Base.metadata.create_all()` 只创建缺失表；存量 SQLite 的新增列由
`Store._upgrade_schema()` 执行幂等兼容迁移。数据库迁移必须兼容旧库，不得假设生产库
可以重建。

## 11. Web 控制面

### 11.1 后端

FastAPI 提供以下能力：

- 登录、退出和当前用户查询。
- 汇总、私有流、持仓、决策、订单、交易、盈亏和行情查询。
- symbol 注册、启停和交易所预检。
- 风控、Engine、执行参数的预览和热更新。
- LLM Profile、fallback 链和 Prompt 版本管理。
- 受控命令入队、命令状态和 WebSocket 实时推送。
- `/healthz` 无业务副作用健康检查。

浏览器优先使用签名 HttpOnly Session Cookie，HTTP Basic Auth 保留给脚本兼容。未配置
`WEB_PASSWORD` 时拒绝访问。mainnet 高风险写操作要求短时确认令牌，令牌必须绑定具体
action 和 payload。

### 11.2 前端

Vue 前端使用 hash 路由，共享一套 UI，通过环境选择器将请求映射到
`/api/testnet/` 或 `/api/mainnet/`。主要页面包括总览、K 线、持仓、决策、交易记录、
盈亏、操作面板、运行参数和 LLM 配置。

前端可以发起命令和配置变更，但不能直接调用交易所交易接口。任何新增控制功能都必须
先定义 Engine 命令语义和服务端校验，再实现 UI。

## 12. 配置、密钥与运行时设置

### 12.1 静态配置

`config.yaml` 由 Pydantic 启动期强校验，未知字段直接报错。静态配置包含：

- 环境、symbols、账户模式和周期。
- 节流、风控、执行、LLM 工程参数。
- SQLite、私有流、通知和日志。

运行环境使用 `/etc/binance-trade/config.testnet.yaml` 和
`config.mainnet.yaml`。需要重启才能生效的工程参数应继续放在 YAML。

### 12.2 动态配置

Web 修改的策略暂停、symbol 状态、风控、Engine、执行参数、LLM Profile 和 Prompt
会持久化到 SQLite。Engine 启动时恢复这些值，因此不能只修改内存状态而不写库。

### 12.3 密钥

- Binance 和 Telegram 凭据从环境文件加载，不写入业务日志或数据库。
- LLM API Key 按环境存入权限为 `0600` 的 SQLite，并只通过脱敏 API 展示。
- Web 用户、密码和 Session Secret 从 Web 环境文件加载。
- `.env`、数据库、日志、证书私钥和交易所原始敏感响应禁止提交。

## 13. CLI 与常用开发命令

| 命令 | 用途 |
|---|---|
| `python main.py run` | 启动 Engine |
| `python main.py run --yes` | 跳过 mainnet CLI 二次确认，供 systemd 使用 |
| `python main.py kill-switch` | 撤销挂单、平仓并停止 |
| `python main.py backtest --symbol ... --csv ...` | 历史 K 线重放 |
| `python main.py external-backfill --days 30` | 外部成交 dry-run |
| `python main.py external-backfill --days 30 --apply` | 幂等写入外部成交账本 |
| `.venv/bin/python -m pytest -q` | 后端全量测试 |
| `npm run build` | 在 `web/frontend` 执行前端生产构建 |

本地开发使用 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 14. 贡献流程

### 14.1 开始修改前

1. 阅读目标模块、对应测试和相关 `docs/ops/` 记录。
2. 确认变更属于配置、状态、风控、执行、存储、Web 或部署中的哪一层。
3. 明确是否影响 testnet/mainnet 隔离、数据库兼容、命令队列或主网确认。
4. 检查工作区已有修改，禁止覆盖或混入无关变更。

### 14.2 实现约束

- 保持模块职责单一，优先扩展现有边界而不是在 Engine 中堆叠重复逻辑。
- 任何交易路径都必须保留“LLM 后、执行前”的硬风控。
- Web 写操作必须校验环境，并通过命令队列或已有的配置热更新机制生效。
- 涉及账户状态时优先使用 `AccountStateCoordinator` 投影，不另建事实来源。
- 涉及 SQLite 时同时考虑新库和存量库迁移。
- 交易所响应必须先标准化，避免业务层直接依赖 ccxt 或 Binance 原始字段。
- 新增外部成交处理不得修改策略交易记录或统计口径。
- 日志和审计数据不得包含密钥、认证信息或完整敏感响应。
- mainnet 的高风险操作不得降低确认强度或自动恢复开仓。

### 14.3 测试要求

| 变更类型 | 最低验证 |
|---|---|
| 纯函数、Schema、标准化 | 对应单元测试 |
| Engine、风控、执行、私有流、对账、存储 | 相关测试 + `.venv/bin/python -m pytest -q` |
| SQLite Schema | 新库创建、旧库补列、幂等重复启动测试 |
| FastAPI、状态查询 | Web server/status 相关测试 |
| Vue 页面或 API 调用 | `npm run build` |
| LLM Provider | 请求字段、解析、降级、fallback 和 secret 脱敏测试 |
| 部署或 systemd | `/opt` 同步比对、服务状态、`/healthz` 和启动日志 |

测试不得依赖真实主网下单。需要真实 API 连通性时，必须使用显式 dry-run 或 testnet，
并在变更记录中说明。

### 14.4 文档要求

交易、风控、执行、对账、私有流、数据库、部署、Web 或前端的高影响变更必须新增或更新
`docs/ops/YYYY-MM-DD-主题.md`，至少说明：

- 背景和问题。
- 设计选择与安全边界。
- 数据库或配置迁移。
- 测试和手工验证。
- 线上影响、发布步骤和回滚注意事项。

若实现改变本文描述的模块边界、数据流或运行拓扑，应同步更新本文件。

### 14.5 提交与发布检查清单

- [ ] 工作区只包含本次变更，没有密钥、数据库、日志或构建缓存。
- [ ] `git diff --check` 通过。
- [ ] 对应测试通过，高影响后端变更已跑全量 pytest。
- [ ] 前端变更已通过生产构建。
- [ ] 高影响变更已有 `docs/ops/` 记录。
- [ ] 提交信息使用简洁中文。
- [ ] 发布前已备份需要迁移的数据库。
- [ ] 代码从 `/data0/binance-trade` 同步到 `/opt/binance-trade`。
- [ ] 服务重启后四个实例状态符合预期。
- [ ] `/healthz`、私有流状态、决策心跳和启动日志已检查。
- [ ] mainnet 仍处于预期的暂停/启用状态，没有自动放开新仓。

## 15. 文档职责

| 文档 | 用途 |
|---|---|
| `README.md` | 项目简介和最短安装、运行入口 |
| `contributing.md` | 当前整体架构、边界和贡献流程 |
| `AGENTS.md` | 仓库内自动化协作和提交约束 |
| `SPEC.local.md` | 当前技术规范摘要和现网约束 |
| `docs/DEPLOY.md` | 服务器部署、目录和 systemd |
| `docs/RUNBOOK.md` | 启停、熔断、日志和故障排查 |
| `docs/ops/` | 每次高影响变更的设计与验证记录 |

发现文档与代码冲突时，以当前代码和生产配置为事实来源，并在同一变更中修正文档。
