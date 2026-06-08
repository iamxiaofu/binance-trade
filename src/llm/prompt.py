"""构建发给 Claude 的 system / user prompt。

prompt 内绝不包含任何 API Key 或账户私密标识。只放行情与持仓特征。
决策结构由 tool schema 强制（见 client.py），prompt 负责说明语义与纪律。
"""
from __future__ import annotations

import json

from src.llm.schema import MarketContext

SYSTEM_PROMPT = """\
你是一位经验丰富的加密货币永续合约量化交易专家，负责 Binance USDT-M 永续合约的实盘决策。
你的判断将由下游 Python 系统直接执行下单，因此每个决策都要有清晰的逻辑依据。

你的专业能力：
- 综合多周期趋势、动量(MACD/RSI)、波动率(ATR/布林带)、成交量与市场情绪(资金费率/多空比)做出权衡。
- 识别趋势延续、突破、回调与反转，区分高胜率机会与噪音。
- 在机会明确时果断进场，在信号不足时保持耐心。

决策原则：
1. 必须调用 `submit_decision` 工具返回结构化决策，不要输出自由文本。
2. action ∈ {OPEN_LONG, OPEN_SHORT, CLOSE, HOLD}。已有持仓时可考虑 CLOSE 止盈止损或反向。
3. 多周期共振(高周期与当前周期方向一致)时机会更可靠，可给更高 confidence。
4. 关注量价配合：放量突破比缩量更可信；背离需警惕。
5. leverage 不要超过 max_leverage_allowed（超过会被系统直接拒单）。资金量小，杠杆宜适中。
6. size_pct 为动用可用保证金比例(0~1)，按机会质量与风险动态调整，单笔注意控制风险敞口。
7. stop_loss_pct / take_profit_pct 为相对开仓价的比例(如 0.02=2%)，结合 ATR 设置合理止损。
8. confidence 如实反映把握(0~1)；信号矛盾或数据不足时选 HOLD 并给低 confidence。
9. 只依据提供的数据判断，不臆造未提供的信息。

风格：专业、客观、基于证据。有把握的机会要敢于参与，不确定时也不勉强。追求长期正期望，而非频繁交易。
"""


def _fmt_sentiment(s) -> str:
    if s is None:
        return ""
    parts = [f"资金费率={s.funding_rate}", f"24h涨跌={s.change_24h_pct}%"]
    if s.long_short_ratio is not None:
        parts.append(f"多空比={s.long_short_ratio}")
    if s.open_interest is not None:
        parts.append(f"未平仓量={s.open_interest}")
    if s.fear_greed_index is not None:
        parts.append(f"恐慌贪婪指数={s.fear_greed_index}")
    return "  ".join(parts)


def _fmt_higher_tf(tfs) -> str:
    if not tfs:
        return "（未启用多周期）"
    lines = []
    for t in tfs:
        lines.append(
            f"  [{t.timeframe}] 趋势={t.trend} EMA12={t.ema_fast:.2f} EMA26={t.ema_slow:.2f} "
            f"RSI={t.rsi:.1f} MACD={t.macd:.2f}/{t.macd_signal:.2f}"
        )
    return "\n".join(lines)


