# 2026-06-07 动态新增交易币种

## 背景

项目原来只能交易 `config.yaml` 中配置的 `symbols`：

```yaml
symbols:
  - BTCUSDT
  - ETHUSDT
  - BNBUSDT
```

Web 控制面板只能对这些静态币种执行启用/停用。

后续需要在 testnet 或 mainnet 中动态新增交易币种，例如 `SOLUSDT`。
如果只让用户手动填写币种并直接生成空持仓数据，会有风险：

- 交易所中可能已经存在该币种持仓。
- 交易所中可能已经存在普通挂单或 SL/TP 条件单。
- 币种的 `minQty`、`minNotional`、`tickSize`、`stepSize` 不应人工猜测。
- testnet 与 mainnet 已经拆成独立数据库，新增币种也必须跟随当前交易环境隔离。

## 设计目标

- 用户只输入币种，例如 `SOLUSDT`。
- 系统按当前交易环境自动到交易所验证币种是否存在。
- 系统自动拉取该币种的交易过滤器，不要求人工填写最小数量、最小名义价值、价格精度或数量精度。
- 新增币种默认 `enabled=false`，不会立即请求 LLM 或下单。
- 新增时先查询交易所当前持仓、普通挂单和条件单。
- 只有交易所确认无持仓、无普通挂单、无条件单时，才写入确认过的空仓快照。
- 如果交易所已有持仓或挂单，币种仍会注册，但标记 `needs_review=true`，禁止直接启用。
- 不为每个币种新增独立业务表，所有决策、订单、交易组、持仓快照仍使用现有 `symbol` 字段关联。
- testnet/mainnet 数据继续通过独立 SQLite 文件隔离。

## 数据结构

新增表：`symbols`

字段：

- `symbol`：币种，主键，例如 `SOLUSDT`。
- `enabled`：该币种是否允许策略交易。
- `status`：注册状态，目前使用 `active` / `archived`。
- `sync_status`：最近一次交易所预检状态。
- `needs_review`：是否需要人工复核。
- `source`：来源，`config` 或 `web`。
- `min_qty`：交易所最小下单数量。
- `min_notional`：交易所最小名义价值。
- `tick_size`：价格最小变动。
- `step_size`：数量最小变动。
- `raw_filters_json`：交易所过滤器原始摘要。
- `exchange_state_json`：新增时同步到的交易所状态。
- `added_at` / `updated_at` / `last_filter_sync_at`：审计时间。

旧表保持不变：

- `decisions.symbol`
- `orders.symbol`
- `trades.symbol`
- `open_orders.symbol`
- `position_snapshots.symbol`

## 迁移策略

项目当前仍使用 `Store.connect()` 中的轻量 SQLite 迁移。

本次迁移流程：

1. `Base.metadata.create_all` 创建 `symbols` 表。
2. 通过 `PRAGMA table_info(symbols)` 检查既有列。
3. 对缺失列执行幂等 `ALTER TABLE symbols ADD COLUMN ...`。
4. 启动时调用 `sync_config_symbols()`，把 `config.yaml` 中的旧币种 seed 到 `symbols` 表。
5. 如果旧库已有 `runtime_settings.symbol.enabled.<SYMBOL>`，首次 seed 时保留旧启停状态。

该流程可重复执行，服务多次启动不会重复建表或覆盖已有动态币种。

## 新增币种流程

Web 控制面板新增币种时：

1. 用户输入 `SOLUSDT`。
2. Web 端写入命令队列：

```text
ADD_SYMBOL SOLUSDT
```

3. 交易主进程消费命令。
4. `ExchangeClient.ensure_symbol()` 加载交易所 market 并解析 filters。
5. 交易主进程查询：
   - 当前持仓：`fetch_positions([symbol])`
   - 普通挂单：`fetch_open_orders(symbol)`
   - 条件单：`fetch_open_condition_orders(symbol)`
6. 根据交易所状态落库：
   - 无持仓、无普通挂单、无条件单：`sync_status=confirmed_flat`，`needs_review=false`。
   - 有持仓：`sync_status=live_position_found`，`needs_review=true`。
   - 有普通挂单：`sync_status=open_orders_found`，`needs_review=true`。
   - 有条件单：`sync_status=condition_orders_found`，`needs_review=true`。
7. 新增币种固定 `enabled=false`。
8. 刷新该币种行情快照。
9. Web 端刷新 `/api/config`，展示新增币种状态。

## 人工复核流程

新增命令：

```text
REVIEW_SYMBOL SOLUSDT
```

适用场景：

- 新增币种时发现已有持仓。
- 新增币种时发现普通挂单。
- 新增币种时发现条件单。
- 用户已经人工处理交易所状态，希望系统重新确认是否可以解除阻断。

复核流程：

1. Web 端只写命令队列，不直接查询交易所。
2. 交易主进程消费 `REVIEW_SYMBOL`。
3. 交易主进程重新查询交易所当前状态：
   - 当前持仓
   - 普通挂单
   - 条件单
