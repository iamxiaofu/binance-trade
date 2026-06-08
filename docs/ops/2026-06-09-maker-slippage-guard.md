# 2026-06-09 Maker 调参与市价单滑点护栏

## 背景

2026-06-09 01:04:46 SOLUSDT OPEN_SHORT 出现"挂单 → 立即撤单"现象：

1. LLM 决策 OPEN_SHORT 0.62，qty=75.33 SOL，ref_price=66.61
2. executor 调 `maker_quote`：best_ask=66.67，offset=1bps → limit_price=66.68
3. 挂 SELL limit @66.68 GTX (post-only)
4. **1.336 秒后**交易所返回 status=CANCELED（Binance 因 Post-Only 规则自动撤单）
5. 5 次 attempt 全失败（3 次 requote + 8s timeout）→ `maker_unfilled_action: CANCEL` → 放弃本次开仓

根因：`maker_price_offset_bps=1` 太小，1-2s 内 ask 跌 1-2bps 时挂单价立刻变成 taker，Post-Only 触发撤单。

## 设计目标

- 加大 maker 挂单离盘口偏移，给急跌行情留缓冲
- 延长单次等待 + 增加重试次数
- **市价单滑点护栏**：即使切到 FALLBACK_MARKET，预检盘口估算冲击价避免拿到不合理价格
- 仍保持保守：`maker_unfilled_action: CANCEL`（不开兜底市价单）—— 护栏先观察一段时间再开

## 配置变更（`config.yaml`）

| 参数 | 旧 | 新 | 含义 |
|---|---|---|---|
| `maker_timeout_seconds` | 8 | 15 | 单次 maker 挂单等待上限 |
| `maker_max_requotes` | 2 | 4 | 最大重挂次数（含首次 = 5 次） |
| `maker_price_offset_bps` | 1 | 5 | 挂单离盘口偏移（5 bps = 0.05%） |
| `maker_unfilled_action` | CANCEL | CANCEL | 暂不开 FALLBACK_MARKET（保守） |
| `market_slippage_bps` | — | 8 | **新增**：市价单最大允许滑点（默认） |
| `market_slippage_bps_per_symbol` | — | `{SOLUSDT: 10}` | **新增**：按币种 override |

## 新增代码

### `src/config/schema.py` (ExecutionConfig)

```python
market_slippage_bps: float = Field(default=8.0, gt=0, le=100)
market_slippage_bps_per_symbol: dict[str, float] = Field(default_factory=dict)
```

### `src/execution/executor.py` 新增

1. `_slippage_limit_bps(symbol)`：先 per_symbol 覆盖，再 default
2. `_preflight_market_slippage(symbol, side, ref_price, qty)`：
   - 拉盘口前 20 档
   - 按 side 累计 qty 得到加权均价 `est_impact`
   - 卖单看 bids（吃买盘），买单看 asks
   - 偏差 `(ref - impact) / ref * 1e4` bps
   - 超阈值返回 `(False, est_impact, "slippage Xbps > limit Ybps ...")`
3. 在 `_open_market_position` 入口（`normalize_order` 之后）插入预检
4. 在 `_close_market_position` 入口（取 mark 后）插入预检
5. 超阈值返回 `status=rejected, reason=slippage_exceeded`（不实际下市价单）

### 边界处理

- **盘口拉取失败** → 保守放行（市价单至少会部分成交）
- **盘口深度不够**（剩余 > 0）→ 仍放行 + warning（避免完全卡死）
- **拒单后行为**：LLM 下次触发（5min 或特征变化）会重新决策

## 阈值设计依据

| 币种 | 正常市价滑点 | 急跌急涨 | 推荐阈值 |
|---|---|---|---|
| BTCUSDT | 1-3 bps | 5-15 bps | 5-8 bps |
| ETHUSDT | 1-3 bps | 5-20 bps | 5-8 bps |
| BNBUSDT | 1-3 bps | 5-15 bps | 5-8 bps |
| **SOLUSDT** | 2-5 bps | 10-30 bps | **10 bps**（per_symbol） |

我们仓位：`max_order_margin_pct=0.2 × 5017 = 1003 USDT × 5x ≈ 5000 USDT 名义`。这个量级下，主流币 orderbook top 20 档深度远超冲击，**正常滑点只来自手续费 + spread**。

## 改动文件

| 文件 | 改动 |
|---|---|
| `src/config/schema.py` | ExecutionConfig 新增 2 字段 |
| `src/execution/executor.py` | 新增 2 个方法 + 2 处预检插入 |
| `config.yaml` | 改 4 行 + 新增 3 行 |
| `tests/test_market_slippage_guard.py` | 新增 6 个测试覆盖各场景 |

## 测试结果

- 6 个新增测试通过（within limit / exceed limit / per_symbol override / buy side / fetch fail / depth insufficient）
- 78 个相关测试全过（executor / engine / config_schema）

## 监控建议

跑 1-2 小时后看：
- `decision.reason` 字段中是否出现 "slippage_exceeded"
- `orders.status='rejected'` 的分布
- 若 BTC/ETH 0 拒单，SOL 偶尔拒 → 阈值合理
- 若 SOL 拒单 > 10% → 阈值过严，提到 12-15 bps
- 若无任何拒单 → 阈值过宽，降到 5 bps
