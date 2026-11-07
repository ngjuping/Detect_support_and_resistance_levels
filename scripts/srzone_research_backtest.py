#!/usr/bin/env python3
# Licensed under GNU GPL v3.0, same as this project.
"""Frozen srzone-v1 research backtest on public Binance Vision files.

Not s2bt. Not the upstream author's strategy. Not a live trading run.
Parameters below were chosen before the result was computed and are not searched.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.srzone.backtest import run_backtest
from src.srzone.config import paper_research_config
from src.srzone.models import ContractFilters
from src.srzone.public_data import (
    download_verified,
    funding_holes,
    funding_url,
    kline_url,
    parse_funding_zip,
    parse_kline_zip,
)
from src.srzone.zones import zones_from_v3

# Frozen before any look at the result. ETHUSDT is a public-data sample only.
SYMBOL = "ETHUSDT"
INTERVAL = "1h"
CACHE = ROOT / "research_cache"
OUT = ROOT / "research_out" / "srzone_v1_ethusdt_1h_2024H1.json"

# tick/step/min qty/min notional were NOT read from fapi (that host returned 451).
RESEARCH_FILTERS = ContractFilters(
    symbol=SYMBOL,
    tick_size="0.01",
    step_size="0.001",
    min_qty="0.001",
    max_qty="10000",
    min_notional="20",
    market_step_size="0.001",
    market_min_qty="0.001",
    source="NOT_LIVE_EXCHANGE_INFO",
)

WINDOWS = (
    {
        "name": "baseline",
        "trade_start": "2024-01-01",
        "trade_end": "2024-04-01",
        "months": ((2023, 11), (2023, 12), (2024, 1), (2024, 2), (2024, 3)),
    },
    {
        "name": "holdout",
        "trade_start": "2024-04-01",
        "trade_end": "2024-07-01",
        "months": ((2024, 2), (2024, 3), (2024, 4), (2024, 5), (2024, 6)),
    },
)


def main() -> int:
    probe = probe_fapi()
    files = []
    bars_by_month = {}
    funding_by_month = {}
    for year, month in sorted({m for w in WINDOWS for m in w["months"]}):
        k_url = kline_url(SYMBOL, INTERVAL, year, month)
        f_url = funding_url(SYMBOL, year, month)
        k_bytes = download_verified(k_url, CACHE)
        f_bytes = download_verified(f_url, CACHE)
        bars_by_month[(year, month)] = parse_kline_zip(k_bytes)
        funding_by_month[(year, month)] = parse_funding_zip(f_bytes)
        files.append({"klines": k_url, "funding": f_url, "verified_sha256": True})

    windows = []
    for spec in WINDOWS:
        bars = []
        seen = set()
        for key in spec["months"]:
            for bar in bars_by_month[key]:
                if bar.open_time in seen:
                    continue
                seen.add(bar.open_time)
                bars.append(bar)
        bars.sort(key=lambda b: b.open_time)
        funding = []
        for key in spec["months"]:
            funding.extend(funding_by_month[key])
        funding = sorted(set(funding))
        holes = funding_holes(funding)
        start_ms = _ms(spec["trade_start"])
        end_ms = _ms(spec["trade_end"])
        cfg = paper_research_config(
            symbol=SYMBOL,
            trade_start_ms=start_ms,
            trade_end_ms=end_ms,
        )
        zone_calls = {"n": 0, "max_seen": None}

        def zone_fn(window, _counter=zone_calls):
            _counter["n"] += 1
            _counter["max_seen"] = window[-1].open_time
            return zones_from_v3(window, symbol=SYMBOL)

        report = run_backtest(
            bars,
            cfg,
            RESEARCH_FILTERS,
            zone_fn,
            funding_rates=dict(funding),
            funding_times={t for t, _ in funding},
        )
        report["window"] = spec["name"]
        report["trade_start"] = spec["trade_start"]
        report["trade_end"] = spec["trade_end"]
        report["first_bar"] = bars[0].open_time if bars else None
        report["last_bar"] = bars[-1].open_time if bars else None
        report["funding_points"] = len(funding)
        report["funding_holes"] = holes
        report["funding_status"] = "applied" if not holes and report["funding_gaps"] == 0 else "incomplete"
        report["zone_calls"] = zone_calls["n"]
        exit_leg = sum(t["pnl"] for t in report["trades"])
        report["pnl_note"] = (
            "realized_pnl is cash: entry fees, exit-leg pnl, and funding. "
            "Each trade pnl is the exit leg only (price result minus that fill's fee). "
            "ending_equity_mark also includes the open position, with an estimated exit fee reserved."
        )
        report["exit_leg_pnl_sum"] = exit_leg
        report["realized_minus_exit_legs"] = report["realized_pnl"] - exit_leg
        windows.append(report)
        print(
            spec["name"],
            "trades", report["n_trades"],
            "realized", round(report["realized_pnl"], 4),
            "mark_equity", None if report["ending_equity_mark"] is None else round(report["ending_equity_mark"], 4),
            "halt", report["risk_halt"],
            "funding", report["funding_status"],
        )

    payload = {
        "design_id": "srzone-v1",
        "label": "new research backtest; not s2bt; not the upstream author strategy; not a live result",
        "ashare_calibration_used": False,
        "parameter_search": False,
        "symbol_note": "ETHUSDT is the public USD-M sample, not a live symbol decision",
        "fapi_exchangeInfo": probe,
        "filters": {
            "source": RESEARCH_FILTERS.source,
            "tick_size": RESEARCH_FILTERS.tick_size,
            "step_size": RESEARCH_FILTERS.step_size,
            "min_qty": RESEARCH_FILTERS.min_qty,
            "min_notional": RESEARCH_FILTERS.min_notional,
            "note": "Assumed so the sizer can run. Not fetched from the exchange.",
        },
        "cost_assumptions": {
            "fee_rate_per_side": 0.0005,
            "slippage_rate": 0.0001,
            "note": "Not read from commissionRate. Funding uses the Vision rate and the bar open as mark.",
        },
        "files": files,
        "windows": windows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=_json), encoding="utf-8")
    print("wrote", OUT)
    return 0


def probe_fapi() -> dict:
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo?symbol=ETHUSDT"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read(400).decode("utf-8", errors="replace")
            return {"url": url, "status": getattr(resp, "status", None), "body": body}
    except urllib.error.HTTPError as exc:
        body = exc.read(400).decode("utf-8", errors="replace")
        return {"url": url, "status": exc.code, "body": body}
    except Exception as exc:
        return {"url": url, "status": None, "error": type(exc).__name__, "detail": str(exc)[:400]}


def _ms(day: str) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _json(value):
    if isinstance(value, float):
        return value
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
