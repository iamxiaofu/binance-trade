# 2026-06-11 账户总览权益跌 0 与交易所数据不一致分析

## 背景

Web 账户总览（DASHBOARD）的「账户权益 (USDT)」曲线偶尔出现跌到 0、随后又恢复的现象，
但同一时间交易所 APP / 网页 / 官方 API 的账户总权益显示正常。
本次仅做根因分析与修复方案登记，暂不改代码。

## 现场现象

- `web/frontend/src/views/Dashboard.vue:90` 展示的 `bal.total_equity` 突然显示为 `0.00`，
  几秒到几分钟后自动恢复，期间交易所侧无任何异常、仓位/余额均正常。
- 期间 `web/server.py:478 /api/equity` 返回的 `balance_snapshots` 中确实出现
  `total_equity=0` 的快照。
- `position` 行的 `unrealized_pnl` 与交易所对得上，
  说明交易所侧持仓与未实现盈亏都正常，问题只发生在"总权益"这一列。

## 根因

### 1. 真正的"账户总权益"被定义错位

USDT-M 永续合约在 Binance 的口径里：

- `walletBalance`：钱包余额（不含未实现盈亏）
- `unrealizedProfit`：未实现盈亏
- **`totalMarginBalance` = walletBalance + unrealizedProfit（账户总权益）**

`ccxt` 异步版 `binanceusdm.fetch_balance()` 返回里：

- `bal['total']['USDT']` ⇔ `totalWalletBalance`（**钱包余额**）
- `bal['free']['USDT']`  ⇔ `availableBalance`（可用保证金）
- 真正的"账户总权益"在 `bal['info']['totalMarginBalance']`（或 `info.assets[].marginBalance` 求和）

项目内**所有**权益读取路径都把 `bal['total']['USDT']` 当作"账户权益"：

- `src/engine/loop.py:2800-2803`（`_record_balance_snapshot`）
  ```
  total = (balance.get("total") or {}).get(self._settings.account.quote_asset) or 0.0
  await self._store.snapshot_balance(total_equity=total, ...)
  ```
- `src/engine/loop.py:1742-1752`（`_current_equity`），同样读 `balance.get("total")[USDT]`
- `src/features/builder.py:90` 进一步把这个值当作 `equity_base` 喂给 LLM 和风控上限

全仓搜索 `totalMarginBalance / totalUnrealizedProfit / totalCrossWalletBalance` 命中数：0。
也就是说**项目从来没读过 `info.totalMarginBalance`**。

这导致两类表象：

1. **有未实现浮盈/浮亏时**，`total_equity` 永远比真实权益少一截，曲线看上去像"少了一坨钱"。
2. **钱包余额短暂为 0 时**（例如刚把 USDT 从合约钱包转出做转仓 / 划转到现货 / 内部划转后尚未到账 / 账户处于
   "逐仓全平、待结算" 的瞬间窗口），`bal['total']['USDT']` 就是 0，ccxt 不会做容错，直接落库为 0，
   前端曲线就出现"跌 0"。
3. **极端情况**：`fetch_balance` 命中限频 / 临时错误时 ccxt 抛异常，外层有 try/except 兜底成 0 落库
   （见 `src/engine/loop.py:2800` 起 `try ... except Exception as e: logger.warning(...)`，
   失败时 `total` 走默认 0 路径写入）。

### 2. "交易所数据正常" 跟项目口径对不上

- 交易所 APP 的"账户总权益" = `totalMarginBalance`；
- 我们的"账户权益" = `totalWalletBalance`。

两者在以下任一情况下会不一致：

- 当前有未实现盈亏（最常见）；
- 钱包发生转入/转出；
- 账户存在历史未结算资金费 / 利息；
- ccxt 解析 `info.assets[]` 与顶层 `totalWalletBalance` 字段偶发不一致（不同 Binance 接口版本）。

因此用户看到的"交易所数据正常、项目跌 0"是必然结果，**不是行情/网络问题**。

## 修复方案

### 推荐：直接读 `info.totalMarginBalance` 作为 `total_equity`

在 `ExchangeClient.fetch_balance()` 增加解析层，把 USDT-M 真实"账户总权益"补到结果里，
向下兼容现有的 `total` / `free` 字段。建议在 `src/exchange/client.py` 新增一个方法：

