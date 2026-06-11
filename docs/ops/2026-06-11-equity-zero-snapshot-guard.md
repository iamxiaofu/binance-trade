# 2026-06-11 账户总览权益跌 0 兜底护栏

## 背景

2026-06-11 用户反馈 Web 账户总览（DASHBOARD）的「账户权益 (USDT)」曲线
偶尔出现跌到 0、随后又恢复的现象，但同期交易所侧数据正常。
前序分析见 `docs/ops/2026-06-11-equity-zero-vs-exchange.md`，根因为
`src/engine/loop.py::_record_balance_snapshot` 内的 `or 0.0` 兜底：
当 `fetch_balance()` 因 ccxt 限频 / 接口残缺 / 字段缺失等原因返回
`bal['total'][USDT]` 为 None / 缺失 / 0 时，仍会把 `total_equity=0` 写入
`balance_snapshots`，前端曲线瞬间砸出跌 0 尖刺。

本次按"最小修复"原则，仅对 `_record_balance_snapshot` 加无效值护栏：
无效值不写库、不覆盖 `runtime.current_equity`，保留上一周期权益。

## 现场现象

- 现场账户为 ISOLATED 模式，ccxt 4.5.56。
- 抓取失败 / 限频残缺响应时，`_record_balance_snapshot` 走 `or 0.0` 兜底
  把 `total_equity=0` 落进 `balance_snapshots`，前端读取最新一行
  (`web/status.py::latest_balance`) 直接展示 0。
- 与"交易所数据正常"对比强烈：交易所侧钱包余额、账户总权益、持仓均无变化。

## 根因

`_record_balance_snapshot` 三行核心逻辑：

```
total = (balance.get("total") or {}).get(self._settings.account.quote_asset) or 0.0
free  = (balance.get("free")  or {}).get(self._settings.account.quote_asset) or 0.0
total = float(total)
self.runtime.update_equity(total)
await self._store.snapshot_balance(total_equity=total, ...)
```

`or 0.0` 在以下场景全部命中并静默落 0（已用 4 个合成 case 验证）：

| `bal` 入参 | 解析结果 |
|---|---|
| `{"total": {"USDT": 5063.27}, "free": {"USDT": 5032.18}}` | 正常落库 |
| `{"total": {"BTC": 1.0}, "free": {}}` | `total=0.0`，**落 0** |
| `{"total": {"USDT": None}, "free": {"USDT": 0.0}}` | `total=0.0`，**落 0** |
| `{"total": {}, "free": {}}` | `total=0.0`，**落 0** |
| `{}` | `total=0.0`，**落 0** |

`fetch_balance()` 抛异常时外层 `try/except` 已兜底，不写库；只有"成功但残缺"
这条路径会把 0 静默落库。

## 代码改造

仅改 `src/engine/loop.py::_record_balance_snapshot`：

- 用 `try/except` 解析 `total_raw` / `free_raw`，过滤非数值类型；
- 判定 `total <= 0` 或 `free < 0` 时**直接 return**，不调用
  `runtime.update_equity`，不调用 `store.snapshot_balance`，并 `logger.warning`
  打印现场数据，保留上一周期的 `runtime.current_equity` 与 peak；
- 解析成功后行为不变，调用 `update_equity` 与 `snapshot_balance` 保持原签名。
- `quote_asset` 提前到局部变量，避免三处重复 `.get`。

不动的部分（按"最小修复"边界）：

- `src/exchange/client.py` 的 `fetch_balance()` 不动，ccxt 解析是上游问题，
  跨模块影响面较大，本期不在范围。
- `runtime.update_equity` 不动，它本身只是赋值与回撤计算，行为正确。
- 风控/上限 `equity_base`、LLM prompt 等下游消费方不动；它们都会从
  `runtime.current_equity` 读到"上一周期的有效值"，比读到 0 更稳。

## 验证

- 新增 4 个单测（`tests/test_engine.py`），覆盖：
  1. `bal['total']` 中缺 USDT 键 → 不写库、`current_equity` 不变。
  2. `bal['total']['USDT'] = None` → 不写库、`current_equity` 不变。
  3. `bal['total'] = {}` → 不写库、`current_equity` 不变。
  4. `bal['free']['USDT'] = -1.0`（脏数据）→ 不写库、`current_equity` 不变。
- 旧用例 `test_record_balance_snapshot_updates_runtime`（正常 321.0）继续通过。
- `.venv/bin/python -m pytest -q` 跑全套：**284 passed**（2 个无关 deprecation
  warning），无回归。

## 线上状态

- 修复未提交，按 `AGENTS.md` 约定等用户明确要求再 `git commit`。
- 部署：需重启 `binance-trade.service`，新代码生效后下一周期 `_snapshot` 即按
  新护栏写库。
- 监控建议：观察 `balance_snapshots` 中是否还会出现 `total_equity=0` 的行；
  若仍出现，说明上游残缺响应在更早的位置（如 `web/server.py` 兜底），需要
  进一步排查。

## 后续注意事项

- 本次只解决"写入侧"的兜底，没有在 `fetch_balance()` 上游做更精细的解析；
  如果后续 ccxt 升级或币安接口变更导致 `bal['total'][USDT]` 长期为 None，
  表现为 `runtime.current_equity` 一直停留在最后一次有效值，曲线变成水平
  线（不再下跌但也不再刷新），需要单独排查。
- `update_equity(0)` 现在已经不会发生；`equity_peak` 也只在拿到有效值时
  才推进，避免被 0 覆盖。
- 风控/上限 `equity_base` 仍以 `runtime.current_equity` 为准；若上游数据
  长期无效导致其停在旧值，账户风险敞口可能滞后反映真实情况，必要时再做
  "30 分钟内未刷新则告警"的次级护栏。
