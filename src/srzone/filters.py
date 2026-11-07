# Licensed under GNU GPL v3.0, same as this project.
"""Parse USD-M ``GET /fapi/v1/exchangeInfo`` filters and round orders.

Field names follow Binance USD-M exchangeInfo: PRICE_FILTER, LOT_SIZE,
MARKET_LOT_SIZE, MIN_NOTIONAL / NOTIONAL. Parsing is tested with a fixture.
This module does not call the network.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional

from .models import ContractFilters


class FilterReject(ValueError):
    pass


def _filt(raw_filters: list, name: str) -> dict:
    for item in raw_filters:
        if item.get("filterType") == name:
            return item
    return {}


def parse_exchange_info(payload: dict, symbol: str, source: str = "exchangeInfo") -> ContractFilters:
    symbols = payload.get("symbols") or []
    match = next((s for s in symbols if s.get("symbol") == symbol), None)
    if match is None:
        raise FilterReject(f"symbol {symbol} missing from exchangeInfo")
    filters = match.get("filters") or []
    price = _filt(filters, "PRICE_FILTER")
    lot = _filt(filters, "LOT_SIZE")
    market = _filt(filters, "MARKET_LOT_SIZE")
    notional = _filt(filters, "MIN_NOTIONAL") or _filt(filters, "NOTIONAL")
    tick = price.get("tickSize")
    step = lot.get("stepSize")
    min_qty = lot.get("minQty")
    max_qty = lot.get("maxQty")
    min_notional = notional.get("notional", notional.get("minNotional"))
    if not all([tick, step, min_qty, max_qty, min_notional]):
        raise FilterReject("exchangeInfo missing tick, step, qty, or min notional")
    return ContractFilters(
        symbol=symbol,
        tick_size=str(tick),
        step_size=str(step),
        min_qty=str(min_qty),
        max_qty=str(max_qty),
        min_notional=str(min_notional),
        source=source,
        market_step_size=str(market["stepSize"]) if market.get("stepSize") else None,
        market_min_qty=str(market["minQty"]) if market.get("minQty") else None,
        contract_type=str(match.get("contractType") or ""),
        quote_asset=str(match.get("quoteAsset") or ""),
        margin_asset=str(match.get("marginAsset") or ""),
        status=str(match.get("status") or ""),
    )


def _q(value: float, step: str, rounding) -> Decimal:
    s = Decimal(step)
    if s <= 0:
        raise FilterReject("step must be positive")
    n = (Decimal(str(value)) / s).to_integral_value(rounding=rounding)
    return n * s


def floor_to_step(value: float, step: str) -> float:
    return float(_q(value, step, ROUND_FLOOR))


def ceil_to_step(value: float, step: str) -> float:
    return float(_q(value, step, ROUND_CEILING))


def round_stop(side: str, stop: float, tick: str) -> float:
    """Move the stop slightly farther from the entry so the trigger stays valid."""

    if side == "long":
        return floor_to_step(stop, tick)
    if side == "short":
        return ceil_to_step(stop, tick)
    raise FilterReject(side)


def round_target(side: str, target: float, tick: str) -> float:
    """Move the target slightly closer so it is not improved by rounding."""

    if side == "long":
        return floor_to_step(target, tick)
    if side == "short":
        return ceil_to_step(target, tick)
    raise FilterReject(side)


def size_qty(
    *,
    side: str,
    equity: float,
    decision_price: float,
    stop: float,
    risk_per_trade_frac: float,
    leverage: float,
    max_total_notional: float,
    filters: ContractFilters,
) -> tuple[Optional[float], str]:
    """Risk-budget size, then caps and exchange steps. Returns (qty, reason)."""

    if equity <= 0 or decision_price <= 0:
        return None, "bad_price"
    dist = (decision_price - stop) if side == "long" else (stop - decision_price)
    if dist <= 0:
        return None, "stop_on_wrong_side"
    risk_usdt = equity * risk_per_trade_frac
    if risk_usdt <= 0:
        return None, "no_risk_budget"
    raw_qty = risk_usdt / dist
    notional_cap = min(max_total_notional, equity * leverage)
    if notional_cap <= 0:
        return None, "notional_cap"
    raw_qty = min(raw_qty, notional_cap / decision_price)
    step = filters.market_step()
    qty = floor_to_step(raw_qty, step)
    max_qty = floor_to_step(float(Decimal(filters.max_qty)), step)
    if qty > max_qty:
        qty = max_qty
    if qty <= 0 or Decimal(str(qty)) < Decimal(filters.market_min()):
        return None, "below_min_qty"
    if Decimal(str(qty)) < Decimal(filters.min_qty):
        return None, "below_min_qty"
    notional = Decimal(str(qty)) * Decimal(str(decision_price))
    if notional < Decimal(filters.min_notional):
        return None, "below_min_notional"
    return qty, "ok"
