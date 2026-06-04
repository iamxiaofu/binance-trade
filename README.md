# biance-trade

LLM 驱动的币安 USDT 本位永续合约交易机器人。Claude 出结构化决策，**硬风控**夹断，
按周期节流调用，dry-run 默认开启。安全优先：杠杆超限直接拒单（不截断）、多道名义价值闸、
日亏熔断、kill-switch。

> ⚠️ 量化交易有资金损失风险。本项目默认 `dry_run: true` 与 `mode: testnet`。
> 切换到 mainnet 真实下单前，请充分回测并自行承担风险。

## 架构

```
config(pydantic 强校验) ─┐
                         ├─ engine/loop 主循环（5m 周期）
exchange(ccxt 异步) ─────┤     │
features(指标) ──────────┤     ├─ throttle.gate   是否调用 LLM（纯函数）
llm(Claude tool-use) ────┤     ├─ llm.decide      结构化决策，失败降级 HOLD
risk(硬风控) ────────────┤     ├─ risk.validate   逐项校验 + 夹断
execution(下单/退避) ────┤     ├─ execution       精度规整 + dry-run/真实下单 + SL/TP
state(运行态) ───────────┤     ├─ store           SQLite 落库（决策/拒单/订单/快照）
store(SQLite) ───────────┤     └─ notify          Telegram 告警（可开关）
notify(Telegram) ────────┘
```

下单前流水线：`节流 → 特征 → LLM → 风控 → 执行 → 落库 → 告警`。
全局熔断（日亏/回撤）在每周期最前检查，触发即平仓并停开新仓。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

需要 Python 3.11+。

## 配置

1. 复制示例并按需修改：
   ```bash
   cp config.yaml.example config.yaml
   cp .env.example .env
   ```
2. `.env` 填入密钥（**绝不提交到 git**，已在 .gitignore）：
   - `BINANCE_API_KEY` / `BINANCE_API_SECRET`
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（启用告警时）
3. `config.yaml` 关键开关：
   - `mode`: `testnet`（默认）/ `mainnet`
   - `execution.dry_run`: `true`（默认，只模拟不下单）
   - `risk.*`: 杠杆上限、单笔/单标的/全账户保证金上限、单笔止损亏损上限、日亏熔断、回撤上限、最小置信度

所有字段在启动时由 pydantic 强校验，非法配置 fail-fast 报错退出。

## 运行

```bash
# 主循环（按 config.yaml）
python main.py run

# mainnet + 真实下单会二次确认；脚本化部署用 --yes 跳过
python main.py run --yes

# 紧急停机：撤所有挂单 + 平所有持仓
python main.py kill-switch

# 回测/重放：历史 K 线 + 同一套 throttle/risk，观察风控夹断
python main.py backtest --symbol BTCUSDT --csv data/btc_5m.csv --leverage 10
# CSV 格式：ts,open,high,low,close,volume（首行表头可选）
```

## 部署（systemd）

```bash
sudo cp deploy/biance-trade.service /etc/systemd/system/
# 按实际路径修改 WorkingDirectory / ExecStart / User
sudo mkdir -p /var/log/biance-trade && sudo chown trader:trader /var/log/biance-trade
sudo systemctl daemon-reload
sudo systemctl enable --now biance-trade
sudo journalctl -u biance-trade -f      # 或看 /var/log/biance-trade/*.log
```

收到 `SIGTERM`（`systemctl stop`）时 main.py 捕获信号，先撤单+平仓再退出。

完整步骤（NTP、防火墙、testnet→mainnet 切换）见 **[docs/DEPLOY.md](docs/DEPLOY.md)**；
日常运维（启停、紧急熔断、看日志、复盘决策、故障排查）见 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**。

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：config 校验、精度过滤、节流纯函数、风控夹断、LLM 结构化输出与降级、
运行态、SQLite 落库与对账、Telegram 告警开关、执行层 dry-run/拒单/SL-TP、
主循环熔断与开仓流水线、回测重放。

## 安全说明

- 密钥只从 `.env` 读取，`Credentials.__repr__` 脱敏，绝不写日志、绝不落库。
- `on_leverage_exceed` 用 enum 锁死为 `REJECT`：超限拒单而非静默截断到上限。
- dry-run 路径完全不触碰交易所；真实下单前才设置杠杆/保证金模式。
- 风控为硬约束，位于 LLM 之后、执行之前；LLM 任何失败都降级为 HOLD。

## 数据与持久化

SQLite（`storage.db_path`，默认 `./data/trade.db`）记录：
- `decisions` — 每周期每标的，含是否跳过 LLM 及原因
- `rejects` — 风控/精度拒单（含拒单码）
- `orders` — 下单结果（含 dry-run）
- `position_snapshots` / `balance_snapshots` — 周期快照

重启时若 `storage.reconcile_on_start: true`，从交易所拉取持仓对账并回填运行态。