```
async def fetch_account_equity(self, quote: str = "USDT") -> dict:
    bal = await self._exchange.fetch_balance()
    info = bal.get("info") or {}
    # 优先级：info.totalMarginBalance > info.totalCrossWalletBalance > bal['total'][USDT]
    total_margin = float(info.get("totalMarginBalance") or 0.0)
    cross_wallet  = float(info.get("totalCrossWalletBalance") or 0.0)
    unrealized    = float(info.get("totalUnrealizedProfit") or 0.0)
    wallet        = float((bal.get("total") or {}).get(quote) or 0.0)
    total_equity = total_margin or (cross_wallet + unrealized) or wallet
    return {
        "total_equity": total_equity,
        "wallet_balance": wallet,
        "unrealized_pnl": unrealized,
        "available_margin": float((bal.get("free") or {}).get(quote) or 0.0),
        "raw": bal,
    }
```

然后把三处调用点改用它：

- `src/engine/loop.py:2800` `_record_balance_snapshot`
- `src/engine/loop.py:1742` `_current_equity`
- `src/features/builder.py:90` 构造 `MarketContext` 时的 `equity` 来源

### 兜底：异常不再静默写 0

- 启动抓权益（`loop.py:110`）和周期快照（`loop.py:2790-2796`）失败时，**不要**用 0 覆盖
  `runtime.current_equity`；保留上一次成功值并打 `warning`，等下一周期再重试。
- `snapshot_balance` 写入侧增加 `total_equity < 0` 校验，小于 0 直接拒绝落库。

### 可选：补一个对账接口便于排查

在 `/api/equity` 之外新增 `/api/equity_raw`，把 `{wallet_balance, total_equity, unrealized_pnl,
source: info|fallback}` 一起返回，前端在「账户总览」卡片上加一个 tooltip 展示三者分解，
便于以后再出现"看着不对"时直接看到差值来源。

## 影响范围

- 风控上限（`max_order_margin_abs` / `max_loss_per_trade_abs` / `daily_max_loss`）当前基于
  `equity_base = wallet if wallet > 0 else available_margin`，存在被低估的可能
  （浮盈时放大下单空间、浮亏时过度收紧）。改成 `total_equity` 后会更稳。
- 仪表盘曲线更接近交易所侧显示；同时已实现盈亏/未实现盈亏/钱包余额可以分解展示，更直观。

## 后续注意事项

- 改完不要立即清空 `balance_snapshots`；曲线会基于历史值继续绘制，必要时只清异常的那几行。
- LLM prompt 里的 `账户权益` 数值会同步变化（更接近真实），但语义不变，决策契约不受影响。
- 主网与 testnet 字段名一致，无需分别处理。
- 监控建议：对 `runtime.current_equity` 与 `info.totalMarginBalance` 取一次差值，
  若两者长期偏离 > 1 USDT 需告警，可能是 ccxt 解析或接口字段变更。


## 附：为什么"逐仓全平"才跌 0、之前为什么没遇到

回答"为什么之前没见过"的关键是把币安 USDT-M **ISOLATED（逐仓）账户的钱包结构**讲清楚。

### 1. 币安 USDT-M 账户的 USDT 到底存在哪

`/fapi/v2/account` 返回 `assets[]`，**逐仓模式下**：

- 账户中 USDT 总量 = 逐仓各 symbol 仓位占用的 `isolatedWallet` 之和 + 逐仓账户里的"剩余 USDT"。
- 全仓（CROSS）下，USDT 主要落在顶层 `totalCrossWalletBalance`，逐仓占用走 `assets[].isolatedWallet`。

具体到 ccxt：

- **CROSS**：顶层 `info.totalCrossWalletBalance` 是大头，`bal['total']['USDT']` 通常能稳定反映。
- **ISOLATED**：ccxt `parseBalance` 把每个 `assets[i]` 投到对应 symbol 上，顶层 USDT 行
  `walletBalance` 经常是 **0** 或接近 0（钱都按"被某个 symbol 锁在 isolatedWallet"算），
  真正反映"账户总权益"的是 `info.totalMarginBalance`（= 所有 isolatedWallet 汇总 + 未实现盈亏）。

