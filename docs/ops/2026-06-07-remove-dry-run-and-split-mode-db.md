# 2026-06-07 移除 dry-run 模式并按交易环境隔离数据库

## 背景

项目早期同时支持 `dry_run`、testnet 和 mainnet。

随着交易记录改为按交易组展示，继续保留 dry-run 会带来两个问题：

- `orders` / `trades` 中同时存在模拟订单和 testnet 订单，页面容易把两类数据混在一起。
- 后续接入 mainnet 实盘时，如果仍共用 `data/trade.db`，testnet 与 mainnet 的决策、订单、持仓快照和运行态会混杂。

本次改造目标是：

- 删除 dry-run 运行模式。
- 当前 testnet 继续保留并只展示真实 testnet 数据。
- mainnet 使用独立 SQLite 文件，为后续实盘切换做数据隔离。

## 设计目标

- 运行模式只保留 `testnet` / `mainnet`。
- testnet 会向 Binance testnet 下单，不再生成模拟订单。
- mainnet 会向 Binance mainnet 下单，启动前仍保留二次确认。
- SQLite 按 mode 隔离：
  - `testnet` → `data/trade-testnet.db`
  - `mainnet` → `data/trade-mainnet.db`
- 保留 `orders.dry_run` / `trades.dry_run` 字段作为旧库兼容字段，但新订单固定写入 `false`。
- 删除 Web 控制面板中的 dry-run 切换按钮，避免运行态和页面状态出现歧义。

## 配置变更

旧配置：

```yaml
execution:
  dry_run: false

storage:
  db_path: ./data/trade.db
```

新配置：

```yaml
execution:
  order_type: MARKET
  attach_sl_tp: true
  rate_limit_backoff: 1.5
  max_order_retries: 3
  recv_window: 5000

storage:
  db_path_template: ./data/trade-{mode}.db
  reconcile_on_start: true
```

`StorageConfig` 仍兼容旧字段 `db_path`，用于历史配置或测试，但正式配置使用 `db_path_template`。

`Settings` 在加载后把最终路径写回 `settings.storage.db_path`，因此业务代码仍通过同一个字段读取最终 DB。

## 执行层变更

`Executor` 删除 dry-run 分支后：

- `open_position()` 会在精度规整通过后调用 `setup_symbol()` 和 `create_order()`。
- `place_sl_tp()` / `place_protection_orders()` 会真实创建 reduce-only 条件单。
- `close_position()` 会真实创建 reduce-only 市价平仓单。
- `cancel_all_orders()` 会真实撤普通挂单和条件单。
- 返回结构仍包含 `dry_run: false`，仅用于兼容原有落库字段。

`_FILLED_STATES` 从：

```python
("filled", "partial", "dry_run")
```

改为：

```python
("filled", "partial")
```

## Engine 变更

删除以下运行态能力：

- `SET_DRY_RUN` 控制命令。
- 启动时读取 `runtime_settings.execution.dry_run`。
- Web API 返回的 `dry_run`、`dry_run_source`、`dry_run_config`。

保留以下运行态能力：

- `strategy.paused`
- `symbol.enabled.<SYMBOL>`
- `RESUME_ALL_SYMBOLS`
- `REPAIR_SL_TP`
- `CANCEL_AND_FLATTEN`
- `STOP_ENGINE`
- `KILL_SWITCH`

缺失止损保护逻辑也随之收紧：

- 开仓后如果策略要求 SL，但 SL 未确认 `placed`，会禁用该币种并尝试应急平仓。
- 不再因为 dry-run 跳过该保护逻辑。

## Web 变更

控制面板删除“下单模式”卡片，新增“运行环境”展示：

- 当前 `mode`
- 当前 `db_path`

Dashboard 运行状态只展示：

- `testnet` / `mainnet`
- 测试网 / 主网标签

订单流水页删除“模拟 / 真实”模式列。

## 数据迁移

迁移前生产库：

```text
data/trade.db
orders=69
orders_dry_run=9
trades=18
trades_dry_run=3
runtime_settings.execution.dry_run=1
```

dry-run 数据明细：

```text
orders:
1 ETHUSDT OPEN dry_run trade_id=1
2 ETHUSDT SL   dry_run trade_id=1
3 ETHUSDT TP   dry_run trade_id=1
4 ETHUSDT OPEN dry_run trade_id=2
5 ETHUSDT SL   dry_run trade_id=2
6 ETHUSDT TP   dry_run trade_id=2
7 BNBUSDT OPEN dry_run trade_id=3
8 BNBUSDT SL   dry_run trade_id=3
9 BNBUSDT TP   dry_run trade_id=3

trades:
1 ETHUSDT short open dry_run entry_order_id=1
2 ETHUSDT short open dry_run entry_order_id=4
3 BNBUSDT short open dry_run entry_order_id=7
```

