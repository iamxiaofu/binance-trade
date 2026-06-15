# 运行手册（Runbook）

日常运维 binance-trade：启停、紧急熔断、看日志、复盘决策。
假设以 systemd 托管、工作目录 `/opt/binance-trade`、运行用户 `trader`。

---

## Binance 私有账户流

交易引擎使用 Binance USD-M User Data Stream 作为余额、持仓和订单状态的实时主通道，
REST 仅用于启动基线、周期审计、断线恢复和紧急确认。

- 控制页私有流状态必须为 `LIVE`，最近 REST 对账时间应持续更新。
- `DISCONNECTED` 或 `RESYNCING` 会立即阻止新开仓；已有持仓不会仅因断线被盲目平仓。
- 重连后先执行完整 REST 对账和保护单检查，再恢复由私有流故障造成的技术暂停。
- `MARGIN_CALL` 会暂停新开仓并告警，不自动平仓。
- 外部手工订单会禁用对应币种；外部持仓会被接管并补止损，补保护失败则紧急平仓。

排查时查看控制页或 `/api/stream-status`，确认 `status=LIVE`、`failed_events=0`，
并检查日志中的 `private user stream disconnected`、`listenKey keepalive failed`。
testnet 默认连接 `wss://fstream.binancefuture.com/ws`，mainnet 默认连接
`wss://fstream.binance.com/private/ws`，均可通过 `user_stream.private_ws_base_url` 覆盖。

---

## 启动 / 停止 / 重启

```bash
sudo systemctl start   binance-trade@testnet
sudo systemctl stop    binance-trade@testnet
sudo systemctl restart binance-trade@mainnet
sudo systemctl status  binance-trade@testnet binance-trade@mainnet
sudo systemctl status  binance-trade-web@testnet binance-trade-web@mainnet
```

`stop` 会向进程发 `SIGTERM`，main.py 捕获后仅停止引擎；撤单 + 平仓必须显式执行 kill-switch。
主网引擎每次启动强制暂停新开仓，必须在主网控制页人工恢复。

## 紧急熔断（Kill Switch）

有三种手段，按场景选择：

1. **代码级 kill（推荐，会撤单+平仓）**
   ```bash
   sudo -u trader /opt/binance-trade/.venv/bin/python /opt/binance-trade/main.py \
     -c /etc/binance-trade/config.testnet.yaml -e /etc/binance-trade/testnet.env kill-switch
   ```
   `kill-switch` 子命令：撤掉所有挂单 + 平掉所有持仓 + 推送告警。

2. **配置级急停（停开新仓，不自动平仓）**
   使用 Web Control 的“暂停策略”，或在库中设置 `strategy.paused=true` 后重启。

3. **交易所侧兜底**：登录币安手动平仓 + 在 API 管理页**临时禁用 key**。
   这是最后防线，确保即使进程失控也能切断下单能力。

## 看日志

```bash
# systemd 聚合日志（实时）
sudo journalctl -u binance-trade@testnet -f
sudo journalctl -u binance-trade@mainnet --since "1 hour ago"

# 文件日志（loguru 轮转，见 config.yaml logging.dir）
tail -f /var/log/binance-trade/testnet/binance-trade_$(date +%F).log
tail -f /var/log/binance-trade/mainnet/binance-trade_$(date +%F).log
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

所有决策、拒单、订单、快照都写入环境独立 SQLite：
`/var/lib/binance-trade/testnet/trade.db` 与 `/var/lib/binance-trade/mainnet/trade.db`。

```bash
sqlite3 /var/lib/binance-trade/testnet/trade.db
```

常用查询：

```sql
-- 最近 20 条决策（含是否跳过 LLM 及原因）
SELECT created_at, symbol, skipped, action, confidence, leverage, reason
FROM decisions ORDER BY id DESC LIMIT 20;

-- 最近被风控拒单的记录（看拒单码与原因）
SELECT created_at, symbol, code, reason, leverage FROM rejects ORDER BY id DESC LIMIT 20;

-- 实际下单
SELECT created_at, symbol, client_kind, side, qty, price, notional, status
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
systemctl is-active binance-trade@testnet binance-trade@mainnet

# 最近有心跳/决策？（应每个周期都有新行）
sqlite3 /var/lib/binance-trade/testnet/trade.db \
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
| LLM 频繁 `degrade HOLD` | 在 LLM Profile 页面检查当前激活源、API Key、网络出站和 timeout |
| 重启后持仓/挂单状态不对 | 确认 `storage.reconcile_on_start: true`；看 `reconciled ... on startup` 日志 |

## 变更配置后

`config.yaml` 改动需 `restart` 生效。改风控阈值前先在 testnet 验证。
`.env` 密钥改动同样需 `restart`，并确认 `chmod 600 .env`。