4. 更新 `exchange_state_json`。
5. 更新该币种持仓快照。
6. 如果仍有普通挂单或条件单，更新未完成挂单快照。
7. 重新刷新该币种行情快照。
8. 根据最新交易所状态更新 `sync_status` 和 `needs_review`。

复核结果：

- 交易所已经无持仓、无普通挂单、无条件单：
  - `sync_status=confirmed_flat`
  - `needs_review=false`
  - `enabled=false`
  - 页面允许后续手动启用交易
- 交易所仍有持仓：
  - `sync_status=live_position_found`
  - `needs_review=true`
  - `enabled=false`
  - 页面继续禁止启用交易
- 交易所仍有普通挂单：
  - `sync_status=open_orders_found`
  - `needs_review=true`
  - `enabled=false`
  - 页面继续禁止启用交易
- 交易所仍有条件单：
  - `sync_status=condition_orders_found`
  - `needs_review=true`
  - `enabled=false`
  - 页面继续禁止启用交易

注意：

- `REVIEW_SYMBOL` 不会自动启用交易。
- `REVIEW_SYMBOL` 不会自动撤单、平仓或补单。
- `REVIEW_SYMBOL` 不会把已有持仓自动接管成本地交易组。
- 如果后续要支持已有持仓接管，应单独设计“接管持仓”流程。

## 启停规则

`SET_SYMBOL_ENABLED` 从“校验 config symbols”改为“校验 symbols 注册表”。

启用前检查：

- 币种必须存在于 `symbols` 表。
- `status` 必须是 `active`。
- `needs_review=true` 时拒绝启用。

停用仍允许执行，用于立即停止该币种策略新决策。

## Engine 变更

主循环、对账、快照和全局风险动作不再固定遍历 `settings.symbols`。

当前动态范围来自：

```text
symbols.status = active
```

涉及路径：

- 周期行情刷新。
- 每个币种的 LLM 决策循环。
- 启动对账。
- 周期性交易所对账。
- 持仓快照。
- `RESUME_ALL_SYMBOLS` 前置检查。
- `CANCEL_AND_FLATTEN`。
- `KILL_SWITCH`。
- 熔断平仓。

`RESUME_ALL_SYMBOLS` 只启用 `needs_review=false` 的 active 币种。
需要人工复核的币种不会被批量启用。

## Web 变更

新增接口：

```text
GET /api/symbols
```

`GET /api/config` 中：

- `symbols` 改为来自 `symbols` 表。
- `symbols_state` 返回注册表详情。
- `symbol_enabled` 对 `needs_review=true` 的币种返回 `false`。

控制面板新增：

- 新增币种输入框。
- 新增币种按钮。
- 重新复核按钮，仅在 `needs_review=true` 时展示。
- 同步状态列。
- 最小数量列。
- 最小名义价值列。
- 来源列。

`needs_review=true` 的币种启用按钮禁用。

## 兼容旧数据

兼容策略：

- `config.yaml` 中的旧币种会自动 seed 到 `symbols` 表。
- 旧的 `runtime_settings.symbol.enabled.<SYMBOL>` 会被同步为注册表启停状态。
- 旧业务数据不迁移、不改写、不删除。
- 新增币种不会创建独立业务表。
- 旧页面读取 `/api/config.symbols` 仍能拿到币种列表，只是来源变为动态注册表。

## 运行环境隔离

新增币种写入当前 mode 对应的 SQLite 文件：

- `testnet`：`data/trade-testnet.db`
- `mainnet`：`data/trade-mainnet.db`

因此在 testnet 新增 `SOLUSDT` 不会影响 mainnet。
后续主网页面切换时，应按当前 mode 读取对应数据库的 `symbols` 表。

## 回滚方案

代码回滚：

1. 回退到本次变更前 commit。
2. 重启 `binance-trade.service` 和 `binance-trade-web.service`。

数据库回滚：

1. 停止服务。
2. 用变更前备份覆盖当前 DB。
3. 重启服务。

如果只回滚代码但不回滚数据库，旧代码会忽略新增的 `symbols` 表，不影响旧表读取。
但动态新增币种功能不可用，页面仍只显示 `config.yaml` 中的币种。

## 验证

后端全量测试：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
172 passed
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

构建过程中仍有 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
属于依赖包注释位置提示，不影响前端产物生成。

## 运维注意

- 新增币种不会自动启用。
- 如果新增时交易所存在持仓或挂单，必须先在交易所侧人工处理，再点击“重新复核”。
- “重新复核”只会在交易所确认干净后清除 `needs_review`，不会自动接管已有持仓。
- 新增币种的交易所 filters 会写入 `symbols` 表，可用于后续排查精度或最小名义价值问题。
- Web 只写命令队列，真实交易所查询仍由交易主进程串行执行。
