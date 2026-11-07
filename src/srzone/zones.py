# Licensed under GNU GPL v3.0, same as this project.
"""Adapt the existing V3 detector to geometry-only zones.

The call matches ``SREngine.detect`` distance and inside-price filters
(``src/sr_engine.py``). Probability models and ``score_calibration.json``
are not consulted. Those files are A-share research artifacts and are not
evidence for a crypto fill.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd

from src.sr_engine import MAX_DIST_ATR, MIN_DIST_ATR, SREngine

from .models import Bar, Zone

_ENGINE = None


def _engine() -> SREngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SREngine(n_zones=6)
    return _ENGINE


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime([b.open_time for b in bars], unit="ms", utc=True),
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )


def zones_from_v3(bars: Sequence[Bar], symbol: str = "CRYPTO") -> list[Zone]:
    """Zones visible at the last bar of ``bars``. Pass a closed prefix only."""

    if len(bars) < 60:
        return []
    df = bars_to_frame(bars)
    eng = _engine()
    ctx = SREngine.build_ctx(df, symbol)
    if not (ctx.atr and ctx.atr > 0):
        return []
    levels = eng.det.detect(ctx)
    price = ctx.price
    atr = ctx.atr
    kept = {"support": [], "resistance": []}
    for level in levels:
        if level.kind == "support" and level.high >= price:
            continue
        if level.kind == "resistance" and level.low <= price:
            continue
        dist = abs(level.center - price) / atr
        if dist < MIN_DIST_ATR or dist > MAX_DIST_ATR:
            continue
        kept[level.kind].append(level)
    out: list[Zone] = []
    for kind in ("support", "resistance"):
        ranked = sorted(kept[kind], key=lambda z: -z.score)[:3]
        for level in ranked:
            out.append(Zone(kind, float(level.low), float(level.high), float(level.center)))
    return out
