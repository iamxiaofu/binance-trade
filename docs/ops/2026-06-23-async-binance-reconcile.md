# 2026-06-23 Binance 成交核对改为后台任务

## 现象与根因

主网点击“一键核对修复”后两次返回 504。Nginx 访问日志显示：

- 请求均为 `POST /api/mainnet/trades/reconcile/preview`；
- `request_time` 分别为 60.002 秒和 60.001 秒；
- 上游 Web 在约 60.9 秒后才保存预览版本；
- Nginx `/api/mainnet/` 没有单独设置 `proxy_read_timeout`，使用默认 60 秒。

因此 504 只表示浏览器与 Nginx 等待超时，不表示核对失败。两次后台执行都完成并生成了
预览版本，但旧接口无法把结果返回给已断开的浏览器。

主要耗时来自 30 天核对期间逐日查询 income、分币种拉取成交，以及逐订单查询 Binance
订单元数据。当前 30 天范围约有 186 笔成交、140 个不同订单。

## 后台任务

预览接口改为：

1. `POST /api/trades/reconcile/preview` 创建持久化任务并立即返回 HTTP 202；
2. 返回 `task_id/status/stage/progress_pct/detail`；
3. Web 后台任务串行执行 Binance 核对；
4. 前端轮询 `GET /api/trades/reconcile/tasks/{task_id}`；
5. 成功后任务结果包含原有 `run_id/preview_hash/summary`，自动打开确认窗口。

新增 `exchange_reconcile_tasks` 表，任务状态包括：

- `queued`
- `running`
- `succeeded`
- `failed`
- `canceled`

任务阶段覆盖本地成交读取、币种发现、远端成交拉取、订单元数据、归属解析、周期重建、
仓位校验和预览持久化。

页面刷新后读取 `GET /api/trades/reconcile/tasks/latest`，可恢复正在执行的进度。Web
进程重启会把遗留的 queued/running 任务明确标记为 failed，避免界面永久显示运行中。

## 幂等与并发

- 单环境只允许一个 active task，通过数据库唯一 `active_slot` 保证，不能依赖单进程内锁；
- 双击或浏览器重试复用当前 active task；
- 同核对天数在五分钟内完成的成功任务直接复用结果；
- Apply 仍使用 `run_id + preview_hash + MAINNET confirmation`；
- Apply 前重新拉取 Binance 数据并校验哈希，数据变化时拒绝应用；
- 预览任务不修改已激活交易记录。

## 性能优化

已激活核对版本会把权威订单元数据写入 `exchange_fills.resolved_*`。后续预览优先复用：

- `resolved_client_order_id`
- `resolved_reduce_only`
- `resolved_order_type`
- `resolved_algo_id`
- `resolved_metadata_source`

仅新增成交或仍缺少权威元数据的订单才请求 Binance order API。分批止盈、分批止损、
部分平仓仍按逐 fill 元数据重建，不通过数量猜测归属。

## 代理兜底

预览接口已异步化，不再依赖长代理超时。Apply 仍需做完整二次校验，因此为以下精确路径
配置 300 秒超时：

- `/api/mainnet/trades/reconcile/apply`
- `/api/testnet/trades/reconcile/apply`

不扩大其他 API 的超时范围。

## 安全边界

- 后台预览失败只记录错误，不修改交易数据；
- 同时只有一个核对任务；
- Apply 前自动备份 SQLite；
- Apply 使用数据库事务激活新版本；
- 外部订单、Engine 订单、分批 TP/SL 和混合生命周期继续使用订单 ID、client ID、
  reduce-only、私有流和 Binance order 元数据判定；
- 主网部署后保持 `MAINNET_RESTART_GUARD`，不自动恢复交易。
