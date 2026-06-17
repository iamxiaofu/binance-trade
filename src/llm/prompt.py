"""构建发给 Claude 的 system / user prompt。

prompt 内绝不包含任何 API Key 或账户私密标识。只放行情与持仓特征。
决策结构由 tool schema 强制（见 client.py），prompt 负责说明语义与纪律。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from src.llm.schema import MarketContext

RENDER_MODE_LEGACY_APPEND = "legacy_append"
RENDER_MODE_FULL_TEMPLATE = "full_template"
TEMPLATE_SCHEMA_VERSION = 1

SYSTEM_PROMPT = """\
你是一位经验丰富的加密货币永续合约量化交易专家，负责 Binance USDT-M 永续合约的实盘决策。
你的判断将由下游 Python 系统直接执行下单，因此每个决策都要有清晰的逻辑依据。

你的专业能力：
- 综合多周期趋势、动量(MACD/RSI)、波动率(ATR/布林带)、成交量与市场情绪(资金费率/多空比)做出权衡。
- 识别趋势延续、突破、回调与反转，区分高胜率机会与噪音。
- 在机会明确时果断进场，在信号不足时保持耐心。

决策原则：
1. 必须调用 `submit_decision` 工具返回结构化决策，不要输出自由文本。
2. action ∈ {OPEN_LONG, OPEN_SHORT, CLOSE, HOLD, ADJUST_SLTP}。
   - OPEN_LONG / OPEN_SHORT：无持仓时新开仓。
   - CLOSE：平掉当前持仓（止盈、止损或反向）。
   - HOLD：维持现状，不操作。
   - ADJUST_SLTP：已有持仓，**不平仓**，仅调整止盈止损触发价。
     适用场景：行情已走出一段、想移动止损锁定利润（trailing stop）；
     或波动放大、需给持仓更大呼吸空间；或原始 SL/TP 已不合理需修正。
     ⚠ ADJUST_SLTP 时 stop_loss_pct / take_profit_pct 以**当前标记价 mark** 为基准：
       多单: SL = mark×(1−stop_loss_pct)，TP = mark×(1+take_profit_pct)
       空单: SL = mark×(1+stop_loss_pct)，TP = mark×(1−take_profit_pct)
     leverage / size_pct 填占位值（如 leverage=1, size_pct=0），系统不读取，不会修改仓位杠杆。
     无持仓时 ADJUST_SLTP 将被忽略，请改用 HOLD。
3. 多周期共振(高周期与当前周期方向一致)时机会更可靠，可给更高 confidence。
4. 关注量价配合：放量突破比缩量更可信；背离需警惕。
5. leverage 不要超过 max_leverage_allowed（超过会被系统直接拒单）。资金量小，杠杆宜适中。
6. size_pct 为动用可用保证金比例(0~1)。单笔保证金硬上限 max_order_margin_pct 按账户权益动态计算，
   系统校验 margin_used=可用保证金×size_pct 不得超过该绝对上限；超出会直接拒单（不截断、不调整）。
   当可用保证金≈账户权益时，max_order_margin_pct 通常约等于 size_pct 上限（常见约 0.2）。
7. stop_loss_pct / take_profit_pct 为相对参考开仓价的价格距离小数，不是保证金比例，也不是账户权益比例。
   0.012 必须表述为 1.20% 价格距离，不能写成 0.12%；0.02 必须表述为 2.00% 价格距离。
8. confidence 如实反映把握(0~1)；信号矛盾或数据不足时选 HOLD 并给低 confidence。
9. OPEN_LONG/OPEN_SHORT 的 reason 必须同时写清风险换算：小数值与百分比、预估 SL/TP 触发价、
   预估止损亏损/止盈收益 USDT、止损亏损占账户权益百分比、止损亏损占本单保证金百分比。
   ADJUST_SLTP 的 reason 必须说明调整原因、新 SL/TP 触发价（以 mark 为基准计算）、
   与旧 SL/TP 的变化方向（收紧/放宽/移至盈利侧）。
10. reason 的 SL/TP 触发价必须严格按 action 方向计算：
    OPEN_LONG: SL=entry_ref×(1-stop_loss_pct) 低于 entry_ref；TP=entry_ref×(1+take_profit_pct) 高于 entry_ref。
    OPEN_SHORT: SL=entry_ref×(1+stop_loss_pct) 高于 entry_ref；TP=entry_ref×(1-take_profit_pct) 低于 entry_ref。
    ADJUST_SLTP 多单: SL=mark×(1-stop_loss_pct) 低于 mark；TP=mark×(1+take_profit_pct) 高于 mark。
    ADJUST_SLTP 空单: SL=mark×(1+stop_loss_pct) 高于 mark；TP=mark×(1-take_profit_pct) 低于 mark。
    方向不满足时必须重新计算后再提交。