也就是说：

> **ISOLATED 模式下，`bal['total']['USDT']` 长期近似 0 是正常现象**，并不是"刚刚跌成 0"，
> 而是这字段本身就不是逐仓账户的正确读法。

### 2. 什么情况下之前"看起来没事"

把时间线对一下：

1. 早期或仓位持续存在：每个 symbol 的 `isolatedWallet` 是被 ccxt 解析到该 symbol 行里的，
   USDT 这一行长期保持 0，但前端那张"账户权益"曲线由于历史值就是 0 / 接近 0，**看上去是平的，
   没人在意**。
2. 仓位从未清空过：USDT 行一直是 0，但项目**没有**发生过"已实现盈亏入账到 USDT 钱包"
   的事件，所以 `snapshot_balance` 写入的 `total_equity` 一直是 0，**前端反而把"0"当成基线**。
3. 一旦发生"逐仓全平"：
   - 仓位消失 ⇒ 该 symbol 的 `isolatedWallet` 释放回顶层 USDT。
   - `fetch_balance` 在这一刻拿到 USDT 行的 `walletBalance` 暂时为 0（释放还在结算、
     或 ccxt 解析的瞬间对不上）。
   - `_record_balance_snapshot` 立刻把 `total_equity=0` 落库。
   - 下一周期结算完成后 USDT 行恢复，`bal['total']['USDT']` 又回到正确的几十 ~ 几百 USDT。
   - **曲线形态 = 从正常值突然跌到 0，再恢复到正常值**，正好对应"逐仓全平那一刻"。

### 3. 为什么之前没遇到

- **账户模式切到 ISOLATED 之前**（如果早期是 CROSS），`bal['total']['USDT']` 一直是有效值，
  跌 0 不会出现。这是最常见的"换模式后才复现"路径。
- **没有发生过"逐仓全平 + 下一周期立即取余额"的时序组合**：如果全平后隔很久才走 `_snapshot`，
  结算窗口已过，`bal['total']['USDT']` 已经是正确值，就不会落 0。
- **ccxt 版本差异**：旧版 ccxt 在 ISOLATED 模式下会尝试把 `isolatedWallet` 累加回顶层 USDT，
  所以读出来不是 0；升级到新版后解析口径变了，顶层 USDT 变成 0，bug 才暴露。
- **早期本金极小 / 全程有浮盈**：walletBalance 经常为 0 但 `totalMarginBalance` 不为 0，
  正好被"看起来正常"掩盖。直到某次真正结算（已实现盈亏进钱包）才把差异显化。

### 4. 复现路径（最常见的"现在才出现"的剧本）

1. 策略在 ISOLATED 模式下跑了若干周期，`balance_snapshots` 中 `total_equity` 一直 ≈ 0，
   曲线被误读为"基线"。
2. 某次减仓 / 强平 / SL/TP 命中，仓位归零，触发 `_snapshot_unlocked`。
3. 紧接着 `fetch_balance` 拿到 USDT 行 `walletBalance=0`（释放中）。
4. `_record_balance_snapshot` 写 `total_equity=0`，前端曲线瞬间砸出"跌 0"尖刺。
5. 几秒到几分钟后下一次 snapshot 拿到正确值，曲线回升。

**所以"逐仓全平才跌 0"不是巧合，是 ISOLATED + 用 `bal['total']['USDT']` 作权益这两个事实
叠加后的必然表象。** 之前没遇到，是因为之前要么是 CROSS，要么这个时序窗口没被采样到，
要么是 ccxt 版本帮你"无意识地"补齐了 isolatedWallet。

### 5. 验证方法（无需改代码即可核对）

1. 直接调币安官方接口 `/fapi/v2/account`（带签名），观察同一时刻的：
   - `totalCrossWalletBalance`
   - `assets[].isolatedWallet` 之和
   - `totalMarginBalance`
   - `totalUnrealizedProfit`
2. 对照 ccxt `fetch_balance()['total']['USDT']` 是不是等于 `totalCrossWalletBalance`。
3. 切换到 ISOLATED 后，这个等式大概率**不成立**，从而把根因坐实。

