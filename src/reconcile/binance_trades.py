"""Deterministic Binance fill ownership and position-lifecycle reconstruction.

The replayer deliberately separates order ownership from lifecycle ownership:
every fill belongs to exactly one creator (engine or external), while a lifecycle
becomes mixed when fills from both creators contribute to it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Iterable


EPSILON = Decimal("0.000000000001")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal(0)


def _f(value: Decimal) -> float:
    return float(value)


def _trade_sort_key(fill: "CanonicalFill") -> tuple[int, int, str, int]:
    try:
        trade_number = int(fill.exchange_trade_id)
    except (TypeError, ValueError):
        trade_number = 0
    return fill.ts_ms, trade_number, fill.exchange_trade_id, fill.exchange_fill_id


@dataclass(frozen=True)
class CanonicalFill:
    exchange_fill_id: int
    ts_ms: int
    symbol: str
    exchange_trade_id: str
    exchange_order_id: str
    client_order_id: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    liquidity: str
    reduce_only: bool
    ownership: str
    order_type: str = ""
    exit_reason: str = ""
    algo_id: str = ""
    metadata_source: str = ""

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "CanonicalFill":
        return cls(
            exchange_fill_id=int(row.get("exchange_fill_id") or row.get("id") or 0),
            ts_ms=int(row.get("ts_ms") or 0),
            symbol=str(row.get("symbol") or "").upper(),
            exchange_trade_id=str(row.get("exchange_trade_id") or ""),
            exchange_order_id=str(row.get("exchange_order_id") or ""),
            client_order_id=str(row.get("client_order_id") or ""),
            side=str(row.get("side") or "").lower(),
            qty=_d(row.get("qty")),
            price=_d(row.get("price")),
            fee=_d(row.get("fee")),
            realized_pnl=_d(row.get("realized_pnl")),
            liquidity=str(row.get("liquidity") or ""),
            reduce_only=bool(row.get("reduce_only")),
            ownership=str(row.get("ownership") or ""),
            order_type=str(row.get("order_type") or ""),
            exit_reason=str(row.get("exit_reason") or ""),
            algo_id=str(row.get("algo_id") or ""),
            metadata_source=str(row.get("metadata_source") or ""),
        )


@dataclass
class FillAllocation:
    exchange_fill_id: int
    role: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    fill_ownership: str
    exit_reason: str = ""

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("qty", "price", "fee", "realized_pnl"):
            row[key] = _f(row[key])
        return row


@dataclass
class TradeCycle:
    sequence: int
    symbol: str
    direction: str
    ownership: str
    status: str
    opened_at_ms: int
    closed_at_ms: int = 0
    entry_price: Decimal = Decimal(0)
    exit_price: Decimal = Decimal(0)
    qty_opened: Decimal = Decimal(0)
    qty_closed: Decimal = Decimal(0)
    entry_notional: Decimal = Decimal(0)
    exit_notional: Decimal = Decimal(0)
    entry_fee: Decimal = Decimal(0)
    exit_fee: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    entry_liquidity: str = ""
    exit_liquidity: str = ""
    exit_reason: str = ""
    confidence: str = "exact"
    classification_reason: str = ""
    allocations: list[FillAllocation] = field(default_factory=list)
    _owners: set[str] = field(default_factory=set, repr=False)
    _exit_reasons: set[str] = field(default_factory=set, repr=False)

    @property
    def open_qty(self) -> Decimal:
        return max(self.qty_opened - self.qty_closed, Decimal(0))

    def add_owner(self, owner: str) -> None:
        if owner:
            self._owners.add(owner)
        self.ownership = "mixed" if len(self._owners) > 1 else next(iter(self._owners), "external")

    def add_exit_reason(self, reason: str) -> None:
        reason = reason or "MANUAL_REDUCE"
        self._exit_reasons.add(reason)
        self.exit_reason = reason if len(self._exit_reasons) == 1 else "MIXED_EXIT"

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("_owners", None)
        row.pop("_exit_reasons", None)
        row["allocations"] = [item.public() for item in self.allocations]
        for key in (
            "entry_price", "exit_price", "qty_opened", "qty_closed",
            "entry_notional", "exit_notional", "entry_fee", "exit_fee",
            "realized_pnl",
        ):
            row[key] = _f(row[key])
        row["total_fee"] = _f(self.entry_fee + self.exit_fee)
        row["gross_realized_pnl"] = _f(self.realized_pnl)
        row["net_realized_pnl"] = _f(self.realized_pnl - self.entry_fee - self.exit_fee)
        return row


@dataclass
class ReplayResult:
    cycles: list[TradeCycle]
    final_positions: dict[str, Decimal]
    allocation_qty: dict[int, Decimal]
    allocation_fee: dict[int, Decimal]
    allocation_pnl: dict[int, Decimal]
    errors: list[str]

    def public(self) -> dict[str, Any]:
        return {
            "cycles": [cycle.public() for cycle in self.cycles],
            "final_positions": {key: _f(value) for key, value in self.final_positions.items()},
            "errors": list(self.errors),
        }


def replay_trade_cycles(
    fills: Iterable[CanonicalFill],
    *,
    initial_positions: dict[str, Any] | None = None,
) -> ReplayResult:
    """Replay one-way signed positions and split fills only at real boundaries."""
    ordered = sorted(fills, key=lambda item: (item.symbol, *_trade_sort_key(item)))
    by_symbol: dict[str, list[CanonicalFill]] = {}
    for fill in ordered:
        by_symbol.setdefault(fill.symbol, []).append(fill)

    cycles: list[TradeCycle] = []
    final_positions: dict[str, Decimal] = {}
    allocation_qty: dict[int, Decimal] = {}
    allocation_fee: dict[int, Decimal] = {}
    allocation_pnl: dict[int, Decimal] = {}
    errors: list[str] = []
    sequence = 0

    def allocate(cycle: TradeCycle, fill: CanonicalFill, role: str, qty: Decimal,
                 fee: Decimal, pnl: Decimal = Decimal(0)) -> None:
        cycle.allocations.append(FillAllocation(
            exchange_fill_id=fill.exchange_fill_id,
            role=role,
            qty=qty,
            price=fill.price,
            fee=fee,
            realized_pnl=pnl,
            fill_ownership=fill.ownership,
            exit_reason=fill.exit_reason if role == "EXIT" else "",
        ))
        allocation_qty[fill.exchange_fill_id] = allocation_qty.get(fill.exchange_fill_id, Decimal(0)) + qty
        allocation_fee[fill.exchange_fill_id] = allocation_fee.get(fill.exchange_fill_id, Decimal(0)) + fee
        allocation_pnl[fill.exchange_fill_id] = allocation_pnl.get(fill.exchange_fill_id, Decimal(0)) + pnl

    for symbol, symbol_fills in by_symbol.items():
        signed = _d((initial_positions or {}).get(symbol))
        if abs(signed) <= EPSILON:
            signed = Decimal(0)
        current: TradeCycle | None = None
        if abs(signed) > EPSILON:
            sequence += 1
            current = TradeCycle(
                sequence=sequence,
                symbol=symbol,
                direction="long" if signed > 0 else "short",
                ownership="external",
                status="open",
                opened_at_ms=symbol_fills[0].ts_ms if symbol_fills else 0,
                qty_opened=abs(signed),
                confidence="carry_in",
                classification_reason=(
                    "position existed before the synchronized fill window; "
                    "entry price and creator are intentionally unknown"
                ),
                _owners={"external"},
            )
            cycles.append(current)

        for fill in symbol_fills:
            if abs(signed) <= EPSILON:
                signed = Decimal(0)
            if fill.side not in {"buy", "sell"} or fill.qty <= 0 or fill.price <= 0:
                errors.append(f"invalid fill {symbol}:{fill.exchange_trade_id}")
                continue
            if fill.ownership not in {"engine", "external"}:
                errors.append(
                    f"unresolved ownership {symbol}:{fill.exchange_trade_id}={fill.ownership}"
                )
                continue
            fill_sign = Decimal(1) if fill.side == "buy" else Decimal(-1)
            same_direction = abs(signed) <= EPSILON or (signed > 0 and fill_sign > 0) or (
                signed < 0 and fill_sign < 0
            )

            if same_direction:
                if fill.reduce_only:
                    errors.append(
                        f"reduce-only fill would increase/reverse {symbol}:{fill.exchange_trade_id}"
                    )
                    continue
                if current is None or current.open_qty <= EPSILON:
                    sequence += 1
                    current = TradeCycle(
                        sequence=sequence,
                        symbol=symbol,
                        direction="long" if fill_sign > 0 else "short",
                        ownership=fill.ownership,
                        status="open",
                        opened_at_ms=fill.ts_ms,
                        entry_liquidity=fill.liquidity,
                        classification_reason=f"opened by {fill.ownership} order",
                        _owners={fill.ownership},
                    )
                    cycles.append(current)
                prior_notional = current.entry_notional
                current.qty_opened += fill.qty
                current.entry_notional += fill.qty * fill.price
                current.entry_price = (
                    current.entry_notional / current.qty_opened
                    if current.qty_opened > EPSILON else Decimal(0)
                )
                current.entry_fee += fill.fee
                current.entry_liquidity = current.entry_liquidity or fill.liquidity
                current.add_owner(fill.ownership)
                allocate(current, fill, "ENTRY", fill.qty, fill.fee)
                signed += fill_sign * fill.qty
                if abs(signed) <= EPSILON:
                    signed = Decimal(0)
                continue

            if current is None or abs(signed) <= EPSILON:
                errors.append(f"missing open cycle for closing fill {symbol}:{fill.exchange_trade_id}")
                continue

            open_qty = abs(signed)
            close_qty = min(open_qty, fill.qty)
            close_fee = fill.fee * close_qty / fill.qty
            # Binance realizedPnl belongs entirely to the closing part of a reversal.
            close_pnl = fill.realized_pnl
            prior_closed = current.qty_closed
            current.qty_closed += close_qty
            current.exit_notional += close_qty * fill.price
            current.exit_price = (
                current.exit_notional / current.qty_closed
                if current.qty_closed > EPSILON else Decimal(0)
            )
            current.exit_fee += close_fee
            current.realized_pnl += close_pnl
            current.exit_liquidity = fill.liquidity or current.exit_liquidity
            current.add_owner(fill.ownership)
            current.add_exit_reason(fill.exit_reason)
            allocate(current, fill, "EXIT", close_qty, close_fee, close_pnl)
            signed += fill_sign * close_qty
            if abs(signed) <= EPSILON:
                signed = Decimal(0)

            if current.open_qty <= EPSILON:
                current.qty_closed = current.qty_opened
                current.status = "closed"
                current.closed_at_ms = fill.ts_ms
            else:
                current.status = "partial"

            reversal_qty = fill.qty - close_qty
            if reversal_qty > EPSILON:
                if fill.reduce_only:
                    errors.append(
                        f"reduce-only fill exceeds open position {symbol}:{fill.exchange_trade_id}"
                    )
                    continue
                reversal_fee = fill.fee - close_fee
                sequence += 1
                current = TradeCycle(
                    sequence=sequence,
                    symbol=symbol,
                    direction="long" if fill_sign > 0 else "short",
                    ownership=fill.ownership,
                    status="open",
                    opened_at_ms=fill.ts_ms,
                    entry_price=fill.price,
                    qty_opened=reversal_qty,
                    entry_notional=reversal_qty * fill.price,
                    entry_fee=reversal_fee,
                    entry_liquidity=fill.liquidity,
                    classification_reason="reversal remainder",
                    _owners={fill.ownership},
                )
                cycles.append(current)
                allocate(current, fill, "REVERSAL_ENTRY", reversal_qty, reversal_fee)
                signed += fill_sign * reversal_qty
                if abs(signed) <= EPSILON:
                    signed = Decimal(0)
            elif current.status == "closed":
                current = None

        final_positions[symbol] = signed

    return ReplayResult(
        cycles=cycles,
        final_positions=final_positions,
        allocation_qty=allocation_qty,
        allocation_fee=allocation_fee,
        allocation_pnl=allocation_pnl,
        errors=errors,
    )


def validate_replay(fills: Iterable[CanonicalFill], result: ReplayResult) -> list[str]:
    """Return conservation/invariant failures; an empty list is safe to persist."""
    errors = list(result.errors)
    for fill in fills:
        qty = result.allocation_qty.get(fill.exchange_fill_id, Decimal(0))
        fee = result.allocation_fee.get(fill.exchange_fill_id, Decimal(0))
        pnl = result.allocation_pnl.get(fill.exchange_fill_id, Decimal(0))
        if abs(qty - fill.qty) > EPSILON:
            errors.append(
                f"quantity not conserved {fill.symbol}:{fill.exchange_trade_id} "
                f"{qty}!={fill.qty}"
            )
        if abs(fee - fill.fee) > EPSILON:
            errors.append(
                f"fee not conserved {fill.symbol}:{fill.exchange_trade_id} {fee}!={fill.fee}"
            )
        if abs(pnl - fill.realized_pnl) > EPSILON:
            errors.append(
                f"realized pnl not conserved {fill.symbol}:{fill.exchange_trade_id} "
                f"{pnl}!={fill.realized_pnl}"
            )
    for cycle in result.cycles:
        if cycle.qty_closed - cycle.qty_opened > EPSILON:
            errors.append(f"cycle {cycle.sequence} closed more than opened")
    return errors
