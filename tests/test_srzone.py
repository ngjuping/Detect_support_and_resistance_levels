# Licensed under GNU GPL v3.0, same as this project.
"""Behavior tests for the new srzone-v1 paper path and the gated live adapter."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.srzone.binance_live import (
    FORBIDDEN_PATHS,
    AccountWideCancelForbidden,
    BinanceUsdmLiveBroker,
    client_id,
)
from src.srzone.broker import PaperBroker
from src.srzone.config import (
    LIVE_CONFIRM_PHRASE,
    paper_research_config,
    validate_live,
    validate_paper,
)
from src.srzone.backtest import run_backtest
from src.srzone.filters import parse_exchange_info, size_qty
from src.srzone.models import Bar, ContractFilters, Order, Zone
from src.srzone.rules import signal_from_closed_bar
from src.srzone.supervisor import Supervisor


INTERVAL = 1000
ROOT = Path(__file__).resolve().parents[1]


def bar(i, o, h, l, c, vol=1.0):
    t = i * INTERVAL
    return Bar(t, float(o), float(h), float(l), float(c), float(vol), t + INTERVAL - 1)


def filters(**kwargs):
    data = dict(
        symbol="ETHUSDT",
        tick_size="0.01",
        step_size="0.001",
        min_qty="0.001",
        max_qty="10000",
        min_notional="5",
        source="unit-test",
    )
    data.update(kwargs)
    return ContractFilters(**data)


def cfg(**kwargs):
    base = dict(
        symbol="ETHUSDT",
        interval_ms=INTERVAL,
        min_bars=2,
        causal_lookback_bars=50,
        atr_period=2,
        sl_atr_buffer=0.25,
        reward_risk=1.5,
        max_hold_bars=1,
        cooldown_bars=0,
        fee_rate_per_side=0.0,
        slippage_rate=0.0,
        capital_usdt=11_000.0,
        leverage=1.0,
        max_total_notional_usdt=100_000.0,
        risk_per_trade_frac=0.001,
    )
    base.update(kwargs)
    return paper_research_config(**base)


def support_zones(_window):
    return [Zone("support", 90.0, 95.0, 92.5)]


def both_zones(_window):
    return [
        Zone("support", 90.0, 95.0, 92.5),
        Zone("resistance", 105.0, 110.0, 107.5),
    ]


def resistance_zones(_window):
    return [Zone("resistance", 105.0, 110.0, 107.5)]


class RulesTests(unittest.TestCase):
    def test_long_uses_closed_bar_and_not_a_same_bar_fill(self):
        prev = bar(0, 100, 101, 99, 100)
        current = bar(1, 101, 102, 94, 100)
        decision = signal_from_closed_bar(prev, current, support_zones([]), 4.0, cfg(), "0.01")
        self.assertEqual(decision.reason, "long")
        self.assertEqual(decision.signal.stop, 89.0)
        self.assertEqual(decision.signal.signal_open_time, current.open_time)
        self.assertNotEqual(decision.signal.signal_open_time, current.open_time + INTERVAL)

    def test_same_bar_both_sides_skipped(self):
        prev = bar(0, 100, 101, 99, 100)
        current = bar(1, 100, 106, 94, 100)
        decision = signal_from_closed_bar(prev, current, both_zones([]), 4.0, cfg(), "0.01")
        self.assertIsNone(decision.signal)
        self.assertEqual(decision.reason, "same_bar_both_sides")

    def test_nan_and_stop_already_traded_are_not_signals(self):
        prev = bar(0, 100, 101, 99, 100)
        bad = Bar(INTERVAL, 101, 102, 94, float("nan"), 1.0, INTERVAL + 10)
        decision = signal_from_closed_bar(prev, bad, support_zones([]), 4.0, cfg(), "0.01")
        self.assertEqual(decision.reason, "bad_bar")
        pierced = bar(1, 101, 102, 80, 100)
        pierced_decision = signal_from_closed_bar(prev, pierced, support_zones([]), 4.0, cfg(), "0.01")
        self.assertIsNone(pierced_decision.signal)

    def test_short_rejection(self):
        prev = bar(0, 100, 101, 99, 100)
        current = bar(1, 100, 106, 99, 100)
        decision = signal_from_closed_bar(prev, current, resistance_zones([]), 4.0, cfg(mode="short"), "0.01")
        self.assertEqual(decision.reason, "short")
        self.assertEqual(decision.signal.stop, 111.0)


class ExecutionTests(unittest.TestCase):
    def test_entry_is_next_open_with_fees_and_slippage(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),  # signal, low is not the fill
            bar(2, 101, 103, 99, 102),
            bar(3, 110, 111, 108, 110),
        ]
        report = run_backtest(
            bars, cfg(fee_rate_per_side=0.0005, slippage_rate=0.0001),
            filters(), support_zones, atr_fn=lambda _w: 4.0,
        )
        self.assertEqual(report["n_trades"], 1)
        trade = report["trades"][0]
        entry = 101 * 1.0001
        exit_px = 110 * 0.9999
        self.assertAlmostEqual(trade["entry"], entry, places=8)
        self.assertAlmostEqual(trade["exit"], exit_px, places=8)
        self.assertNotAlmostEqual(trade["entry"], 94.0)
        self.assertEqual(trade["signal_open_time"], bars[1].open_time)
        self.assertEqual(trade["entry_open_time"], bars[2].open_time)
        gross = exit_px - entry
        fees = 0.0005 * entry + 0.0005 * exit_px
        self.assertAlmostEqual(report["realized_pnl"], gross - fees, places=8)
        self.assertGreater(trade["entry_open_time"], trade["signal_open_time"])

    def test_short_pnl(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 100, 106, 99, 100),
            bar(2, 100, 101, 99, 100),
            bar(3, 90, 91, 89, 90),
        ]
        report = run_backtest(
            bars, cfg(mode="short", fee_rate_per_side=0.0, slippage_rate=0.0001, risk_per_trade_frac=0.001),
            filters(), resistance_zones, atr_fn=lambda _w: 4.0,
        )
        # dist = 111 - 100 = 11, risk = 11, qty = 1
        self.assertEqual(report["n_trades"], 1)
        trade = report["trades"][0]
        entry = 100 * 0.9999
        exit_px = 90 * 1.0001
        self.assertAlmostEqual(trade["entry"], entry, places=8)
        self.assertAlmostEqual(trade["exit"], exit_px, places=8)
        self.assertAlmostEqual(report["realized_pnl"], entry - exit_px, places=8)
        self.assertEqual(trade["side"], "short")

    def test_same_bar_stop_and_target_uses_stop(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 100, 101, 96, 100),
            bar(3, 100, 200, 50, 100),
        ]
        report = run_backtest(
            bars, cfg(max_hold_bars=10, slippage_rate=0.0, fee_rate_per_side=0.0),
            filters(), support_zones, atr_fn=lambda _w: 4.0,
        )
        self.assertEqual(report["n_trades"], 1)
        self.assertEqual(report["trades"][0]["reason"], "exit_sl")
        self.assertAlmostEqual(report["trades"][0]["exit"], 89.0, places=8)

    def test_duplicate_bar_and_gap_and_short_history(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
        ]
        sup_cfg = cfg()
        broker = PaperBroker(sup_cfg)
        sup = Supervisor(sup_cfg, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        sup.on_closed_bar(bars)
        staged = broker.place_count
        again = sup.on_closed_bar(bars)
        self.assertTrue(again["duplicate"])
        self.assertEqual(broker.place_count, staged)

        gapped = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            Bar(3000, 101, 102, 99, 101, 1.0, 3999),
        ]
        report = run_backtest(gapped, cfg(), filters(), support_zones, atr_fn=lambda _w: 4.0)
        self.assertEqual(report["n_trades"], 0)
        self.assertIn("gap_or_missed_open", report["notes"])

        short = run_backtest(bars, cfg(min_bars=50), filters(), support_zones, atr_fn=lambda _w: 4.0)
        self.assertEqual(short["n_trades"], 0)

    def test_nan_bar_does_not_order(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            Bar(INTERVAL, 101, 102, 94, float("nan"), 1.0, 2 * INTERVAL - 1),
            bar(2, 101, 102, 100, 101),
        ]
        report = run_backtest(bars, cfg(), filters(), support_zones, atr_fn=lambda _w: 4.0)
        self.assertEqual(report["n_trades"], 0)
        self.assertFalse(report["entry_halt"])

    def test_one_position_and_prefix_window(self):
        seen = []

        def zones(window):
            seen.append(window[-1].open_time)
            self.assertLessEqual(len(window), 50)
            self.assertEqual(window[-1].open_time, max(b.open_time for b in window))
            return support_zones(window)

        bars = [bar(i, 100, 102, 94, 100) for i in range(6)]
        bars[0] = bar(0, 100, 101, 99, 100)
        report = run_backtest(
            bars, cfg(max_hold_bars=10, cooldown_bars=3), filters(), zones, atr_fn=lambda _w: 4.0,
        )
        self.assertLessEqual(report["n_trades"], 1)
        self.assertEqual(broker_positions(report), report["n_trades"])
        self.assertEqual(seen, sorted(seen))
        # A second, longer run must not change trades that already closed inside the prefix.
        longer = bars + [bar(i, 100, 102, 94, 100) for i in range(6, 10)]
        full = run_backtest(
            longer, cfg(max_hold_bars=1, cooldown_bars=0), filters(), support_zones, atr_fn=lambda _w: 4.0,
        )
        prefix = run_backtest(
            bars, cfg(max_hold_bars=1, cooldown_bars=0), filters(), support_zones, atr_fn=lambda _w: 4.0,
        )
        prefix_exit = bars[-1].open_time
        full_done = [t for t in full["trades"] if t["exit_open_time"] <= prefix_exit]
        self.assertEqual(full_done, prefix["trades"])

    def test_protection_failure_flattens_and_halts_entries(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
            bar(3, 101, 102, 94, 100),
            bar(4, 101, 103, 99, 102),
        ]
        conf = cfg(max_hold_bars=10, cooldown_bars=0)
        broker = PaperBroker(conf)
        broker.reject_purposes.add("sl")
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(len(bars)):
            sup.on_closed_bar(bars[: i + 1])
        self.assertEqual(broker.our_qty, 0.0)
        self.assertTrue(sup.ledger.entry_halt)
        self.assertEqual(len(sup.ledger.trades), 1)
        self.assertEqual(sup.ledger.trades[0]["reason"], "exit_flatten")

    def test_protection_and_flatten_failure_does_not_invent_a_stop(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
            bar(3, 100, 200, 1, 100),
        ]
        conf = cfg(max_hold_bars=10)
        broker = PaperBroker(conf)
        broker.reject_purposes.update({"sl", "flatten"})
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(len(bars)):
            sup.on_closed_bar(bars[: i + 1])
        self.assertNotEqual(broker.our_qty, 0.0)
        self.assertTrue(sup.ledger.risk_halt)
        self.assertEqual(sup.ledger.trades, [])
        self.assertIsNone(broker._resting("sl"))

    def test_partial_fill_sizes_protection_to_the_fill(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 102, 99, 101),
        ]
        conf = cfg(max_hold_bars=10)
        broker = PaperBroker(conf)
        broker.partial_ratio = 0.4
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(len(bars)):
            sup.on_closed_bar(bars[: i + 1])
        self.assertAlmostEqual(broker.our_qty, 0.4, places=8)
        sl = broker._resting("sl")
        self.assertIsNotNone(sl)
        self.assertAlmostEqual(sl.qty, 0.4, places=8)
        self.assertTrue(sl.reduce_only)

    def test_restart_adopts_unknown_order_without_a_second_submit(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
        ]
        conf = cfg(max_hold_bars=10)
        broker = PaperBroker(conf)
        broker.unknown_purposes.add("entry")
        broker.store_unknown = True
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        sup.on_closed_bar(bars[:1])
        sup.on_closed_bar(bars[:2])
        self.assertEqual(sup.ledger.pending_entry["state"], "submit_unknown")
        placed = broker.place_count
        path = Path("/tmp/srzone-ledger.json")
        sup.ledger.save(path)
        revived = Supervisor(
            conf, broker, filters(), support_zones,
            ledger=type(sup.ledger).load(path), atr_fn=lambda _w: 4.0,
        )
        revived.reconcile()
        self.assertEqual(broker.place_count, placed)
        self.assertEqual(revived.ledger.pending_entry["state"], "staged")
        revived.on_closed_bar(bars[:3])
        self.assertAlmostEqual(broker.our_qty, 1.0, places=8)
        self.assertEqual(broker.place_count, placed + 2)  # stop and target, not a second entry

    def test_unknown_submit_missing_on_restart_is_not_resent(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
            bar(3, 101, 102, 94, 100),
            bar(4, 101, 103, 99, 102),
        ]
        conf = cfg(max_hold_bars=10, cooldown_bars=0)
        broker = PaperBroker(conf)
        broker.unknown_purposes.add("entry")
        broker.store_unknown = False
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        sup.on_closed_bar(bars[:2])
        self.assertTrue(sup.ledger.entry_halt)
        placed = broker.place_count
        path = Path("/tmp/srzone-ledger-missing.json")
        sup.ledger.save(path)
        revived = Supervisor(
            conf, broker, filters(), support_zones,
            ledger=type(sup.ledger).load(path), atr_fn=lambda _w: 4.0,
        )
        revived.reconcile()
        for i in range(2, len(bars)):
            revived.on_closed_bar(bars[: i + 1])
        self.assertEqual(broker.place_count, placed)
        self.assertEqual(broker.our_qty, 0.0)
        self.assertTrue(revived.ledger.entry_halt)

    def test_external_position_is_not_adopted_or_cancelled(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
        ]
        conf = cfg()
        broker = PaperBroker(conf)
        broker.external_qty = 2.5
        foreign = Order("ext-123", "ETHUSDT", "sl", "STOP_MARKET", "SELL", 2.5, True, status="open", trigger_price=1)
        broker.orders[foreign.client_id] = foreign
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(len(bars)):
            sup.on_closed_bar(bars[: i + 1])
        self.assertEqual(broker.our_qty, 0.0)
        self.assertEqual(broker.external_qty, 2.5)
        self.assertEqual(sup.ledger.position, None)
        self.assertNotIn("ext-123", broker.cancels)
        self.assertTrue(sup.ledger.entry_halt)
        self.assertEqual(sup.ledger.entry_halt_reason, "external_position")

    def test_orphan_stop_is_replaced(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
            bar(3, 101, 103, 100, 102),
        ]
        conf = cfg(max_hold_bars=10)
        broker = PaperBroker(conf)
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(3):
            sup.on_closed_bar(bars[: i + 1])
        sl_id = sup.ledger.position["sl_id"]
        del broker.orders[sl_id]
        sup.on_closed_bar(bars[:4])
        self.assertIsNotNone(broker._resting("sl"))
        self.assertIn("repair_protection", sup.ledger.notes)

    def test_private_api_failure_stops_new_entries(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 101, 103, 99, 102),
        ]
        conf = cfg()
        broker = PaperBroker(conf)
        broker.private_fail = True
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(len(bars)):
            sup.on_closed_bar(bars[: i + 1])
        self.assertTrue(sup.ledger.entry_halt)
        self.assertEqual(broker.our_qty, 0.0)
        self.assertEqual(broker.place_count, 0)

    def test_loss_cap_persists_across_restart(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 94, 100),
            bar(2, 100, 101, 96, 100),
            bar(3, 100, 100, 80, 90),
            bar(4, 101, 102, 94, 100),
            bar(5, 100, 101, 96, 100),
        ]
        conf = cfg(
            max_hold_bars=10, cooldown_bars=0, slippage_rate=0.0, fee_rate_per_side=0.0,
            capital_usdt=10_000.0, risk_per_trade_frac=0.011, portfolio_loss_cap_frac=0.01,
            max_total_notional_usdt=100_000.0,
        )
        broker = PaperBroker(conf)
        sup = Supervisor(conf, broker, filters(), support_zones, atr_fn=lambda _w: 4.0)
        for i in range(4):
            sup.on_closed_bar(bars[: i + 1])
        self.assertTrue(sup.ledger.risk_halt)
        self.assertEqual(len(sup.ledger.trades), 1)
        path = Path("/tmp/srzone-halt.json")
        sup.ledger.save(path)
        revived = Supervisor(
            conf, broker, filters(), support_zones,
            ledger=type(sup.ledger).load(path), atr_fn=lambda _w: 4.0,
        )
        for i in range(4, len(bars)):
            revived.on_closed_bar(bars[: i + 1])
        self.assertTrue(revived.ledger.risk_halt)
        self.assertEqual(len(revived.ledger.trades), 1)
        with self.assertRaises(PermissionError):
            revived.clear_risk_halt("please")
        revived.clear_risk_halt("CLEAR_RISK_HALT")
        self.assertFalse(revived.ledger.risk_halt)


class LiveGateTests(unittest.TestCase):
    def test_default_live_path_sends_nothing(self):
        transport = RecordingTransport()
        broker = BinanceUsdmLiveBroker(paper_research_config(), filters(), transport)
        with self.assertRaises(Exception):
            broker.place(Order("srzabc", "ETHUSDT", "entry", "MARKET", "BUY", 1, False))
        self.assertEqual(transport.calls, [])
        problems = validate_live(paper_research_config(), filters())
        self.assertIn("execution", problems)
        self.assertIn("enable_live", problems)
        self.assertIn("filters_source", problems)

    def test_explicit_opt_in_payload_is_reduce_only_and_not_account_wide(self):
        transport = RecordingTransport()
        live = paper_research_config(
            execution="live",
            enable_live=True,
            allow_private_api=True,
            confirm_live=LIVE_CONFIRM_PHRASE,
            confirm_symbol="ETHUSDT",
        )
        live_filters = filters(source="exchangeInfo")
        self.assertEqual(validate_live(live, live_filters), [])
        broker = BinanceUsdmLiveBroker(live, live_filters, transport, api_key="test-key", api_secret="test-secret")
        entry = Order(client_id("pos1", "entry"), "ETHUSDT", "entry", "MARKET", "BUY", 0.01, False)
        sl = Order(client_id("pos1", "sl"), "ETHUSDT", "sl", "STOP_MARKET", "SELL", 0.01, True, trigger_price=90)
        broker.place(entry)
        broker.place(sl)
        methods = [(c[0], c[1]) for c in transport.calls]
        self.assertIn(("POST", "/fapi/v1/order"), methods)
        self.assertIn(("POST", "/fapi/v1/algoOrder"), methods)
        sl_params = transport.calls[1][2]
        self.assertEqual(sl_params["reduceOnly"], "true")
        self.assertNotIn("closePosition", sl_params)
        self.assertEqual(sl_params["type"], "STOP_MARKET")
        with self.assertRaises(AccountWideCancelForbidden):
            broker._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": "ETHUSDT"}, True)
        self.assertTrue(all(call[1] not in FORBIDDEN_PATHS for call in transport.calls))
        cancel = broker.cancel(sl.client_id, "sl")
        self.assertEqual(cancel.state, "acked")
        self.assertEqual(transport.calls[-1][1], "/fapi/v1/algoOrder")
        foreign = broker.cancel("someone-else", "sl")
        self.assertEqual(foreign.state, "rejected")

    def test_exchange_info_fixture_parses_and_min_notional_blocks(self):
        payload = json.loads((ROOT / "src/srzone/fixtures/exchange_info_example.json").read_text())
        parsed = parse_exchange_info(payload, "ETHUSDT")
        self.assertEqual(parsed.tick_size, "0.01")
        self.assertEqual(parsed.source, "exchangeInfo")
        qty, reason = size_qty(
            side="long", equity=100.0, decision_price=100.0, stop=90.0,
            risk_per_trade_frac=0.001, leverage=1, max_total_notional=100,
            filters=parsed,
        )
        self.assertIsNone(qty)
        self.assertEqual(reason, "below_min_notional")

    def test_detector_adapter_does_not_read_probability_files(self):
        text = (ROOT / "src/srzone/zones.py").read_text(encoding="utf-8")
        self.assertNotIn("import probability", text)
        self.assertNotIn("get_prob_models", text)
        self.assertNotIn("get_calibration", text)
        self.assertNotIn("p_hold", text)

    def test_v3_zones_ignore_bars_after_the_prefix(self):
        import random
        from src.srzone.zones import zones_from_v3

        rng = random.Random(7)
        price = 100.0
        bars = []
        for i in range(140):
            opened = price
            close = max(1.0, price * (1 + rng.uniform(-0.02, 0.02)))
            high = max(opened, close) * (1 + rng.uniform(0, 0.01))
            low = min(opened, close) * (1 - rng.uniform(0, 0.01))
            bars.append(bar(i, opened, high, low, close, vol=1000))
            price = close
        first = zones_from_v3(bars[:100], symbol="TEST")
        self.assertEqual(zones_from_v3(bars[:100], symbol="TEST"), first)
        self.assertEqual(zones_from_v3(bars[:140][:100], symbol="TEST"), first)

    def test_vision_csv_keeps_the_last_duplicate_bar(self):
        import io
        import zipfile
        from src.srzone.public_data import parse_funding_zip, parse_kline_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "k.csv",
                "open_time,open,high,low,close,volume,close_time\n"
                "10,1,2,1,1.5,3,19\n"
                "10,1,2,1,1.8,4,19\n"
                "20,1,2,1,1.2,5,29\n",
            )
        parsed = parse_kline_zip(buf.getvalue())
        self.assertEqual([b.open_time for b in parsed], [10, 20])
        self.assertEqual(parsed[0].close, 1.8)
        fund = io.BytesIO()
        with zipfile.ZipFile(fund, "w") as zf:
            zf.writestr("f.csv", "calc_time,funding_interval_hours,last_funding_rate\n10,8,0.0001\n")
        self.assertEqual(parse_funding_zip(fund.getvalue()), [(10, 0.0001)])
        from src.srzone.public_data import funding_holes
        step = 8 * 3_600_000
        jitter = [(0, 0.0001), (step + 1, 0.0001), (2 * step, 0.0001)]
        self.assertEqual(funding_holes(jitter), [])
        gap = [(0, 0.0001), (2 * step, 0.0001)]
        self.assertEqual(funding_holes(gap), [step])

    def test_existing_engine_import_still_works(self):
        from src.sr_engine import SREngine
        from src.srlab.detectors import V3Fusion

        self.assertEqual(V3Fusion().name, "V3_fusion")
        self.assertEqual(SREngine().n_zones, 6)


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, params, signed, key, secret):
        self.calls.append((method, path, dict(params), signed))
        return {"status": "NEW", "clientOrderId": params.get("newClientOrderId") or params.get("clientAlgoId")}


def broker_positions(report):
    return 1 if report["n_trades"] else 0


if __name__ == "__main__":
    unittest.main()
