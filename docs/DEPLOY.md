# 部署文档（东京服务器 / Rocky Linux 9.x）

面向在日本东京 Linux 服务器（Rocky Linux 9.4）上部署 binance-trade。
全程以**非 root 专用用户**运行，先 testnet 跑通再切 mainnet。

> 安全前提：API Key 仅开「读取 + 合约交易」，**关闭提现权限**。最小权限原则。

---

## 1. 系统准备

```bash
# 以 root 或 sudo 执行
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-devel git chrony firewalld

# 专用运行用户（无登录 shell 更安全，这里保留 shell 便于排障）
sudo useradd -m -s /bin/bash trader
sudo mkdir -p /opt/binance-trade /var/log/binance-trade
sudo chown -R trader:trader /opt/binance-trade /var/log/binance-trade
```

## 2. 时间同步（NTP，关键）

币安下单对时间敏感（`recvWindow` 默认 5000ms）。服务器时钟漂移会导致下单被拒。

```bash
sudo systemctl enable --now chronyd
# 东京可用 NTP 源（可选，默认池也可）
echo "server ntp.nict.jp iburst" | sudo tee -a /etc/chrony.conf
sudo systemctl restart chronyd
chronyc tracking        # 确认 Leap status: Normal，System time 偏差 < 50ms
chronyc sources -v
```

代码侧 ccxt 已开 `adjustForTimeDifference: True`，会用服务器时间二次校准；NTP 是第一道防线。

## 3. 防火墙

机器人只发**出站** HTTPS（币安 / Anthropic），不需要入站端口。

```bash
sudo systemctl enable --now firewalld
# 默认 public zone 已拒绝入站；仅在需要 SSH 时放行
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

> 出站默认放行，无需额外规则。若启用了出站过滤，需放行 `api.binance.com` /
> `testnet.binancefuture.com` / `api.anthropic.com` 的 443。
> 将来上线 web 可视化（监听端口）时，再按需放行对应端口并加反向代理 + 鉴权。

## 4. 部署代码

```bash
sudo -iu trader
cd /opt/binance-trade
git clone <your-repo-url> .        # 或 rsync 上传

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 5. 配置与密钥

```bash
cp config.yaml.example config.yaml
cp .env.example .env
chmod 600 .env                      # 仅 trader 可读，防泄露

# 编辑 .env，填入 testnet 的 key（注意 testnet 与 mainnet key 不同）
vi .env
```

`.env` 必填：`BINANCE_API_KEY` / `BINANCE_API_SECRET` / `ANTHROPIC_API_KEY`，
启用告警再加 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`。

`config.yaml` 首次保持默认：`mode: testnet`、`execution.dry_run: true`。

## 6. testnet 冒烟验证

```bash
source .venv/bin/activate
# 配置自检（非法配置会 fail-fast 报错）
python -c "from src.config.loader import load_config; load_config(); print('config OK')"

# 跑一个回测，确认风控夹断链路正常
python main.py backtest --symbol BTCUSDT --csv <历史K线.csv> --leverage 10
#   预期：LEVERAGE_EXCEEDED 计数 == triggered（杠杆10 > max_leverage 3 全被拒）

# 前台试跑主循环，观察日志（Ctrl-C 退出会优雅平仓）
python main.py run
```

确认日志出现 `markets loaded`、`engine started`、按周期的 `[skip-llm]` 或决策记录。

## 7. systemd 托管

```bash
exit                                # 回到 sudo 用户
sudo cp /opt/binance-trade/deploy/binance-trade.service /etc/systemd/system/
# 按实际路径核对 WorkingDirectory / ExecStart / User
sudo systemctl daemon-reload
sudo systemctl enable --now binance-trade
sudo systemctl status binance-trade
sudo journalctl -u binance-trade -f
```

`SIGTERM`（`systemctl stop`）时 main.py 捕获信号 → 撤单 + 平仓 → 退出。

## 8. testnet → mainnet 切换

确认在 testnet 稳定运行、风控/告警/对账都符合预期后：

1. **停服务**：`sudo systemctl stop binance-trade`
2. **换 key**：编辑 `.env`，替换为 mainnet 的 `BINANCE_API_KEY/SECRET`
   （再次确认该 key 已**关闭提现权限**）。
3. **改配置**：`config.yaml` 设 `mode: mainnet`。
   - 建议先保持 `execution.dry_run: true` 跑 1～2 天，验证 mainnet 行情/对账无误。
   - 风控阈值按真实账户权益重核：`max_order_margin_pct` /
     `max_symbol_margin_pct` / `max_total_margin_pct` /
     `max_loss_per_trade_pct` / `daily_max_loss_pct` / `max_drawdown_pct`。
   - 保证金字段是权益倍率小数：`0.2` 表示权益 × 20%，`0.8` 表示权益 × 80%。
     `max_loss_per_trade_pct`、`daily_max_loss_pct` 与 `max_drawdown_pct`
     是百分数：`2` 表示 2%。
4. **开真实下单**：确认无误后设 `execution.dry_run: false`。
   - systemd 的 `ExecStart` 带 `--yes` 跳过交互确认；**务必在此步前确认所有阈值**。
   - 手动前台启动时不带 `--yes` 会要求输入 `yes` 二次确认。
5. **启动 + 紧盯**：`sudo systemctl start binance-trade` 并持续 `journalctl -f` 观察首轮真实周期。

## 9. 升级 / 回滚

```bash
sudo systemctl stop binance-trade
sudo -iu trader
cd /opt/binance-trade && git pull && source .venv/bin/activate && pip install -r requirements.txt
exit
sudo systemctl start binance-trade
```

回滚：`git checkout <上一个稳定 tag>` 后重复上面步骤。SQLite 数据库（`data/trade.db`）向后兼容，新表自动建。
