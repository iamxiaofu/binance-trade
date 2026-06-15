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

## 4. 目录职责

正式部署使用不可混淆的目录边界：

| 路径 | 职责 | 所有者 |
|---|---|---|
| `/data0/binance-trade` | 唯一 Git 源码仓库、开发与测试 | root/维护者 |
| `/opt/binance-trade` | 当前发布副本，不保存运行数据或密钥 | root:root，`trader` 只读 |
| `/etc/binance-trade` | 环境配置与 Binance/Web 密钥 | root:trader，文件 `0640` |
| `/var/lib/binance-trade/{testnet,mainnet}` | 环境独立 SQLite | trader:trader，目录 `0700`、DB `0600` |
| `/var/log/binance-trade/{testnet,mainnet}` | 环境独立文件日志 | trader:trader，目录 `0700` |

不得直接在 `/opt/binance-trade` 修改代码；先在 `/data0/binance-trade` 完成测试，再同步发布。

## 5. 部署代码

```bash
sudo rsync -a --delete \
  --exclude .git --exclude .env --exclude '.env.*' --exclude .venv \
  --exclude data --exclude logs --exclude node_modules \
  /data0/binance-trade/ /opt/binance-trade/
sudo chown -R root:root /opt/binance-trade
```

Python 运行时安装在 `/opt/binance-trade/.venv`，允许 `trader` 执行，但由 root 管理。

## 6. 配置与密钥

```bash
sudo install -d -m 0750 -o root -g trader /etc/binance-trade
sudo install -d -m 0700 -o trader -g trader \
  /var/lib/binance-trade/{testnet,mainnet} \
  /var/log/binance-trade/{testnet,mainnet}
```

每个环境使用独立文件：

- `config.testnet.yaml` / `config.mainnet.yaml`
- `testnet.env` / `mainnet.env`：交易引擎 Binance Key
- `web-testnet.env` / `web-mainnet.env`：Web 只读 Binance Key 与 Basic Auth

配置必须使用绝对 DB 和日志路径。LLM API Key 通过对应环境看板动态管理，不写入 env。

## 7. systemd 双环境托管

```bash
sudo install -m 0644 deploy/binance-trade@.service deploy/binance-trade-web@.service \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  binance-trade@testnet binance-trade-web@testnet \
  binance-trade@mainnet binance-trade-web@mainnet
```

端口固定为 testnet `8000`、mainnet `8001`。主网引擎每次重启都会强制暂停新开仓，
完成私有流、REST 对账和人工检查后才能从主网控制页恢复。

## 8. 升级 / 回滚

```bash
sudo systemctl stop 'binance-trade@*' 'binance-trade-web@*'
# 在 /data0/binance-trade 完成测试后执行第 5 节 rsync
sudo systemctl start \
  binance-trade-web@testnet binance-trade-web@mainnet \
  binance-trade@testnet binance-trade@mainnet
```

回滚代码不会回滚数据库；恢复数据库前必须停对应环境的 engine 与 web，并使用独立备份。
