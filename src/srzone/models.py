# Licensed under GNU GPL v3.0, same as this project.
"""Shared records for the new srzone-v1 execution layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Bar:
    """One closed OHLCV bar. ``open_time`` is Unix milliseconds."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class Zone:
    """Geometry only. No probability, hold rate, or A-share bucket."""

    kind: str  # support | resistance
    low: float
    high: float
    center: float

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.low:.8f}:{self.high:.8f}"


@dataclass(frozen=True)
class Signal:
    side: str  # long | short
    zone_key: str
    zone_low: float
    zone_high: float
    stop: float
    atr: float
    signal_open_time: int


@dataclass(frozen=True)
class ContractFilters:
    """USD-M symbol filters.

    ``source`` must be ``exchangeInfo`` before a live session can start.
    Research runs label any stand-in source explicitly.
    """

    symbol: str
    tick_size: str
    step_size: str
    min_qty: str
    max_qty: str
    min_notional: str
    source: str
    market_step_size: Optional[str] = None
    market_min_qty: Optional[str] = None
    contract_type: str = "PERPETUAL"
    quote_asset: str = "USDT"
    margin_asset: str = "USDT"
    status: str = "TRADING"

    def market_step(self) -> str:
        return self.market_step_size or self.step_size

    def market_min(self) -> str:
        return self.market_min_qty or self.min_qty


@dataclass
class Order:
    client_id: str
    symbol: str
    purpose: str  # entry | sl | tp | flatten
    type: str  # MARKET | STOP_MARKET | TAKE_PROFIT_MARKET
    side: str  # BUY | SELL
    qty: float
    reduce_only: bool
    status: str = "staged"  # staged | open | filled | canceled | rejected | unknown
    trigger_price: Optional[float] = None
    activate_open_time: Optional[int] = None
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    owned: bool = True


@dataclass
class FillEvent:
    kind: str
    client_id: str
    purpose: str
    order_side: str
    qty: float
    price: float
    fee: float
    pnl: float
    open_time: int
    position_side: str = ""


@dataclass
class SubmitResult:
    state: str  # acked | rejected | unknown
    order: Optional[Order] = None
    error: str = ""


@dataclass
class Decision:
    signal: Optional[Signal] = None
    reason: str = ""
    notes: list = field(default_factory=list)
