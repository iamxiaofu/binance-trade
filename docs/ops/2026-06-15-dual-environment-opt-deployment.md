# 2026-06-15 testnet/mainnet 统一迁移到 trader + /opt 双环境部署

## 背景

迁移前存在两套不同的运行方式：

- testnet 使用旧单实例服务，以 root 从 `/data0/binance-trade` 直接运行。
- mainnet 使用模板服务，以 `trader` 从 `/opt/binance-trade` 运行。
- testnet 数据库、日志和密钥仍位于源码仓库内，部署、开发和运行态边界不清晰。
- 仓库同时保留旧单实例 systemd 文件和新模板文件，容易误部署。

本次目标是统一 testnet/mainnet 的部署模型，同时保留 `/data0/binance-trade`
作为唯一源码仓库。

## 新技术架构

```text
/data0/binance-trade                  唯一 Git 源码仓库
          |
          | 测试通过后 rsync 发布，排除密钥/数据库/日志/.git
          v
/opt/binance-trade                    root 管理的只读发布副本
          |
          +-- binance-trade@testnet   trader，testnet engine
          +-- binance-trade@mainnet   trader，mainnet engine
          +-- binance-trade-web@testnet -> 127.0.0.1:8000
          +-- binance-trade-web@mainnet -> 127.0.0.1:8001

/etc/binance-trade
          +-- config.testnet.yaml / testnet.env / web-testnet.env
          +-- config.mainnet.yaml / mainnet.env / web-mainnet.env

/var/lib/binance-trade
          +-- testnet/trade.db
          +-- mainnet/trade.db

/var/log/binance-trade
          +-- testnet/
          +-- mainnet/

nginx TLS :443
          +-- /api/testnet/*, /ws/testnet -> 8000
          +-- /api/mainnet/*, /ws/mainnet -> 8001
          +-- / -> testnet Web 提供的同一份前端静态资源
```

## 目录与权限

| 路径 | 权限模型 | 说明 |
|---|---|---|
| `/opt/binance-trade` | `root:root 0755` | `trader` 只读执行，运行时不得写源码 |
| `/etc/binance-trade/*.yaml` | `root:trader 0640` | 非敏感运行配置 |
| `/etc/binance-trade/*.env` | `root:trader 0640` | Binance/Web 密钥 |
| `/var/lib/binance-trade/<env>` | `trader:trader 0700` | 环境独立 SQLite |
| `/var/lib/binance-trade/<env>/trade.db` | `trader:trader 0600` | 含 LLM Profile 密钥 |
| `/var/log/binance-trade/<env>` | `trader:trader 0700` | 环境独立日志 |

## systemd 设计

正式服务只保留两个模板：

- `binance-trade@.service`
- `binance-trade-web@.service`

实例名 `%i` 只能使用 `testnet` 或 `mainnet`，并映射到：

- `/etc/binance-trade/config.%i.yaml`
- `/etc/binance-trade/%i.env`
- `/etc/binance-trade/web-%i.env`
- `/var/lib/binance-trade/%i`
- `/var/log/binance-trade/%i`

旧的 `binance-trade.service`、`binance-trade-web.service` 和
`binance-trade-frontend.service` 已退出正式部署，避免 root 进程继续直接使用源码仓库。

## 数据迁移

testnet 迁移必须在旧 engine 和 web 均停止后执行：

1. 停止旧服务，确认进程退出。
2. 对 `data/trade-testnet.db` 执行一致性检查并生成备份。
3. 复制为 `/var/lib/binance-trade/testnet/trade.db`。
4. 设置 `trader:trader 0600`。
5. 使用新 Web 实例先打开数据库并检查 LLM Profile、运行态和命令队列。
6. 启动新 engine，验证私有流、REST 对账和账户状态。

testnet 的决策、交易、LLM Profile、动态风控和币种启用状态全部随 SQLite 原样迁移。
源码仓库里的旧数据库保留为迁移前快照，但新服务不再读取它。

## Nginx 评估

迁移前后 Web 端口保持不变：

- testnet `127.0.0.1:8000`
- mainnet `127.0.0.1:8001`

因此 Nginx 路由不需要修改。迁移后必须执行 `nginx -t`，并验证：

- `/api/testnet/config` 返回 `mode=testnet`
- `/api/mainnet/config` 返回 `mode=mainnet`
- 两套 WebSocket 路由能够建立连接

## 安全行为

- 两套 engine 启动前都校验 One-way Mode，并拒绝 Multi-assets Mode。
- mainnet 每次重启自动写入 `MAINNET_RESTART_GUARD` 并暂停新开仓。
- 私有 User Data Stream 是实时账户状态主通道；REST 用于启动基线、断线恢复和周期审计。
- LLM Profile 按环境存储在对应 SQLite，env 不再提供固定 LLM Key 或固定中转 URL。
- Web 主网高风险操作要求一次性 `MAINNET` 确认令牌。

## 发布与回滚

发布只允许从 `/data0/binance-trade` 到 `/opt/binance-trade` 单向同步，并排除：

```text
.git
.env*
.venv
data/
logs/
node_modules/
```

回滚代码时不得覆盖 `/etc`、`/var/lib` 或 `/var/log`。数据库恢复必须按环境单独停止
engine 与 web 后执行，禁止 testnet/mainnet 互相复制数据库。

## 验证

- 后端全量测试：`pytest -q`
- 前端生产构建：`npm run build`
- `git diff --check`
- 两套 Binance REST 与私有流连接
- 两套 `/api/<env>/config`、`summary`、`stream-status`、`llm/status`
- systemd 进程用户必须为 `trader`
- `/proc/<pid>/cwd` 必须指向 `/opt/binance-trade`
- Nginx `nginx -t` 与双环境反代

## 本次迁移结果

- 后端全量测试：`339 passed`
- 前端生产构建：通过
- testnet 源数据库与迁移后运行库 SHA-256 一致：
  `694d35576acf5494e8f08c1aff66ac70747d73be2233f9f3990a17b60f16ea0c`
- testnet 迁移前 SQLite 备份：
  `/var/lib/binance-trade/testnet/backups/trade-pre-opt-migration-20260615-213108.db`
- 两套数据库 `PRAGMA integrity_check=ok`
- 两套 engine 和 Web 均由 `trader` 从 `/opt/binance-trade` 运行并启用开机自启
- 两套私有流均为 `LIVE`，启动对账后无持仓、无挂单、无状态漂移
- testnet 原有 LLM Profile、历史数据和四币种禁用状态完整保留
- mainnet 启动后自动进入 `MAINNET_RESTART_GUARD`，没有恢复真实交易
- Nginx 端口和路径保持不变，`nginx -t` 通过，因此没有修改线上 Nginx 配置

testnet Web 当前沿用已有 testnet 交易 Key。Web 进程本身只执行读取接口，但要达到与
mainnet 完全一致的最小权限模型，仍应在 Binance testnet 侧创建独立只读 Key，并替换
`/etc/binance-trade/web-testnet.env`。