def build_user_prompt(
    ctx: MarketContext,
    kline_interval: str = "5m",
    prompt_kline_count: int | None = None,
    micro_kline_count: int | None = None,
) -> str:
    """把 MarketContext 渲染成紧凑、信息密度高的 user prompt。"""
    pos = ctx.position
    if pos.has_position:
        pos_desc = (
            f"持仓: {pos.side} 数量={pos.size} 开仓价={pos.entry_price} "
            f"未实现盈亏={pos.unrealized_pnl_pct}% 当前杠杆={pos.current_leverage}x"
        )
    else:
        pos_desc = "持仓: 无（空仓）"

    ind = ctx.indicators
    # 只取最近 N 根主周期 K 线进 prompt，控制 token（完整序列已用于指标计算）
    main_count = prompt_kline_count if prompt_kline_count is not None else ctx.prompt_kline_count
    micro_count = micro_kline_count if micro_kline_count is not None else ctx.micro_kline_count
    recent = ctx.recent_klines[-main_count:]
    klines_brief = json.dumps(
        [[round(x, 4) for x in k] for k in recent], ensure_ascii=False
    )
    micro_recent = ctx.micro_klines[-micro_count:] if micro_count > 0 else []
    micro_brief = (
        json.dumps([[round(x, 4) for x in k] for k in micro_recent], ensure_ascii=False)
        if micro_recent else "（未获取）"
    )

    return f"""\
标的: {ctx.symbol}    主分析周期: {kline_interval}
最新价: {ctx.last_price}  标记价: {ctx.mark_price}
账户权益: {ctx.account_equity:.2f} USDT    可用保证金: {ctx.available_margin} USDT
风控允许最大杠杆(max_leverage_allowed): {ctx.max_leverage_allowed}x
单笔保证金上限: {ctx.max_order_margin_abs:.2f} USDT（= size_pct × 可用保证金，不得超过此值，否则被拒单）
单笔止损理论亏损上限: {ctx.max_loss_per_trade_abs:.2f} USDT（= size_pct × 可用保证金 × 杠杆 × stop_loss_pct）
说明: 请据账户权益与上限自主决定 size_pct（占可用保证金比例）与止损距离。
名义价值=size_pct×杠杆×可用保证金；杠杆不会放大保证金上限，但会放大名义价值与止损亏损。
{pos_desc}

市场情绪/资金面: {_fmt_sentiment(ctx.sentiment)}

技术指标(主周期 {kline_interval}，最新值):
  EMA(12)={ind.ema_fast:.4f}  EMA(26)={ind.ema_slow:.4f}
  RSI(14)={ind.rsi:.2f}
  MACD={ind.macd:.4f}  Signal={ind.macd_signal:.4f}  Hist={ind.macd_hist:.4f}
  ATR(14)={ind.atr:.4f}  ATR%={ind.atr_pct:.4f}
  Boll中轨={ind.boll_mid:.4f}  上轨={ind.boll_upper:.4f}  下轨={ind.boll_lower:.4f}
  成交量={ind.volume:.2f}  20均量={ind.volume_ma:.2f}  量比={ind.volume_ratio}（>1放量）

主周期结构化趋势特征（百分比字段单位均为 %，由完整K线窗口计算）:
  趋势={ind.trend_direction}  趋势一致性score={ind.trend_score:.3f}（-1强空，+1强多）
  EMA价差={ind.ema_spread_pct:.4f}  Δ3={ind.ema_spread_delta_3:.4f}
  Δ6={ind.ema_spread_delta_6:.4f}  Δ12={ind.ema_spread_delta_12:.4f}
  价格相对EMA12={ind.price_vs_ema_fast_pct:.4f}  相对EMA26={ind.price_vs_ema_slow_pct:.4f}
  收益率: 1根={ind.return_1_pct:.4f}  3根={ind.return_3_pct:.4f}
  6根={ind.return_6_pct:.4f}  12根={ind.return_12_pct:.4f}
  MACD柱变化: Δ3={ind.macd_hist_delta_3:.4f}  Δ6={ind.macd_hist_delta_6:.4f}
  RSI变化: Δ3={ind.rsi_delta_3:.2f}  Δ6={ind.rsi_delta_6:.2f}
  波动/位置: ATR%Δ6={ind.atr_pct_delta_6:.4f}
  Boll%B={ind.boll_percent_b:.4f}  Boll带宽={ind.boll_bandwidth_pct:.4f}
  最新K线振幅={ind.last_range_pct:.4f}  实体={ind.last_body_pct:.4f}
  量能变化: 量比Δ3={ind.volume_ratio_delta_3:.4f}  20量Z={ind.volume_zscore_20:.4f}

多周期指标(共振参考):
{_fmt_higher_tf(ctx.higher_timeframes)}

最近{len(recent)}根K线（{kline_interval}级别）[ts,open,high,low,close,volume]:
{klines_brief}

最近{len(micro_recent)}根微观K线（{ctx.micro_kline_interval}级别）[ts,open,high,low,close,volume]:
{micro_brief}

请基于以上多维数据，调用 submit_decision 给出本周期对 {ctx.symbol} 的交易决策。
"""
