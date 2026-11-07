# Licensed under GNU GPL v3.0, same as this project.
"""Bar research runner for srzone-v1.

This is a new research backtest. It is not the upstream walk-forward labeler
and it is not the s2bt project. It does not tune parameters.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .broker import PaperBroker
from .config import StrategyConfig
from .models import Bar, ContractFilters
from .supervisor import Supervisor, ZoneFn


def run_backtest(
    bars: Sequence[Bar],
    cfg: StrategyConfig,
    filters: ContractFilters,
    zone_fn: ZoneFn,
    funding_rates: Optional[dict[int, float]] = None,
    funding_times: Optional[set[int]] = None,
    atr_fn=None,
) -> dict:
    broker = PaperBroker(cfg)
    supervisor = Supervisor(cfg, broker, filters, zone_fn, atr_fn=atr_fn)
    funding_rates = funding_rates or {}
    funding_times = funding_times or set()
    curve = []
    for i in range(len(bars)):
        bar = bars[i]
        expected = bar.open_time in funding_times
        rate = funding_rates.get(bar.open_time) if expected else None
        supervisor.on_closed_bar(bars[: i + 1], funding_rate=rate, funding_expected=expected)
        curve.append({"open_time": bar.open_time, "equity": supervisor.equity(bar.close)})
    open_pos = supervisor.ledger.position
    mark = bars[-1].close if bars else None
    return {
        "design_id": cfg.design_id,
        "design_note": cfg.design_note,
        "not_s2bt": True,
        "not_original_author_strategy": True,
        "ashare_calibration_used": False,
        "execution": cfg.execution,
        "symbol": cfg.symbol,
        "params": cfg.to_public_dict(),
        "filters_source": filters.source,
        "n_bars": len(bars),
        "n_trades": len(supervisor.ledger.trades),
        "trades": list(supervisor.ledger.trades),
        "realized_pnl": supervisor.ledger.realized_pnl,
        "ending_equity_mark": supervisor.equity(mark) if mark is not None else None,
        "open_position": open_pos,
        "risk_halt": supervisor.ledger.risk_halt,
        "risk_halt_reason": supervisor.ledger.risk_halt_reason,
        "entry_halt": supervisor.ledger.entry_halt,
        "entry_halt_reason": supervisor.ledger.entry_halt_reason,
        "funding_gaps": supervisor.ledger.funding_gaps,
        "notes": list(supervisor.ledger.notes),
        "equity_curve_last": curve[-1] if curve else None,
        "max_drawdown": _max_drawdown([row["equity"] for row in curve]),
    }


def _max_drawdown(equity: list[float]) -> Optional[float]:
    if not equity:
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst
