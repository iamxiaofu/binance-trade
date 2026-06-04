# Web 看板搭建存档（命令 / 环境 / 参数）

本文件记录 web 可视化前端的完整环境、安装命令、端口、凭据位置与运维步骤，
便于在新机器复现或排障。**已搭建完成并验证通过**（本地 testnet）。

---

## 1. 环境与版本（已装）

| 组件 | 版本 | 安装方式 |
|------|------|----------|
| OS | Rocky Linux 9.4 | — |
| Node.js | 20.20.2 | NodeSource RPM 仓库 |
| npm | 10.8.2 | 随 Node |
| nginx | 1.20.1 | `dnf install nginx` |
| Python | 3.11.15 | 现有 uv venv (`.venv`) |
| 后端 | FastAPI 0.136 / uvicorn 0.48 / websockets 16 | `uv pip`（venv） |
| 前端框架 | Vue 3.5 + Vite + Pinia 3 + vue-router 4 | npm |
| UI | Element Plus 2.14 + @element-plus/icons-vue | npm |
| K线图 | klinecharts 9.8（内置 EMA/BOLL/MACD/RSI） | npm |
| 通用图表 | echarts 6 | npm |

## 2. 安装命令（按序）

```bash
# --- Node.js 20（Rocky 自带的 16 太旧，Vite 需 18+）---
curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
sudo dnf install -y nodejs nginx

# --- 后端 Python 依赖（写入 .venv；本仓库用 uv 管理）---
cd /opt/biance-trade        # 部署路径，开发机为 /data0/biance-trade
VIRTUAL_ENV=$PWD/.venv uv pip install "fastapi>=0.115" "uvicorn[standard]>=0.32" "python-multipart>=0.0.12"
# 或用 pip：.venv/bin/python -m pip install ...
# 也可：uv pip install -e ".[web]"   （pyproject 已声明 web 可选依赖组）

# --- 前端依赖 ---
cd web/frontend
npm install                  # 安装 package.json 所有依赖
npm run build                # 产出 web/frontend/dist/，由 FastAPI 托管
```

## 3. 端口与网络

| 用途 | 地址 | 说明 |
|------|------|------|
| 后端 uvicorn | `127.0.0.1:8000` | 仅本机，经 nginx 反代 |
| 前端开发热更新 | `127.0.0.1:5173` | 仅 `npm run dev` 时；代理 /api 与 /ws 到 8000 |
| nginx 对外 | 80 → 443 | 反代到 8000；WS 走 /ws 透传 Upgrade |

防火墙：放行 443（和 80 用于跳转），后端 8000 **不对外**。
```bash
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --reload
```

## 4. 凭据位置（Basic Auth）

- 全部在 **`/opt/biance-trade/.env`**（权限 600，已 gitignore）：
  - `WEB_USER`（默认 `admin`）
  - `WEB_PASSWORD`（已用 `secrets.token_urlsafe(18)` 生成强随机值）
  - `WEB_HOST=127.0.0.1` / `WEB_PORT=8000`
  - 可选 `WEB_PUSH_INTERVAL`（WS 推送间隔秒，默认 5）
- **安全约束**：`WEB_PASSWORD` 未设置时，后端拒绝一切 API 访问（503），不会裸奔。
- 查看当前密码：`grep WEB_ /opt/biance-trade/.env`
- 更换密码：编辑 .env 后 `systemctl restart biance-trade-web`。

## 5. 部署（systemd + nginx）

```bash
# 后端服务
sudo cp deploy/biance-trade-web.service /etc/systemd/system/
sudo mkdir -p /var/log/biance-trade && sudo chown trader:trader /var/log/biance-trade
sudo systemctl daemon-reload
sudo systemctl enable --now biance-trade-web
sudo systemctl status biance-trade-web

# nginx 反代（改 server_name 和证书路径后）
sudo cp deploy/nginx-biance-trade.conf /etc/nginx/conf.d/biance-trade.conf
sudo nginx -t && sudo systemctl enable --now nginx && sudo systemctl reload nginx
```

TLS 证书（Let's Encrypt）：
```bash
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example.com
```

## 6. 开发模式（本地改前端）

```bash
# 终端1：后端
cd /opt/biance-trade && PYTHONPATH=$PWD .venv/bin/python -m web.server
# 终端2：前端热更新（自动代理 /api、/ws 到 8000）
cd web/frontend && npm run dev    # 打开 http://127.0.0.1:5173
# 改完构建：
npm run build                     # 刷新 dist/，生产由 8000 直接托管
```

## 7. 功能与接口对照

页面（7 个，`web/frontend/src/views/`）：
- Dashboard 总览（权益/保证金/日盈亏/回撤 + 权益曲线 + 运行状态）
- Chart K线（KLineCharts，EMA/BOLL 主图叠加 + MACD/RSI 副图 + 开仓价标注）
- Positions 持仓
- Decisions 决策日志（含跳过记录；详情弹窗展示喂给 LLM 的 context_json）
- Orders 交易记录 + 风控拒单（双 tab）
- Pnl 盈亏统计
- Control 操作面板（Kill Switch / 暂停 / dry_run 切换 + 命令历史）

REST/WS 接口（`web/server.py`）：
- 只读：`/api/summary` `/api/positions` `/api/decisions[/{id}]` `/api/orders`
  `/api/rejects` `/api/pnl` `/api/equity` `/api/commands` `/api/config` `/api/klines/{symbol}`
- 操作：`POST /api/command/{KILL_SWITCH|PAUSE|RESUME|SET_DRY_RUN}`（写命令队列，交易进程消费）
- 实时：`WS /ws`（聚合状态）与 `WS /ws/market`（ticker + 最新K线，支持 mainnet/testnet）
- 探活：`GET /healthz`（无需鉴权）

## 8. 操作命令如何生效（关键架构）

Web **不直接操作交易所**。操作面板的命令写入 SQLite 的 `control_commands` 表，
交易主进程（engine/loop）**每周期开头**轮询并执行（`_process_commands`），
执行后回写 done/failed。延迟上限 = 一个决策周期。Kill Switch 仍保留命令行兜底：
`python main.py kill-switch`。

## 9. 排障

| 现象 | 排查 |
|------|------|
| API 全 401 | 浏览器没弹 Basic Auth 框？确认 .env 有 WEB_PASSWORD;curl -u 测试 |
| API 全 503 | WEB_PASSWORD 未设置;在 .env 配置后 restart |
| 页面空白 | `npm run build` 是否执行;`web/frontend/dist/` 是否存在 |
| WS 不连 | nginx /ws 是否透传 Upgrade;看浏览器控制台;后端日志 |
| K线 502 | 交易所行情拉取失败;看后端日志的 exchange error;检查网络/key |
| 持仓/盈亏为空 | 交易进程没跑过或没快照;web 会优雅降级为空，非报错 |

## 10. 已验证（本地 testnet）

- ✅ Node20/nginx/后端依赖安装成功
- ✅ 前端 `npm run build` 通过，dist 产物 2.8M（按页面代码分割）
- ✅ 后端 Basic Auth：无凭据/错密码 401，正确 200
- ✅ `/api/klines` 拉 testnet/mainnet 历史行情 + 指标
- ✅ WebSocket 聚合状态与实时行情推送正常，无凭据被拒
- ✅ 操作命令正确入队，交易进程消费执行（单测覆盖）
- ✅ 经 nginx 反代：SPA / API / WS 全链路通
- ✅ Python 测试套件 115 passed
