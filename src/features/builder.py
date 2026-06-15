"""特征组装：把行情快照 + 持仓状态 → LLM 的 MarketContext。

只做特征工程，不含任何买卖判断。
"""
from __future__ import annotations

import time

from loguru import logger

from src.config.schema import Settings
from src.exchange.market_data import SymbolSnapshot
from src.features.indicators import compute_snapshot, compute_timeframe_brief
from src.llm.schema import (
    IndicatorSnapshot,
    MarketContext,
    MarketSentiment,
    PositionSnapshot,
    TimeframeIndicators,
)


def build_position_snapshot(raw: dict | None) -> PositionSnapshot:
    """从 ccxt position dict 构造 PositionSnapshot。raw=None 表示无持仓。"""
    if not raw:
        return PositionSnapshot(has_position=False)
    contracts = float(raw.get("contracts") or 0)
    if contracts == 0:
        return PositionSnapshot(has_position=False)
    side = raw.get("side")  # "long" | "short"
    return PositionSnapshot(
        has_position=True,
        side=side.upper() if side else None,
        entry_price=float(raw["entryPrice"]) if raw.get("entryPrice") else None,
        size=contracts,
        unrealized_pnl_pct=(
            float(raw["percentage"]) if raw.get("percentage") is not None else None
        ),
        current_leverage=int(raw["leverage"]) if raw.get("leverage") else None,
    )


def build_context(
    *,
    symbol: str,
    snapshot: SymbolSnapshot,
    position: PositionSnapshot,
    available_margin: float,
    settings: Settings,
    equity: float = 0.0,
    higher_tf_klines: dict[str, list[list[float]]] | None = None,
    micro_klines: list[list[float]] | None = None,
    sentiment_extra: dict | None = None,
) -> MarketContext | None:
    """组装单个 symbol 的 MarketContext。

    数据不可用（快照未就绪 / K线不足）时返回 None，调用方应跳过该 symbol。

    higher_tf_klines: {timeframe: klines} 形如 {"15m": [...], "1h": [...]}，
      由调用方(engine)预先拉好传入；缺失则多周期为空，不影响主决策。
    micro_klines: 短周期原始 K 线窗口，仅用于 Prompt 展示最近入场节奏。
    sentiment_extra: 额外情绪字段(long_short_ratio/open_interest/fear_greed_index)。
    """
    if not snapshot.is_ready:
        logger.warning("snapshot not ready for {}, skip", symbol)
        return None
    if len(snapshot.klines) < 30:  # 指标需要足够样本
        logger.warning("insufficient klines ({}) for {}", len(snapshot.klines), symbol)
        return None

    ind = compute_snapshot(snapshot.klines)

    # 多周期指标
    higher: list[TimeframeIndicators] = []
    for tf, kl in (higher_tf_klines or {}).items():
        if kl and len(kl) >= 30:
            higher.append(TimeframeIndicators(**compute_timeframe_brief(kl, tf)))

    # 市场情绪
    extra = sentiment_extra or {}
    sentiment = MarketSentiment(
        funding_rate=snapshot.funding_rate,
        change_24h_pct=snapshot.change_24h_pct,
        long_short_ratio=extra.get("long_short_ratio"),
        open_interest=extra.get("open_interest"),
        fear_greed_index=extra.get("fear_greed_index"),
    )

    # 资金基准：优先权益，缺失退回可用保证金。把保证金/止损亏损上限告知 LLM。
    equity_base = equity if equity > 0 else available_margin
    max_order_margin_abs = equity_base * settings.risk.max_order_margin_pct
    max_loss_per_trade_abs = max_order_margin_abs * (
        settings.risk.max_loss_per_order_margin_pct / 100.0
    )

    return MarketContext(
        symbol=symbol,
        timestamp=snapshot.updated_ms or int(time.time() * 1000),
        last_price=snapshot.last_price,
        mark_price=snapshot.mark_price or snapshot.last_price,
        funding_rate=snapshot.funding_rate,
        change_24h_pct=snapshot.change_24h_pct,
        recent_klines=snapshot.klines,
        prompt_kline_count=settings.llm.prompt_kline_count,
        micro_kline_interval=settings.llm.micro_kline_interval,
        micro_kline_count=settings.llm.micro_kline_lookback,
        micro_klines=micro_klines or [],
        indicators=IndicatorSnapshot(**ind),
        position=position,
        available_margin=available_margin,
        max_leverage_allowed=settings.risk.max_leverage,
        account_equity=equity_base,
        max_order_margin_abs=max_order_margin_abs,
        max_order_margin_pct=settings.risk.max_order_margin_pct,
        max_loss_per_trade_abs=max_loss_per_trade_abs,
        higher_timeframes=higher,
        sentiment=sentiment,
    )
