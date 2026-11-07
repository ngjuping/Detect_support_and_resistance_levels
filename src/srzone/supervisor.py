# Licensed under GNU GPL v3.0, same as this project.
"""One-symbol supervisor.

Decisions use only the closed prefix. An entry staged at the close of bar i
can fill at the open of bar i+1 and nowhere earlier. Protective orders are
reduce-only and sized to the filled quantity. A failed protection attempt
flattens that quantity; if the flatten also fails, a persistent risk halt is
stored in the ledger. External positions are not adopted and their orders are
not canceled. Ambiguous submits are queried, never blindly resent.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from src.srlab.data import atr_series

from .binance_live import client_id
from .broker import PaperBroker, PrivateAPIError
from .config import CLEAR_ENTRY_HALT, CLEAR_RISK_HALT, StrategyConfig, validate_paper
from .filters import round_target, size_qty
from .ledger import Ledger
from .models import Bar, ContractFilters, FillEvent, Order, Zone
from .rules import entry_allowed_at_open, signal_from_closed_bar


ZoneFn = Callable[[Sequence[Bar]], list[Zone]]
AtrFn = Callable[[Sequence[Bar]], float]


def default_atr(bars: Sequence[Bar], period: int) -> float:
    if len(bars) < 2:
        return float("nan")
    import numpy as np

    high = np.array([b.high for b in bars], dtype=float)
    low = np.array([b.low for b in bars], dtype=float)
    close = np.array([b.close for b in bars], dtype=float)
    return float(atr_series(high, low, close, period)[-1])


class Supervisor:
    def __init__(
        self,
        cfg: StrategyConfig,
        broker: PaperBroker,
        filters: ContractFilters,
        zone_fn: ZoneFn,
        ledger: Optional[Ledger] = None,
        atr_fn: Optional[AtrFn] = None,
    ):
        problems = validate_paper(cfg)
        if problems:
            raise ValueError("paper config refused: " + ",".join(problems))
        self.cfg = cfg
        self.broker = broker
        self.filters = filters
        self.zone_fn = zone_fn
        self.atr_fn = atr_fn or (lambda bars: default_atr(bars, cfg.atr_period))
        self.ledger = ledger or Ledger(symbol=cfg.symbol or "", initial_equity=float(cfg.capital_usdt))
        self._seq = len(self.ledger.trades) + len(self.ledger.owned_ids) + 1

    def equity(self, mark: float) -> float:
        unreal = 0.0
        pos = self.ledger.position
        if pos and math.isfinite(mark):
            if pos["side"] == "long":
                unreal = (mark - pos["entry"]) * pos["qty"]
            else:
                unreal = (pos["entry"] - mark) * pos["qty"]
            unreal -= pos["qty"] * mark * float(self.cfg.fee_rate_per_side or 0.0)
        return float(self.cfg.capital_usdt) + self.ledger.realized_pnl + unreal

    def clear_risk_halt(self, phrase: str) -> None:
        if phrase != CLEAR_RISK_HALT:
            raise PermissionError("risk halt stays until CLEAR_RISK_HALT")
        self.ledger.risk_halt = False
        self.ledger.risk_halt_reason = ""

    def clear_entry_halt(self, phrase: str) -> None:
        if phrase != CLEAR_ENTRY_HALT:
            raise PermissionError("entry halt stays until CLEAR_ENTRY_HALT")
        if self.ledger.risk_halt:
            raise PermissionError("risk halt is still set")
        self.ledger.entry_halt = False
        self.ledger.entry_halt_reason = ""

    def reconcile(self) -> None:
        """Reload path. Does not resend an order whose submit result was unknown."""

        self._check_external()
        pending = self.ledger.pending_entry
        if pending and pending.get("state") == "submit_unknown":
            self._resolve_unknown(pending, "entry")
        if self.ledger.position and not self.ledger.risk_halt:
            self._repair_protection(self._last_mark_price())

    def on_closed_bar(
        self,
        bars: Sequence[Bar],
        funding_rate: Optional[float] = None,
        funding_expected: bool = False,
    ) -> dict:
        if not bars:
            return {"skipped": "empty"}
        bar = bars[-1]
        if bar.open_time in self.ledger.processed_open_times:
            return {"duplicate": True}
        if bar.open_time in self.broker.applied_open_times and self.ledger.position is None and self.ledger.pending_entry is None:
            self.ledger.mark_processed(bar.open_time)
            return {"duplicate": True}

        summary = {"open_time": bar.open_time, "events": []}
        self._prepare_open(bar)
        try:
            open_events = self.broker.fill_open_orders(bar)
        except PrivateAPIError as exc:
            self.ledger.halt_entries(f"private_api:{exc}")
            self.ledger.mark_processed(bar.open_time)
            return summary
        self._apply_events(open_events)
        summary["events"].extend(kind_of(open_events))
        self._maybe_accept_entry(bar)
        self._check_external()

        fund_events = self.broker.apply_funding(bar, funding_rate, funding_expected)
        for event in fund_events:
            if event.kind == "funding_gap":
                self.ledger.funding_gaps += 1
                self.ledger.notes.append(f"funding_gap:{bar.open_time}")
            else:
                self.ledger.realized_pnl += event.pnl
        summary["events"].extend(kind_of(fund_events))

        if self.ledger.position and not self._protection_open():
            self._repair_protection(bar.open)
        prot_events = self.broker.apply_protection(bar)
        self._apply_events(prot_events)
        summary["events"].extend(kind_of(prot_events))

        self._mark_loss_cap(bar.close)
        self._time_exit_if_due(bar)
        self._stage_new_entry(bars)
        self.ledger.mark_processed(bar.open_time)
        return summary

    def _prepare_open(self, bar: Bar) -> None:
        """Resolve a lost ack, then cancel a staged entry before it can fill."""

        pending = self.ledger.pending_entry
        if pending and pending.get("state") == "submit_unknown":
            self._resolve_unknown(pending, "entry")
            pending = self.ledger.pending_entry
        if not pending or pending.get("state") not in ("staged", "acked", "open"):
            self._expire_time_exit(bar)
            return
        if bar.open_time < pending["entry_open_time"]:
            self._expire_time_exit(bar)
            return
        if bar.open_time > pending["entry_open_time"] or not entry_allowed_at_open(
            pending["side"], bar.open, pending["stop"]
        ):
            reason = "gap_or_missed_open" if bar.open_time != pending["entry_open_time"] else "open_through_stop"
            self._drop_pending(reason)
        self._expire_time_exit(bar)

    def _expire_time_exit(self, bar: Bar) -> None:
        pending = self.ledger.pending_exit
        if not pending:
            return
        if bar.open_time <= pending["entry_open_time"]:
            return
        # The scheduled exit bar never arrived. Flatten at this open.
        self.broker.cancel(pending["client_id"])
        self.ledger.pending_exit = None
        if self.ledger.position and self.broker.our_qty != 0:
            self._flatten_now(bar, "missed_time_exit")

    def _maybe_accept_entry(self, bar: Bar) -> None:
        pending = self.ledger.pending_entry
        if not pending or pending.get("state") not in ("staged", "acked", "open"):
            return
        if pending["entry_open_time"] != bar.open_time:
            return
        order = self.broker.get_order(pending["client_id"])
        if order is None or order.filled_qty <= 0:
            return
        filled = order.filled_qty
        price = float(order.avg_price)
        side = pending["side"]
        if (side == "long" and price <= pending["stop"]) or (side == "short" and price >= pending["stop"]):
            self._flatten_now(bar, "fill_through_stop")
            return
        dist = (price - pending["stop"]) if side == "long" else (pending["stop"] - price)
        target_raw = price + self.cfg.reward_risk * dist if side == "long" else price - self.cfg.reward_risk * dist
        target = round_target(side, target_raw, self.filters.tick_size)
        pos_id = pending["pos_id"]
        self.ledger.position = {
            "pos_id": pos_id,
            "side": side,
            "qty": filled,
            "entry": price,
            "stop": pending["stop"],
            "tp": target,
            "entry_open_time": bar.open_time,
            "signal_open_time": pending["signal_open_time"],
            "zone_key": pending["zone_key"],
            "closes_completed": 0,
            "sl_id": client_id(pos_id, "sl"),
            "tp_id": client_id(pos_id, "tp"),
            "entry_id": pending["client_id"],
        }
        self.ledger.pending_entry = None
        self._place_protection(bar.open)

    def _place_protection(self, mark: float) -> None:
        pos = self.ledger.position
        if not pos:
            return
        exit_side = "SELL" if pos["side"] == "long" else "BUY"
        sl = self._order(pos["sl_id"], "sl", "STOP_MARKET", exit_side, pos["qty"], True, pos["stop"])
        tp = self._order(pos["tp_id"], "tp", "TAKE_PROFIT_MARKET", exit_side, pos["qty"], True, pos["tp"])
        ok_sl = self._place(sl)
        ok_tp = self._place(tp)
        if not ok_sl:
            self._flatten_now_price(mark, pos["entry_open_time"], "protection_failed")

    def _repair_protection(self, mark: float) -> None:
        pos = self.ledger.position
        if not pos or self.broker.our_qty == 0:
            return
        if self._protection_open():
            return
        self.ledger.notes.append("repair_protection")
        self._place_protection(mark)

    def _protection_open(self) -> bool:
        pos = self.ledger.position
        if not pos:
            return False
        try:
            order = self.broker.get_order(pos["sl_id"])
        except PrivateAPIError:
            self.ledger.halt_entries("protection_query_failed")
            return False
        return order is not None and order.status in ("open", "staged", "unknown")

    def _flatten_now(self, bar: Bar, reason: str) -> None:
        self._flatten_now_price(bar.open, bar.open_time, reason)

    def _flatten_now_price(self, price: float, open_time: int, reason: str) -> None:
        pos = self.ledger.position
        qty = abs(self.broker.our_qty)
        side = "SELL" if self.broker.our_qty > 0 else "BUY"
        if qty <= 0 or pos is None:
            self._drop_pending(reason)
            self.ledger.halt_entries(reason)
            return
        self._cancel_owned_protection()
        flat_id = client_id(pos["pos_id"], "flatten")
        order = self._order(flat_id, "flatten", "MARKET", side, qty, True, None)
        try:
            events = self.broker.market_now(order, price, open_time)
        except PrivateAPIError as exc:
            self.ledger.halt_risk(f"flatten_private_api:{exc}")
            return
        if not events or self.broker.our_qty != 0:
            self.ledger.halt_risk("flatten_failed:" + reason)
            return
        self._apply_events(events)
        self.ledger.halt_entries(reason)

    def _stage_new_entry(self, bars: Sequence[Bar]) -> None:
        if self.ledger.risk_halt or self.ledger.entry_halt:
            return
        if self.ledger.position or self.ledger.pending_entry or self.broker.our_qty != 0:
            return
        bar = bars[-1]
        if self.cfg.trade_start_ms is not None and bar.open_time < self.cfg.trade_start_ms:
            return
        if self.cfg.trade_end_ms is not None and bar.open_time >= self.cfg.trade_end_ms:
            return
        if len(bars) < self.cfg.min_bars:
            return
        if bar.open_time < self.ledger.cooldown_until_ms:
            return
        start = max(0, len(bars) - self.cfg.causal_lookback_bars)
        window = list(bars[start:])
        try:
            atr = float(self.atr_fn(window))
            zones = self.zone_fn(window)
        except Exception as exc:
            self.ledger.notes.append(f"zone_failure:{exc}")
            return
        decision = signal_from_closed_bar(window[-2], window[-1], zones, atr, self.cfg, self.filters.tick_size)
        if decision.signal is None:
            return
        signal = decision.signal
        qty, why = size_qty(
            side=signal.side,
            equity=self.equity(bar.close),
            decision_price=bar.close,
            stop=signal.stop,
            risk_per_trade_frac=float(self.cfg.risk_per_trade_frac),
            leverage=float(self.cfg.leverage),
            max_total_notional=float(self.cfg.max_total_notional_usdt),
            filters=self.filters,
        )
        if qty is None:
            self.ledger.notes.append(f"size_skip:{why}")
            return
        pos_id = f"{bar.open_time:x}{self._seq:x}"
        self._seq += 1
        entry_id = client_id(pos_id, "entry")
        order_side = "BUY" if signal.side == "long" else "SELL"
        when = bar.open_time + self.cfg.interval_ms
        order = self._order(entry_id, "entry", "MARKET", order_side, qty, False, None)
        order.activate_open_time = when
        self.ledger.remember_id(entry_id)
        try:
            result = self.broker.place(order)
        except PrivateAPIError as exc:
            self.ledger.pending_entry = self._pending(signal, pos_id, entry_id, qty, when, "submit_unknown")
            self.ledger.halt_entries(f"private_api:{exc}")
            return
        if result.state == "rejected":
            self.ledger.notes.append("entry_rejected:" + result.error)
            return
        if result.state == "unknown":
            self.ledger.pending_entry = self._pending(signal, pos_id, entry_id, qty, when, "submit_unknown")
            try:
                found = self.broker.get_order(entry_id)
            except PrivateAPIError:
                self.ledger.halt_entries("entry_query_failed")
                return
            if found is None:
                self.ledger.halt_entries("ambiguous_submit_not_found")
            return
        self.ledger.pending_entry = self._pending(signal, pos_id, entry_id, qty, when, "staged")

    def _pending(self, signal, pos_id, entry_id, qty, when, state) -> dict:
        return {
            "pos_id": pos_id,
            "client_id": entry_id,
            "state": state,
            "side": signal.side,
            "qty": qty,
            "stop": signal.stop,
            "signal_open_time": signal.signal_open_time,
            "entry_open_time": when,
            "zone_key": signal.zone_key,
        }

    def _time_exit_if_due(self, bar: Bar) -> None:
        pos = self.ledger.position
        if not pos or self.broker.our_qty == 0:
            return
        if bar.open_time < pos["entry_open_time"]:
            return
        if bar.open_time == pos["entry_open_time"]:
            pos["closes_completed"] = 1
        else:
            pos["closes_completed"] = int(pos["closes_completed"]) + 1
        if pos["closes_completed"] < self.cfg.max_hold_bars:
            return
        if self.ledger.pending_exit:
            return
        # Leave the reduce-only stop up until the exit fill. The exit is a
        # market order at the next open; the stop still covers that bar's range
        # if the market exit does not fill.
        exit_side = "SELL" if pos["side"] == "long" else "BUY"
        exit_id = client_id(pos["pos_id"] + "t", "flatten")
        # client_id uses pos id; adding t may collide with length. Use flatten slot only if unused.
        # A time exit uses a distinct id by folding the bar time.
        exit_id = client_id(f"{pos['pos_id']}{bar.open_time:x}"[-20:], "flatten")
        when = bar.open_time + self.cfg.interval_ms
        order = self._order(exit_id, "flatten", "MARKET", exit_side, pos["qty"], True, None)
        order.activate_open_time = when
        if self._place(order):
            self.ledger.pending_exit = {"client_id": exit_id, "entry_open_time": when}
            self.ledger.remember_id(exit_id)

    def _apply_events(self, events: list[FillEvent]) -> None:
        for event in events:
            self.ledger.realized_pnl += event.pnl
            if event.kind == "entry":
                continue
            if event.kind.startswith("exit") and self.broker.our_qty == 0:
                self._record_exit(event)

    def _record_exit(self, event: FillEvent) -> None:
        pos = self.ledger.position
        if pos is None:
            return
        self.ledger.trades.append({
            "side": pos["side"],
            "qty": pos["qty"],
            "entry": pos["entry"],
            "exit": event.price,
            "pnl": event.pnl,
            "reason": event.kind,
            "signal_open_time": pos["signal_open_time"],
            "entry_open_time": pos["entry_open_time"],
            "exit_open_time": event.open_time,
            "zone_key": pos["zone_key"],
        })
        self.ledger.cooldown_until_ms = event.open_time + self.cfg.cooldown_bars * self.cfg.interval_ms
        self._cancel_owned_protection()
        self.ledger.position = None
        self.ledger.pending_exit = None

    def _resolve_unknown(self, pending: dict, kind: str) -> None:
        try:
            order = self.broker.get_order(pending["client_id"])
        except PrivateAPIError:
            self.ledger.halt_entries("reconcile_query_failed")
            return
        if order is None:
            self.ledger.halt_entries("ambiguous_submit_not_found")
            return
        if order.status == "unknown":
            order.status = "staged"
        pending["state"] = "staged" if order.status in ("staged", "open", "unknown") else order.status
        self.ledger.notes.append("reconciled_existing_order")

    def _check_external(self) -> None:
        symbol = self.cfg.symbol or ""
        try:
            rows = self.broker.position_rows(symbol)
        except PrivateAPIError:
            self.ledger.halt_entries("position_query_failed")
            return
        hedge = [r for r in rows if str(r.get("positionSide")) in ("LONG", "SHORT") and abs(float(r.get("positionAmt") or 0)) > 0]
        if len(hedge) >= 2:
            self.ledger.halt_entries("hedge_mode_unsupported")
            return
        try:
            exch = float(self.broker.position_qty(symbol))
        except PrivateAPIError:
            self.ledger.halt_entries("position_query_failed")
            return
        dust = float(self.filters.market_step()) / 2
        if abs(exch - self.broker.our_qty) > dust:
            self.ledger.halt_entries("external_position")

    def _mark_loss_cap(self, mark: float) -> None:
        if not math.isfinite(mark):
            self.ledger.notes.append("bad_mark")
            return
        start = float(self.cfg.capital_usdt)
        floor = start * (1.0 - float(self.cfg.portfolio_loss_cap_frac))
        if self.equity(mark) <= floor:
            self.ledger.halt_risk("portfolio_loss_cap")

    def _drop_pending(self, reason: str) -> None:
        pending = self.ledger.pending_entry
        if pending:
            self.broker.cancel(pending["client_id"])
        self.ledger.pending_entry = None
        self.ledger.notes.append(reason)

    def _cancel_owned_protection(self) -> None:
        pos = self.ledger.position
        if not pos:
            return
        for cid in (pos.get("sl_id"), pos.get("tp_id")):
            if cid and cid.startswith("srz"):
                self.broker.cancel(cid)

    def _place(self, order: Order) -> bool:
        self.ledger.remember_id(order.client_id)
        try:
            result = self.broker.place(order)
        except PrivateAPIError as exc:
            self.ledger.halt_entries(f"private_api:{exc}")
            return False
        if result.state == "acked":
            return True
        if result.state == "unknown":
            return False
        return False

    def _order(self, cid, purpose, typ, side, qty, reduce_only, trigger) -> Order:
        return Order(
            client_id=cid,
            symbol=self.cfg.symbol or "",
            purpose=purpose,
            type=typ,
            side=side,
            qty=float(qty),
            reduce_only=reduce_only,
            trigger_price=None if trigger is None else float(trigger),
            owned=True,
        )

    def _last_mark_price(self) -> float:
        pos = self.ledger.position
        return float(pos["entry"]) if pos else 0.0


def kind_of(events: list[FillEvent]) -> list[str]:
    return [event.kind for event in events]
