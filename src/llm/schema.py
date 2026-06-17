"""LLM 输入 / 输出的 pydantic 模型。

- 输入：``MarketContext`` —— 单个 symbol 的完整决策上下文，组装进 prompt。
- 输出：``TradeDecision`` —— LLM 必须返回的固定结构；任何越界/缺失/多余字段
  都会校验失败，由调用方统一降级为 ``safe_hold``。

注意：``leverage`` 在此只做 1~125 的合法性校验（交易所物理上限）；
业务上限 ``max_leverage`` 的拒单判定在 risk 层，不在这里截断。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


# ---------- 输入：喂给 LLM 的市场上下文 ----------
class IndicatorSnapshot(BaseModel):
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    atr: float
    boll_upper: float
    boll_lower: float
    # 成交量指标
    volume: float = 0.0              # 最新一根成交量
    volume_ma: float = 0.0           # 成交量均线(20)
    volume_ratio: float = 1.0        # 当前量 / 均量，>1 放量
    # 主周期结构化趋势特征：由完整 K 线窗口压缩而来，避免只给 LLM 最新单点值。
    trend_direction: str = "flat"    # "up" | "down" | "flat"，多因子快速判断
    trend_score: float = 0.0         # -1~1，越接近两端趋势一致性越强
    ema_spread_pct: float = 0.0      # (EMA快-EMA慢)/close * 100
    ema_spread_delta_3: float = 0.0
    ema_spread_delta_6: float = 0.0
    ema_spread_delta_12: float = 0.0
    price_vs_ema_fast_pct: float = 0.0
    price_vs_ema_slow_pct: float = 0.0
    return_1_pct: float = 0.0
    return_3_pct: float = 0.0
    return_6_pct: float = 0.0
    return_12_pct: float = 0.0
    macd_hist: float = 0.0
    macd_hist_delta_3: float = 0.0
    macd_hist_delta_6: float = 0.0
    rsi_delta_3: float = 0.0
    rsi_delta_6: float = 0.0
    atr_pct: float = 0.0             # ATR / close * 100，跨标的可比
    atr_pct_delta_6: float = 0.0
    atr_pct_percentile_96: float = 0.0
    boll_mid: float = 0.0
    boll_percent_b: float = 0.5      # close 在布林带内的位置，0=下轨，1=上轨
    boll_bandwidth_pct: float = 0.0  # (上轨-下轨)/中轨 * 100
    boll_bandwidth_percentile_96: float = 0.0
    last_range_pct: float = 0.0      # 最新 K 线 high-low / close * 100
    last_body_pct: float = 0.0       # 最新 K 线 close-open / open * 100，有方向
    upper_wick_pct: float = 0.0
    lower_wick_pct: float = 0.0
    body_to_range: float = 0.0
    consecutive_up_count: int = 0
    consecutive_down_count: int = 0
    volume_ratio_delta_3: float = 0.0
    volume_zscore_20: float = 0.0
    adx_14: float = 0.0
    plus_di_14: float = 0.0
    minus_di_14: float = 0.0
    vwap: float = 0.0
    price_vs_vwap_pct: float = 0.0
    vwap_slope_pct: float = 0.0
    swing_high: float = 0.0
    swing_low: float = 0.0
    dist_to_swing_high_pct: float = 0.0
    dist_to_swing_low_pct: float = 0.0
    range_position_pct: float = 0.5
    breakout_state: str = "inside_range"


class TimeframeIndicators(BaseModel):
    """单一周期的精简指标，用于多周期共振判断。"""
    timeframe: str                   # 如 "15m" / "1h"
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    trend: str                       # "up" | "down" | "flat"，由 EMA 关系给出的快捷判断
    swing_high: float = 0.0
    swing_low: float = 0.0
    dist_to_swing_high_pct: float = 0.0
    dist_to_swing_low_pct: float = 0.0
    range_position_pct: float = 0.5
    adx: float = 0.0
    atr_pct: float = 0.0
    breakout_state: str = "inside_range"


class MarketSentiment(BaseModel):
    """整体市场情绪/资金面。字段缺失用中性默认值。"""
    funding_rate: float = 0.0        # 资金费率，正=多头付费(偏多拥挤)
    change_24h_pct: float = 0.0      # 24h 涨跌
    long_short_ratio: float | None = None   # 多空持仓比(可选)
    open_interest: float | None = None       # 未平仓合约量(可选)
    fear_greed_index: int | None = None       # 恐惧贪婪指数 0-100(可选)


class PositionSnapshot(BaseModel):
    has_position: bool = False
    side: str | None = None  # LONG | SHORT | None
    entry_price: float | None = None
    size: float | None = None
    unrealized_pnl_pct: float | None = None
    current_leverage: int | None = None
    sl_price: float | None = None   # 当前交易所挂单的止损触发价（无则 None）
    tp_price: float | None = None   # 当前交易所挂单的止盈触发价（无则 None）
    opened_at_ms: int | None = None
    position_age_minutes: float | None = None
    position_age_1m_bars: int | None = None
    last_sltp_adjust_at_ms: int | None = None
    minutes_since_last_sltp_adjust: float | None = None
    close_confirm_count: int = 0


class MarketContext(BaseModel):
    """单个 symbol 的完整决策输入。"""
    symbol: str
    timestamp: int  # 毫秒
    last_price: float
    mark_price: float
    funding_rate: float
    change_24h_pct: float
    recent_klines: list[list[float]]  # [[ts,o,h,l,c,v], ...]
    prompt_kline_count: int = 20
    micro_kline_interval: str = "1m"
    micro_kline_count: int = 30
    micro_klines: list[list[float]] = []  # [[ts,o,h,l,c,v], ...] 短周期入场节奏
    indicators: IndicatorSnapshot
    position: PositionSnapshot
    available_margin: float
    max_leverage_allowed: int  # 把风控上限告知 LLM（仅参考）
    # 资金尺度与风险上限(USDT)，告知 LLM 以便其自主决定 size_pct/止损距离
    account_equity: float = 0.0
    max_order_margin_abs: float = 0.0
    # 硬上限百分比（0~1），由 config.yaml risk.max_order_margin_pct 设置；
    # user prompt 透传给 LLM，让 LLM 在决策时知道 size_pct 的硬边界。
    max_order_margin_pct: float = 0.0
    max_loss_per_trade_abs: float = 0.0
    # 新增：多周期指标 + 市场情绪（可为空，向后兼容）
    higher_timeframes: list[TimeframeIndicators] = []
    sentiment: MarketSentiment | None = None


# ---------- 输出：LLM 必须返回的决策 ----------
DECISION_REASON_MAX_LENGTH = 1000


class Action(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"
    ADJUST_SLTP = "ADJUST_SLTP"   # 已有持仓时调整止盈止损，不平仓


class TradeDecision(BaseModel):
    """LLM 决策输出。extra=forbid → 多余字段直接拒绝。"""
    model_config = {"extra": "forbid"}

    symbol: str = Field(description="交易标的，例如 BTCUSDT。")
    action: Action = Field(description="本周期动作：OPEN_LONG、OPEN_SHORT、CLOSE 或 HOLD。")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="决策置信度，0~1；低于系统 min_confidence 会被视为不开仓。",
    )
    size_pct: float = Field(
        ge=0.0, le=1.0,
        description="动用可用保证金比例；0.15 表示使用可用保证金的 15%，不是权益亏损比例。",
    )
    leverage: int = Field(
        ge=1, le=125,
        description="建议杠杆；超过系统 max_leverage 会被直接拒单，不会截断。",
    )
    stop_loss_pct: float = Field(
        ge=0.0, le=1.0,
        description=(
            "相对基准价的价格止损距离小数，不是保证金比例或权益比例；"
            "百分比=小数×100；0.012 必须解释为 1.20% 价格距离。"
            "OPEN 时基准价=entry_ref；ADJUST_SLTP 时基准价=当前标记价 mark。"
        ),
    )
    take_profit_pct: float = Field(
        ge=0.0, le=1.0,
        description=(
            "相对基准价的价格止盈距离小数，不是保证金比例或权益比例；"
            "百分比=小数×100；0.02 必须解释为 2.00% 价格距离。"
            "OPEN 时基准价=entry_ref；ADJUST_SLTP 时基准价=当前标记价 mark。"
        ),
    )
    reason: str = Field(
        max_length=DECISION_REASON_MAX_LENGTH,
        description=(
            "中文决策理由。OPEN_LONG/OPEN_SHORT 必须包含风险换算："
            "stop_loss_pct/take_profit_pct 的小数值与百分比、预估 SL/TP 触发价、"
            "预估止损亏损/止盈收益 USDT、亏损占账户权益百分比、亏损占本单保证金百分比。"
            "预估触发价必须与 action 方向一致：OPEN_LONG 的 SL 低于 entry_ref、TP 高于 entry_ref；"
            "OPEN_SHORT 的 SL 高于 entry_ref、TP 低于 entry_ref。"
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _symbol_upper(cls, v: str) -> str:
        return v.upper()

    @property
    def is_open(self) -> bool:
        return self.action in (Action.OPEN_LONG, Action.OPEN_SHORT)

    @property
    def is_adjust(self) -> bool:
        return self.action == Action.ADJUST_SLTP

    @classmethod
    def safe_hold(cls, symbol: str, reason: str) -> "TradeDecision":
        """解析失败 / 超时 / 越界时的统一降级出口。"""
        return cls(
            symbol=symbol.upper(),
            action=Action.HOLD,
            confidence=0.0,
            size_pct=0.0,
            leverage=1,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            reason=f"[degraded] {reason}"[:DECISION_REASON_MAX_LENGTH],
        )

    @classmethod
    def json_schema_for_llm(cls) -> dict:
        """供 LLM tool/结构化输出使用的 JSON Schema。"""
        return cls.model_json_schema()
