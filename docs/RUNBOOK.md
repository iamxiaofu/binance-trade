# 运行手册（Runbook）

日常运维 binance-trade：启停、紧急熔断、看日志、复盘决策。
假设以 systemd 托管、工作目录 `/opt/binance-trade`、运行用户 `trader`。

---

## 启动 / 停止 / 重启

```bash
sudo systemctl start   binance-trade     # 启动
sudo systemctl stop    binance-trade     # 停止（优雅：先撤单+平仓再退出）
sudo systemctl restart binance-trade     # 重启
sudo systemctl status  binance-trade     # 查看状态
sudo systemctl enable  binance-trade     # 开机自启
sudo systemctl disable binance-trade     # 取消自启
```

`stop` 会向进程发 `SIGTERM`，main.py 捕获后执行撤单 + 平仓再退出，`TimeoutStopSec=60`。

## 紧急熔断（Kill Switch）

有三种手段，按场景选择：

1. **代码级 kill（推荐，会撤单+平仓）**
   ```bash
   sudo systemctl stop binance-trade          # SIGTERM → 优雅平仓
   # 或独立执行一次性 kill 命令（不依赖主进程在跑）：
   sudo -iu trader bash -lc 'cd /opt/binance-trade && source .venv/bin/activate && python main.py kill-switch'
   ```
   `kill-switch` 子命令：撤掉所有挂单 + 平掉所有持仓 + 推送告警。

2. **配置级急停（停开新仓，不自动平仓）**
   把 `config.yaml` 的 `execution.dry_run` 改回 `true` 后 `restart`：之后只模拟不下真单。

3. **交易所侧兜底**：登录币安手动平仓 + 在 API 管理页**临时禁用 key**。
   这是最后防线，确保即使进程失控也能切断下单能力。

> dry_run 模式下 `kill-switch` 只记录不下真单，可安全演练。

## 看日志

```bash
# systemd 聚合日志（实时）
sudo journalctl -u binance-trade -f
sudo journalctl -u binance-trade --since "1 hour ago"

# 文件日志（loguru 轮转，见 config.yaml logging.dir）
tail -f /opt/binance-trade/logs/binance-trade_$(date +%F).log
# systemd 重定向的 stdout/stderr
tail -f /var/log/binance-trade/stdout.log /var/log/binance-trade/stderr.log
```

关注的关键日志：
- `engine started` / `markets loaded` — 启动正常
- `[skip-llm] <sym> reason=...` — 节流跳过（心跳，正常）
- `[reject] <sym> ...` — 风控拒单（含原因，如 `LEVERAGE_EXCEEDED`）
- `CIRCUIT BREAKER: ...` — 日亏/回撤熔断触发，已平仓停开新仓
- `external close detected` — SL/TP 在交易所侧触发，已补记盈亏
- `KILL SWITCH triggered` — 紧急停机
- `LLM ... degrade HOLD` — LLM 失败降级，未带病下单

## 复盘决策（审计）

所有决策、拒单、订单、快照都落在 SQLite（`config.yaml` 的 `storage.db_path`，默认 `data/trade.db`）。

```bash
cd /opt/binance-trade
sqlite3 data/trade.db
```

常用查询：

```sql
-- 最近 20 条决策（含是否跳过 LLM 及原因）
SELECT created_at, symbol, skipped, action, confidence, leverage, reason
FROM decisions ORDER BY id DESC LIMIT 20;

-- 最近被风控拒单的记录（看拒单码与原因）
SELECT created_at, symbol, code, reason, leverage FROM rejects ORDER BY id DESC LIMIT 20;

-- 实际下单（含 dry-run）
SELECT created_at, symbol, client_kind, side, qty, price, notional, status, dry_run
FROM orders ORDER BY id DESC LIMIT 20;

-- 某次决策的完整输入上下文（JSON，用于还原 LLM 看到的市场）
SELECT context_json FROM decisions WHERE id = <决策id>;

-- 当日盈亏 / 回撤轨迹
SELECT created_at, total_equity, available_margin, day_realized_pnl, drawdown_pct
FROM balance_snapshots ORDER BY id DESC LIMIT 50;

-- 启动对账恢复的未完成挂单
SELECT created_at, symbol, order_type, side, qty, stop_price, reduce_only FROM open_orders
ORDER BY id DESC LIMIT 20;
```

> 已实现盈亏说明：`day_realized_pnl` 由平仓（显式 CLOSE 与 SL/TP 触发的外部平仓）累计，
> 用「入场价 vs 标记价」近似，**未计手续费与资金费**。精确对账请以币安 income 流水为准；
> 此值的用途是驱动日亏熔断，量级足够。

## 健康检查

```bash
# 进程在跑？
systemctl is-active binance-trade

# 最近有心跳/决策？（应每个周期都有新行）
sqlite3 /opt/binance-trade/data/trade.db \
  "SELECT created_at, symbol, skipped FROM decisions ORDER BY id DESC LIMIT 5;"

# 时钟没漂？
chronyc tracking | grep -E 'Leap|System time'
```

若决策表长时间无新行 → 检查日志是否有异常、行情拉取是否失败、API key 是否被限/过期。

## 常见故障

| 现象 | 排查方向 |
|------|----------|
| 启动即退出，报配置错误 | `python -c "from src.config.loader import load_config; load_config()"` 看具体字段 |
| `recvWindow` / 时间戳报错 | `chronyc tracking` 确认 NTP；重启 chronyd |
| 下单全被拒 `LEVERAGE_EXCEEDED` | LLM 给的杠杆 > `risk.max_leverage`，符合预期（不截断）；如需放宽改 config |
| 下单 `below minNotional` 被拒 | 本金/`size_pct` 太小，单笔名义价值不足交易所下限 |
| LLM 频繁 `degrade HOLD` | 检查 `ANTHROPIC_API_KEY`、网络出站、`llm.timeout` |
| 重启后持仓/挂单状态不对 | 确认 `storage.reconcile_on_start: true`；看 `reconciled ... on startup` 日志 |

## 变更配置后

`config.yaml` 改动需 `restart` 生效。改风控阈值前先在 testnet 或 dry_run 验证。
`.env` 密钥改动同样需 `restart`，并确认 `chmod 600 .env`。
