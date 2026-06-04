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


class TimeframeIndicators(BaseModel):
    """单一周期的精简指标，用于多周期共振判断。"""
    timeframe: str                   # 如 "15m" / "1h"
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    trend: str                       # "up" | "down" | "flat"，由 EMA 关系给出的快捷判断


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


class MarketContext(BaseModel):
    """单个 symbol 的完整决策输入。"""
    symbol: str
    timestamp: int  # 毫秒
    last_price: float
    mark_price: float
    funding_rate: float
    change_24h_pct: float
    recent_klines: list[list[float]]  # [[ts,o,h,l,c,v], ...]
    indicators: IndicatorSnapshot
    position: PositionSnapshot
    available_margin: float
    max_leverage_allowed: int  # 把风控上限告知 LLM（仅参考）
    # 资金尺度与风险上限(USDT)，告知 LLM 以便其自主决定 size_pct/止损距离
    account_equity: float = 0.0
    max_order_margin_abs: float = 0.0
    max_loss_per_trade_abs: float = 0.0
    # 新增：多周期指标 + 市场情绪（可为空，向后兼容）
    higher_timeframes: list[TimeframeIndicators] = []
    sentiment: MarketSentiment | None = None


# ---------- 输出：LLM 必须返回的决策 ----------
class Action(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


class TradeDecision(BaseModel):
    """LLM 决策输出。extra=forbid → 多余字段直接拒绝。"""
    model_config = {"extra": "forbid"}

    symbol: str
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    size_pct: float = Field(ge=0.0, le=1.0)        # 占可用保证金比例
    leverage: int = Field(ge=1, le=125)            # 物理上限；业务上限在 risk 层
    stop_loss_pct: float = Field(ge=0.0, le=1.0)
    take_profit_pct: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=500)

    @field_validator("symbol")
    @classmethod
    def _symbol_upper(cls, v: str) -> str:
        return v.upper()

    @property
    def is_open(self) -> bool:
        return self.action in (Action.OPEN_LONG, Action.OPEN_SHORT)

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
            reason=f"[degraded] {reason}"[:500],
        )

    @classmethod
    def json_schema_for_llm(cls) -> dict:
        """供 LLM tool/结构化输出使用的 JSON Schema。"""
        return cls.model_json_schema()