11. 如果 reason 中的百分比、触发价或损益估算与结构化字段不一致，必须修正 reason 后再提交。
12. 只依据提供的数据判断，不臆造未提供的信息。
13. 主动 CLOSE 是高摩擦动作，只能在结构明显破坏时使用：
    - 持仓未满 5 根 1m K 线，不要因为 1m 反向波动提交 CLOSE。
    - 浮亏未超过约 1 ATR 时，不要因为 1m 反向波动提交 CLOSE，应优先 HOLD 或合理 ADJUST_SLTP。
    - 主动 CLOSE 需要连续证据支持；单次噪音、影线或微小回撤不足以平仓。
14. ADJUST_SLTP 应避免频繁改价：
    - 同一仓位移动 SL 至少间隔约 15 分钟。
    - 新 SL 必须比旧 SL 有明显改善，至少约 0.4 ATR。
    - 保本/锁利 SL 必须覆盖手续费与滑点缓冲，不能把 SL 刚好放在 entry 附近。

风格：专业、客观、基于证据。有把握的机会要敢于参与，不确定时也不勉强。追求长期正期望，而非频繁交易。
"""
DEFAULT_SYSTEM_PROMPT_TEMPLATE = SYSTEM_PROMPT

DEFAULT_USER_PROMPT_TEMPLATE = """\
标的: {symbol}    主分析周期: {kline_interval}
最新价: {last_price}  标记价: {mark_price}
账户权益: {account_equity_2} USDT    可用保证金: {available_margin} USDT
风控允许最大杠杆(max_leverage_allowed): {max_leverage_allowed}x
单笔保证金硬上限: margin_used ≤ {max_order_margin_abs_2} USDT (= max_order_margin_pct {max_order_margin_pct_4} × 账户权益 {account_equity_2})，硬性约束，超出直接拒单
   size_pct 参考上限: 当可用保证金≈账户权益时约 ≤ {max_order_margin_pct_percent_1}%；实际必须用 margin_used=可用保证金×size_pct 交叉校验
单笔止损理论亏损上限: {max_loss_per_trade_abs_2} USDT（按单笔保证金止损亏损比例约束）
说明: 请据账户权益与上限自主决定 size_pct（占可用保证金比例）与止损距离。
名义价值=size_pct×杠杆×可用保证金；杠杆不会放大保证金上限，但会放大名义价值与止损亏损。
{position_block}

风险字段语义与 reason 必填格式:
  - 本周期估算参考开仓价 entry_ref = 最新价 {last_price}；实际成交价可能由执行层按盘口略有偏移。
  - stop_loss_pct / take_profit_pct 是价格距离小数，不是保证金比例，也不是账户权益比例。
    OPEN 时基准=entry_ref；ADJUST_SLTP 时基准=当前标记价 mark={mark_price}。
  - 百分比换算公式: pct_percent = pct_decimal × 100。
  - 0.012 必须写为 1.20% 价格距离，不能写成 0.12%；0.02 必须写为 2.00% 价格距离。
  - 风险换算必须严格以 action 为方向基准，先确认 action 再计算 SL/TP。
  - OPEN_LONG: SL=entry_ref×(1-stop_loss_pct) 且必须低于 entry_ref；TP=entry_ref×(1+take_profit_pct) 且必须高于 entry_ref。
  - OPEN_SHORT: SL=entry_ref×(1+stop_loss_pct) 且必须高于 entry_ref；TP=entry_ref×(1-take_profit_pct) 且必须低于 entry_ref。
  - ADJUST_SLTP 多单: SL=mark×(1-stop_loss_pct) 且必须低于 mark；TP=mark×(1+take_profit_pct) 且必须高于 mark。
  - ADJUST_SLTP 空单: SL=mark×(1+stop_loss_pct) 且必须高于 mark；TP=mark×(1-take_profit_pct) 且必须低于 mark。
  - 若计算出的 SL/TP 方向与 action 不一致，必须重新计算；不允许输出与 action 冲突的 reason。
  - reason 中的 SL/TP 是基于 entry_ref（OPEN）或 mark（ADJUST_SLTP）的预估触发价；实际成交后系统会用交易所实际价格重算保护单。
  - margin_used=可用保证金×size_pct；notional=margin_used×leverage。
  - sl_loss≈notional×stop_loss_pct；tp_profit≈notional×take_profit_pct。
  - equity_loss_pct≈sl_loss÷账户权益×100；margin_loss_pct≈sl_loss÷margin_used×100。
  - R≈tp_profit÷sl_loss。
  - 若 action 为 OPEN_LONG/OPEN_SHORT，reason 必须包含紧凑风险块：
    风险换算: entry_ref=...; SL_pct=小数=>百分比, SL≈...; TP_pct=小数=>百分比, TP≈...; 亏损≈...USDT(权益...%, 保证金...%); 收益≈...USDT; R≈...

