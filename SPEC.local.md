# binance-trade 当前技术规范

本文档描述仓库当前已经落地的真实架构、运行逻辑和操作边界。它用于指导后续改动、排障和部署，不再保留早期概念稿。

## 1. 目标与原则

- 目标：在 Binance USD-M 永续合约上运行 LLM 驱动的自动交易机器人。
- 原则：LLM 只给建议，Python 代码负责执行和风控；风控优先级高于 LLM。
- 原则：`testnet` 与 `mainnet` 逻辑一致，但运行态、数据库、密钥和服务实例完全隔离。
- 原则：账户状态以 Binance 原生私有 User Data Stream 为主，REST 只做基线、恢复和审计。

## 2. 当前运行拓扑

```text
/data0/binance-trade     唯一源码仓库
/opt/binance-trade       root 管理的发布副本
/etc/binance-trade       环境配置与密钥
/var/lib/binance-trade   分环境 SQLite 数据库
/var/log/binance-trade   分环境文件日志
```

服务实例：

- `binance-trade@testnet`
- `binance-trade@mainnet`
- `binance-trade-web@testnet`
- `binance-trade-web@mainnet`

运行用户：

- engine: `trader`
- web: `trader`

监听端口：

- testnet web: `127.0.0.1:8000`
- mainnet web: `127.0.0.1:8001`

## 3. 环境分工

### 3.1 testnet

- 用于功能验证、回归测试和操作演练。
- 默认跟随与 mainnet 相同的代码路径和前端界面。
- 拥有独立数据库 `trade.db`、独立日志目录和独立配置文件。

### 3.2 mainnet

- 用于真实交易。
- 每次启动后默认进入新开仓保护态，需要人工确认恢复。
- 高风险命令和参数变更需要二次确认令牌。
- 任何不明确的私有事件默认 fail-closed。

## 4. 数据与状态

### 4.1 SQLite

- 交易引擎和 Web 都读取环境独立的 SQLite。
- 数据库记录决策、拒单、订单、成交、持仓快照、余额快照、命令历史、私有事件和运行时设置。
- `testnet` 和 `mainnet` 数据库不得互相复制或混用。

### 4.2 运行态

- 引擎运行态由单写入者维护。
- 重要运行态包括：策略是否暂停、风控版本、当日盈亏、回撤、私有流状态、主网重启守卫。
- 前端只是读取和发命令，不直接修改底层交易状态。

## 5. 交易引擎逻辑

### 5.1 主循环

主循环按周期运行，典型节流周期为 5 分钟。链路为：

```text
行情与账户状态 -> 特征构建 -> 节流判定 -> LLM 决策
-> pydantic 校验 -> 风控校验 -> 精度规整 -> 执行 -> 落库 -> 告警
```

### 5.2 节流

- 不是每个周期都调用 LLM。
- 只有满足显著行情变化、持仓状态变化、挂单变化或达到最大跳过周期数时，才触发 LLM。
- 跳过周期必须写心跳日志，说明跳过原因。

### 5.3 风控

- 杠杆上限、单笔保证金上限、单币种保证金上限、总保证金上限、单笔止损上限、日亏熔断、回撤熔断、最小置信度都由配置控制。
- 杠杆超限必须拒单，不允许静默截断。
- 风控永远优先于 LLM。
- 回撤或日亏熔断触发后，策略应进入保护态并阻止新开仓。

### 5.4 执行

- 开仓前要确保目标账户模式和杠杆设置正确。
- 下单前必须完成 tickSize、stepSize、minNotional 等精度规整。
- 订单失败、部分成交、撤单失败都要进入日志和数据库审计。

## 6. 私有流逻辑

### 6.1 主通道

- 账户余额、持仓、订单状态、账户配置变化通过 Binance 原生 USD-M User Data Stream 接收。
- REST 只用于启动基线、对账、重连恢复和审计。
- 私有流断线时不应盲目假设账户状态为静止。

### 6.2 事件分类

当前已知分类包括：

- `ORDER_TRADE_UPDATE`
- `ACCOUNT_UPDATE`
- `ACCOUNT_CONFIG_UPDATE`
- `CONDITIONAL_ORDER_TRIGGER_REJECT`
- `MARGIN_CALL`

其中：

- `ACCOUNT_CONFIG_UPDATE` 已按杠杆更新和 multi-assets 更新分类处理。
- 杠杆在硬上限内只记录和对账，不暂停。
- 超限或未知结构仍应 fail-closed。
- `CONDITIONAL_ORDER_TRIGGER_REJECT` 保持高风险处理。

## 7. Web 与前端

### 7.1 控制方式

- Web 不直接操作交易所。
- Web 通过命令队列让引擎执行暂停、恢复、平仓、撤单、修复、风控参数调整等操作。
- mainnet 高风险写操作需要额外确认。

### 7.2 环境切换

- 前端支持 testnet / mainnet 切换。
- 实盘页面与测试页面共享同一套 UI 代码，但数据源和确认逻辑分离。

### 7.3 展示逻辑

- 桌面端保留表格密度。
- 移动端优先采用卡片式信息块，避免固定列遮挡和按钮挤压。
- 决策、订单、持仓、控制面板都应在窄屏下可读、可点、可滚动。

## 8. 配置与密钥

- 运行配置位于 `/etc/binance-trade/config.testnet.yaml` 和 `/etc/binance-trade/config.mainnet.yaml`。
- 引擎密钥位于 `/etc/binance-trade/testnet.env` 和 `/etc/binance-trade/mainnet.env`。
- Web 密钥位于 `/etc/binance-trade/web-testnet.env` 和 `/etc/binance-trade/web-mainnet.env`。
- 密钥文件只读给 `trader`，不可提交到仓库。
- LLM Profile 密钥走 Web 看板动态管理，不应写死在前端源码中。

## 9. 部署与同步

- 开发和测试都在 `/data0/binance-trade` 完成。
- 发布只允许从 `/data0/binance-trade` 单向同步到 `/opt/binance-trade`。
- 同步后要重启对应的 systemd 实例并检查状态。
- 不要在 `/opt/binance-trade` 里手工改代码。

## 10. 文档与审计

- 涉及代码、风控、执行、部署、私有流、Web 的改动，必须补 `docs/ops/` 记录。
- 记录应包含背景、改动、验证、线上影响和回滚注意事项。
- 提交前至少跑一次相关测试或构建。

## 11. 现网注意事项

- mainnet 现在有重启保护态，不能把它当成普通 testnet。
- 任何“看起来像配置变更”的私有事件都要先分类，再决定是否暂停。
- 交易所状态和本地状态不一致时，优先以私有流+对账结果修正本地投影。
- 账户模式、杠杆、风险参数、币种启停都属于受控变更，不应通过前端绕过引擎直接修改。