因此清理时必须删除 9 条订单和 3 条交易组，不能只删除最开始误认为的 4 条单笔订单。

执行步骤：

```text
1. stop binance-trade.service
2. stop binance-trade-web.service
3. 备份 data/trade.db
4. 复制 data/trade.db → data/trade-testnet.db
5. 在 data/trade-testnet.db 中删除 dry-run 记录
6. 初始化空 data/trade-mainnet.db
7. 启动服务
8. 验证 API 和 DB 计数
```

备份文件：

```text
data/backups/trade-before-remove-dry-run-split-db-20260607-141955.db
sha256=7f9ee05e0c7eb7a586e382f837e3d203a45a5a16a41d2bd4dba73ab557436e10
size=11075584
```

清理 SQL：

```sql
DELETE FROM orders WHERE dry_run = 1;
DELETE FROM trades WHERE dry_run = 1;
DELETE FROM runtime_settings WHERE key = 'execution.dry_run';
```

清理后 testnet 库：

```text
data/trade-testnet.db
orders=60
orders_dry_run=0
trades=15
trades_dry_run=0
runtime_settings.execution.dry_run=0
unlinked_orders=0
integrity_check=ok
```

mainnet 空库：

```text
data/trade-mainnet.db
orders=0
trades=0
integrity_check=ok
```

旧 `data/trade.db` 暂时保留，不再由当前配置读取，用作一个部署周期内的人工回滚参考。

## 涉及文件

- `config.yaml`
- `config.yaml.example`
- `main.py`
- `src/config/schema.py`
- `src/config/loader.py`
- `src/execution/executor.py`
- `src/engine/loop.py`
- `src/store/models.py`
- `src/store/repo.py`
- `web/server.py`
- `web/status.py`
- `web/frontend/src/views/Control.vue`
- `web/frontend/src/views/Dashboard.vue`
- `web/frontend/src/views/Orders.vue`
- `tests/conftest.py`
- `tests/test_config_schema.py`
- `tests/test_executor.py`
- `tests/test_engine.py`
- `tests/test_store.py`
- `tests/test_web_status.py`
- `README.md`
- `docs/DEPLOY.md`
- `docs/RUNBOOK.md`
- `docs/WEB_SETUP.md`
- `docs/ops/2026-06-05-exchange-reconcile-hardening.md`

## 验证

后端测试：

```bash
.venv/bin/python -m pytest
```

结果：

```text
167 passed
```

前端构建：

```bash
cd web/frontend
npm run build
```

结果：

```text
✓ built
```

构建过程中出现 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，与既有构建警告一致，不影响产物生成。

服务验证：

```text
systemctl is-active binance-trade.service      → active
systemctl is-active binance-trade-web.service  → active
```

日志确认：

```text
store connected: ./data/trade-testnet.db
engine started (mode=testnet, db=./data/trade-testnet.db, equity=5024.54)
```

API 验证：

```text
GET /api/config
mode=testnet
db_path=./data/trade-testnet.db
dry_run 字段不存在

GET /api/trades?limit=1
total=15
```

## 回滚方案

代码回滚：

```bash
git checkout <上一稳定提交>
```

数据回滚到本次变更前：

```bash
systemctl stop binance-trade.service
systemctl stop binance-trade-web.service
cp data/backups/trade-before-remove-dry-run-split-db-20260607-141955.db data/trade.db
systemctl start binance-trade.service
systemctl start binance-trade-web.service
```

如果只回滚当前版本的数据隔离：

```bash
systemctl stop binance-trade.service
systemctl stop binance-trade-web.service
cp data/backups/trade-before-remove-dry-run-split-db-20260607-141955.db data/trade-testnet.db
systemctl start binance-trade.service
systemctl start binance-trade-web.service
```

注意：第二种只恢复 testnet 隔离库，不恢复旧代码中的 dry-run 开关。

## 运维注意

- 以后 testnet 与 mainnet 必须分别备份：
  - `data/trade-testnet.db`
  - `data/trade-mainnet.db`
- 切换 `mode` 前必须确认 `.env` 中 Binance API key 与目标环境匹配。
- mainnet 不再有 dry-run 保护层，启动前必须完成风控阈值复核。
- Web 控制面板不再支持切换下单模式，只支持暂停、恢复、币种开关、补保护单、撤单平仓、停止引擎和 Kill Switch。