市场情绪/资金面: {sentiment_block}

技术指标(主周期 {kline_interval}，最新值):
{indicator_block}

主周期结构化趋势特征（百分比字段单位均为 %，由完整K线窗口计算）:
{trend_feature_block}

主周期结构与执行纪律:
{structure_block}

多周期指标(共振参考):
{higher_timeframes_block}

最近{recent_count}根K线（{kline_interval}级别）[ts,open,high,low,close,volume]:
{recent_klines_json}

最近{micro_recent_count}根微观K线（{micro_kline_interval}级别）[ts,open,high,low,close,volume]:
{micro_klines_json}

请基于以上多维数据，调用 submit_decision 给出本周期对 {symbol} 的交易决策。
"""

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class PromptRenderResult:
    text: str
    warnings: list[str]
    unknown_placeholders: list[str]


def build_system_prompt(addendum: str | None = None) -> str:
    """返回最终发给 LLM 的 system prompt。

    ``addendum`` 是运行期可由前端编辑的附加策略指令。固定系统硬规则始终保留，
    附加指令只能补充偏好，不能替代风控纪律或输出 schema。
    """
    extra = (addendum or "").strip()
    if not extra:
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT.rstrip()}\n\n"
        "运行期附加策略指令（不得覆盖以上硬性约束；如冲突，以上硬性约束优先）：\n"
        f"{extra}\n"
    )


def render_template(template: str, variables: dict[str, Any]) -> PromptRenderResult:
    """Render a safe prompt template with a fixed placeholder namespace.

    Only ``{identifier}`` placeholders are replaced. Other braces are left intact,
    so operators can still write JSON examples without escaping every brace.
    """
    unknown: list[str] = []

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            if key not in unknown:
                unknown.append(key)
            return match.group(0)
        return str(variables[key])

    text = _PLACEHOLDER_RE.sub(repl, template or "")
    warnings = [f"unknown placeholder: {key}" for key in unknown]
    return PromptRenderResult(text=text, warnings=warnings, unknown_placeholders=unknown)


def render_system_prompt_template(template: str) -> PromptRenderResult:
    result = render_template(template or DEFAULT_SYSTEM_PROMPT_TEMPLATE, {})
    warnings = list(result.warnings)
    required_markers = (
        "submit_decision", "OPEN_LONG", "OPEN_SHORT", "HOLD",
        "size_pct", "stop_loss_pct", "take_profit_pct",
    )
    missing = [m for m in required_markers if m not in result.text]
    if missing:
        warnings.append(f"system prompt missing recommended markers: {', '.join(missing)}")
    return PromptRenderResult(result.text, warnings, result.unknown_placeholders)


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
            f"RSI={t.rsi:.1f} MACD={t.macd:.2f}/{t.macd_signal:.2f} "
            f"ADX={t.adx:.1f} ATR%={t.atr_pct:.3f} "
            f"Swing高/低={t.swing_high:.4f}/{t.swing_low:.4f} "
            f"区间位置={t.range_position_pct:.3f} 突破状态={t.breakout_state}"
        )
    return "\n".join(lines)


def _position_block(ctx: MarketContext) -> str:
    pos = ctx.position
    if pos.has_position:
        sl_desc = f"SL≈{pos.sl_price}" if pos.sl_price else "SL=未挂"
        tp_desc = f"TP≈{pos.tp_price}" if pos.tp_price else "TP=未挂"
        return (
            f"持仓: {pos.side} 数量={pos.size} 开仓价={pos.entry_price} "
            f"未实现盈亏={pos.unrealized_pnl_pct}% 当前杠杆={pos.current_leverage}x  "
            f"当前保护单: {sl_desc} / {tp_desc}"
        )
    return "持仓: 无（空仓）"


def user_prompt_variables(
    ctx: MarketContext,
    kline_interval: str = "5m",
    prompt_kline_count: int | None = None,
    micro_kline_count: int | None = None,
) -> dict[str, Any]:
    """Build the whitelisted placeholder namespace for user prompt templates."""
    ind = ctx.indicators
    main_count = prompt_kline_count if prompt_kline_count is not None else ctx.prompt_kline_count
    micro_count = micro_kline_count if micro_kline_count is not None else ctx.micro_kline_count
    recent = ctx.recent_klines[-main_count:]
    micro_recent = ctx.micro_klines[-micro_count:] if micro_count > 0 else []
    recent_json = json.dumps([[round(x, 4) for x in k] for k in recent], ensure_ascii=False)
    micro_json = (
        json.dumps([[round(x, 4) for x in k] for k in micro_recent], ensure_ascii=False)
        if micro_recent else "（未获取）"
    )
    indicator_block = f"""\
  EMA(12)={ind.ema_fast:.4f}  EMA(26)={ind.ema_slow:.4f}
  RSI(14)={ind.rsi:.2f}
  MACD={ind.macd:.4f}  Signal={ind.macd_signal:.4f}  Hist={ind.macd_hist:.4f}
  ATR(14)={ind.atr:.4f}  ATR%={ind.atr_pct:.4f}
  ADX(14)={ind.adx_14:.2f}  +DI={ind.plus_di_14:.2f}  -DI={ind.minus_di_14:.2f}
  VWAP={ind.vwap:.4f}  价格相对VWAP={ind.price_vs_vwap_pct:.4f}  VWAP斜率12根={ind.vwap_slope_pct:.4f}
  Boll中轨={ind.boll_mid:.4f}  上轨={ind.boll_upper:.4f}  下轨={ind.boll_lower:.4f}
  成交量={ind.volume:.2f}  20均量={ind.volume_ma:.2f}  量比={ind.volume_ratio}（>1放量）"""
    trend_feature_block = f"""\
  趋势={ind.trend_direction}  趋势一致性score={ind.trend_score:.3f}（-1强空，+1强多）
  EMA价差={ind.ema_spread_pct:.4f}  Δ3={ind.ema_spread_delta_3:.4f}
  Δ6={ind.ema_spread_delta_6:.4f}  Δ12={ind.ema_spread_delta_12:.4f}
  价格相对EMA12={ind.price_vs_ema_fast_pct:.4f}  相对EMA26={ind.price_vs_ema_slow_pct:.4f}
  收益率: 1根={ind.return_1_pct:.4f}  3根={ind.return_3_pct:.4f}
  6根={ind.return_6_pct:.4f}  12根={ind.return_12_pct:.4f}
  MACD柱变化: Δ3={ind.macd_hist_delta_3:.4f}  Δ6={ind.macd_hist_delta_6:.4f}
  RSI变化: Δ3={ind.rsi_delta_3:.2f}  Δ6={ind.rsi_delta_6:.2f}
  波动/位置: ATR%Δ6={ind.atr_pct_delta_6:.4f}
  ATR分位96={ind.atr_pct_percentile_96:.2f}  Boll%B={ind.boll_percent_b:.4f}
  Boll带宽={ind.boll_bandwidth_pct:.4f}  Boll带宽分位96={ind.boll_bandwidth_percentile_96:.2f}
  最新K线振幅={ind.last_range_pct:.4f}  实体={ind.last_body_pct:.4f}
  上影线={ind.upper_wick_pct:.4f}  下影线={ind.lower_wick_pct:.4f}  实体/全振幅={ind.body_to_range:.4f}
  连续上涨K数={ind.consecutive_up_count}  连续下跌K数={ind.consecutive_down_count}
  量能变化: 量比Δ3={ind.volume_ratio_delta_3:.4f}  20量Z={ind.volume_zscore_20:.4f}"""
    structure_block = f"""\
  最近48根Swing高={ind.swing_high:.4f}  Swing低={ind.swing_low:.4f}
  距Swing高={ind.dist_to_swing_high_pct:.4f}%  距Swing低={ind.dist_to_swing_low_pct:.4f}%
  区间位置={ind.range_position_pct:.4f}（0接近低点，1接近高点）  突破状态={ind.breakout_state}
  CLOSE纪律: 持仓未满5根1m、浮亏未超过约1 ATR、或只有单次1m反向噪音时，优先 HOLD/ADJUST_SLTP，不主动 CLOSE。
  SL纪律: 同仓位移动SL约15分钟最多一次；新SL至少改善0.4 ATR；保本/锁利必须覆盖手续费与滑点。"""
    return {
        "symbol": ctx.symbol,
        "timestamp": ctx.timestamp,
        "kline_interval": kline_interval,
        "last_price": ctx.last_price,
        "mark_price": ctx.mark_price,
        "funding_rate": ctx.funding_rate,
        "change_24h_pct": ctx.change_24h_pct,
        "account_equity": ctx.account_equity,
        "account_equity_2": f"{ctx.account_equity:.2f}",
        "available_margin": ctx.available_margin,
        "max_leverage_allowed": ctx.max_leverage_allowed,
        "max_order_margin_abs": ctx.max_order_margin_abs,
        "max_order_margin_abs_2": f"{ctx.max_order_margin_abs:.2f}",
        "max_order_margin_pct": ctx.max_order_margin_pct,
        "max_order_margin_pct_4": f"{ctx.max_order_margin_pct:.4f}",
        "max_order_margin_pct_percent_1": f"{ctx.max_order_margin_pct * 100:.1f}",
        "max_loss_per_trade_abs": ctx.max_loss_per_trade_abs,
        "max_loss_per_trade_abs_2": f"{ctx.max_loss_per_trade_abs:.2f}",
        "position_block": _position_block(ctx),
        "sentiment_block": _fmt_sentiment(ctx.sentiment),
        "indicator_block": indicator_block,
        "trend_feature_block": trend_feature_block,
        "structure_block": structure_block,
        "higher_timeframes_block": _fmt_higher_tf(ctx.higher_timeframes),
        "recent_count": len(recent),
        "recent_klines_json": recent_json,
        "micro_kline_interval": ctx.micro_kline_interval,
        "micro_recent_count": len(micro_recent),
        "micro_klines_json": micro_json,
    }


def render_user_prompt_template(
    template: str,
    ctx: MarketContext,
    kline_interval: str = "5m",
    prompt_kline_count: int | None = None,
    micro_kline_count: int | None = None,
) -> PromptRenderResult:
    variables = user_prompt_variables(
        ctx,
        kline_interval=kline_interval,
        prompt_kline_count=prompt_kline_count,
        micro_kline_count=micro_kline_count,
    )
    result = render_template(template or DEFAULT_USER_PROMPT_TEMPLATE, variables)
    warnings = list(result.warnings)
    required = ("symbol", "last_price", "mark_price", "position_block", "indicator_block")
    missing = [p for p in required if f"{{{p}}}" not in (template or DEFAULT_USER_PROMPT_TEMPLATE)]
    if missing:
        warnings.append(f"user prompt template missing recommended placeholders: {', '.join(missing)}")
    return PromptRenderResult(result.text, warnings, result.unknown_placeholders)


def render_prompts(
    *,
    ctx: MarketContext,
    prompt_version: dict[str, Any] | None = None,
    kline_interval: str = "5m",
    prompt_kline_count: int | None = None,
    micro_kline_count: int | None = None,
) -> tuple[str, str, list[str]]:
    """Render final system/user prompts for the active prompt version."""
    mode = (prompt_version or {}).get("render_mode") or RENDER_MODE_LEGACY_APPEND
    if mode == RENDER_MODE_FULL_TEMPLATE:
        system = render_system_prompt_template(
            (prompt_version or {}).get("system_prompt_template") or DEFAULT_SYSTEM_PROMPT_TEMPLATE
        )
        user = render_user_prompt_template(
            (prompt_version or {}).get("user_prompt_template") or DEFAULT_USER_PROMPT_TEMPLATE,
            ctx,
            kline_interval=kline_interval,
            prompt_kline_count=prompt_kline_count,
            micro_kline_count=micro_kline_count,
        )
        return system.text, user.text, [*system.warnings, *user.warnings]
    system_text = build_system_prompt((prompt_version or {}).get("content", ""))
    user_text = build_user_prompt(
        ctx,
        kline_interval=kline_interval,
        prompt_kline_count=prompt_kline_count,
        micro_kline_count=micro_kline_count,
    )
    return system_text, user_text, []


def build_user_prompt(
    ctx: MarketContext,
    kline_interval: str = "5m",
    prompt_kline_count: int | None = None,
    micro_kline_count: int | None = None,
) -> str:
    """把 MarketContext 渲染成紧凑、信息密度高的 user prompt。"""
    return render_user_prompt_template(
        DEFAULT_USER_PROMPT_TEMPLATE,
        ctx,
        kline_interval=kline_interval,
        prompt_kline_count=prompt_kline_count,
        micro_kline_count=micro_kline_count,
    ).text
