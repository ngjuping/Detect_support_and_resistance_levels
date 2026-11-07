# Licensed under GNU GPL v3.0, same as this project.
"""Causal entry rules for srzone-v1.

New design, not the upstream labeling rule.

The upstream research label treated an intrabar high/low tag as a touch and
then booked the fill at that same bar's close. That mixed two prices from the
signal bar. srzone-v1 does not.

On a closed bar the high, low, and close are all known. They may confirm that
a support bounce or a resistance rejection happened. The order is sent for the
next bar's open only. The signal bar is never the fill bar. The entry bar's
high and low are not used to decide the entry price.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from .config import StrategyConfig
from .filters import round_stop
from .models import Bar, Decision, Signal, Zone


def bar_is_usable(bar: Bar) -> bool:
    vals = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in vals):
        return False
    if bar.low <= 0 or bar.high < bar.low:
        return False
    if bar.open < bar.low or bar.open > bar.high:
        return False
    if bar.close < bar.low or bar.close > bar.high:
        return False
    if bar.volume < 0 or bar.close_time < bar.open_time:
        return False
    return True


def _zone_ok(zone: Zone) -> bool:
    if zone.kind not in ("support", "resistance"):
        return False
    if not all(math.isfinite(v) for v in (zone.low, zone.high, zone.center)):
        return False
    return zone.low < zone.high and zone.low > 0


def signal_from_closed_bar(
    prev: Bar,
    bar: Bar,
    zones: Sequence[Zone],
    atr: float,
    cfg: StrategyConfig,
    tick_size: str,
) -> Decision:
    """Confirm a setup on ``bar`` only. Does not look at any later bar."""

    if not bar_is_usable(prev) or not bar_is_usable(bar):
        return Decision(reason="bad_bar")
    if not math.isfinite(atr) or atr <= 0:
        return Decision(reason="bad_atr")
    if cfg.mode not in ("long", "short", "both"):
        return Decision(reason="bad_mode")

    longs: list[Signal] = []
    shorts: list[Signal] = []
    for zone in zones:
        if not _zone_ok(zone):
            continue
        if cfg.mode in ("long", "both") and zone.kind == "support":
            sig = _long_signal(prev, bar, zone, atr, cfg, tick_size)
            if sig is not None:
                longs.append(sig)
        if cfg.mode in ("short", "both") and zone.kind == "resistance":
            sig = _short_signal(prev, bar, zone, atr, cfg, tick_size)
            if sig is not None:
                shorts.append(sig)

    if longs and shorts:
        # Same closed bar offered both a long and a short. Take neither.
        return Decision(reason="same_bar_both_sides")
    if longs:
        best = max(longs, key=lambda s: s.zone_high)
        return Decision(signal=best, reason="long")
    if shorts:
        best = min(shorts, key=lambda s: s.zone_low)
        return Decision(signal=best, reason="short")
    return Decision(reason="no_setup")


def _long_signal(prev, bar, zone, atr, cfg, tick_size) -> Optional[Signal]:
    """Support bounce: came from above, tagged the zone, closed back above it."""

    if not (prev.close > zone.high and bar.low <= zone.high and bar.close > zone.high):
        return None
    stop = round_stop("long", zone.low - cfg.sl_atr_buffer * atr, tick_size)
    if stop <= 0 or stop >= bar.close:
        return None
    # The closed bar already traded through the stop. Do not arm an entry.
    if bar.low <= stop:
        return None
    return Signal(
        side="long",
        zone_key=zone.key,
        zone_low=zone.low,
        zone_high=zone.high,
        stop=stop,
        atr=atr,
        signal_open_time=bar.open_time,
    )


def _short_signal(prev, bar, zone, atr, cfg, tick_size) -> Optional[Signal]:
    """Resistance rejection: came from below, tagged the zone, closed back under it."""

    if not (prev.close < zone.low and bar.high >= zone.low and bar.close < zone.low):
        return None
    stop = round_stop("short", zone.high + cfg.sl_atr_buffer * atr, tick_size)
    if stop <= bar.close:
        return None
    if bar.high >= stop:
        return None
    return Signal(
        side="short",
        zone_key=zone.key,
        zone_low=zone.low,
        zone_high=zone.high,
        stop=stop,
        atr=atr,
        signal_open_time=bar.open_time,
    )


def entry_allowed_at_open(side: str, open_price: float, stop: float) -> bool:
    """The next open is the order event. Decline if that open is already through the stop.

    This uses the open print only. It does not use that bar's high or low.
    """

    if not math.isfinite(open_price) or open_price <= 0:
        return False
    if side == "long":
        return open_price > stop
    if side == "short":
        return open_price < stop
    return False
