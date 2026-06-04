"""合约精度处理：按交易对的 tickSize / stepSize / minNotional 规整价格与数量。

ccxt 的 ``market`` 结构里：
- ``market['precision']['price']`` / ``['amount']``：小数位数或步长
- ``market['limits']['amount']['min']`` / ``['cost']['min']``：最小数量 / 最小名义价值

为避免对 ccxt 精度表示（有时是小数位、有时是步长）的歧义，这里统一接收
显式的 tick/step/min_notional，由 ExchangeClient 从 market 解析后传入。
所有计算用 Decimal，避免浮点累积误差。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class SymbolFilters:
    """单个交易对的精度与下限约束。"""
    tick_size: Decimal       # 价格最小变动
    step_size: Decimal       # 数量最小变动
    min_qty: Decimal         # 最小下单数量
    min_notional: Decimal    # 最小名义价值(USDT)

    @classmethod
    def from_ccxt_market(cls, market: dict) -> "SymbolFilters":
        """从 ccxt market 结构解析。优先用 limits，缺失时回退到 precision。"""
        def _dec(v, default: str) -> Decimal:
            if v is None:
                return Decimal(default)
            return Decimal(str(v))

        limits = market.get("limits", {}) or {}
        precision = market.get("precision", {}) or {}

        # ccxt 统一精度里，price/amount 多为「步长」(如 0.1)；若是整数位数则换算
        def _step_from_precision(p, default: str) -> Decimal:
            if p is None:
                return Decimal(default)
            pd = Decimal(str(p))
            # 位数模式：如 3 → 0.001；步长模式：如 0.001 直接用
            if pd >= 1 and pd == pd.to_integral_value():
                return Decimal(1).scaleb(-int(pd))
            return pd

        tick = _step_from_precision(precision.get("price"), "0.01")
        step = _step_from_precision(precision.get("amount"), "0.001")
        min_qty = _dec((limits.get("amount") or {}).get("min"), str(step))
        min_notional = _dec((limits.get("cost") or {}).get("min"), "5")
        return cls(tick_size=tick, step_size=step, min_qty=min_qty, min_notional=min_notional)


def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    quant = (value / step).to_integral_value(rounding=rounding) * step
    # 规整到与 step 相同的小数位，去除浮点尾巴
    return quant.quantize(step) if step < 1 else quant


def round_price(price: float, f: SymbolFilters) -> Decimal:
    """价格按 tickSize 就近规整（四舍五入）。"""
    return _round_to_step(Decimal(str(price)), f.tick_size, ROUND_HALF_UP)


def round_qty(qty: float, f: SymbolFilters) -> Decimal:
    """数量按 stepSize 向下取整（避免超出可用保证金）。"""
    return _round_to_step(Decimal(str(qty)), f.step_size, ROUND_DOWN)


@dataclass(frozen=True)
class NormalizedOrder:
    qty: Decimal
    price: Decimal | None  # MARKET 单为 None
    notional: Decimal


def normalize_order(
    *,
    qty: float,
    price: float,
    f: SymbolFilters,
    is_market: bool = True,
) -> NormalizedOrder | None:
    """把原始数量/价格规整为可下单参数。

    返回 None 表示规整后不满足最小约束（minQty / minNotional）→ 调用方应拒单。
    名义价值用规整后的数量 × 价格计算。
    """
    rq = round_qty(qty, f)
    rp = round_price(price, f)
    if rq < f.min_qty or rq <= 0:
        return None
    notional = rq * rp
    if notional < f.min_notional:
        return None
    return NormalizedOrder(
        qty=rq,
        price=None if is_market else rp,
        notional=notional,
    )
