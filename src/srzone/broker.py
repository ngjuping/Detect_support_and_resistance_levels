# Licensed under GNU GPL v3.0, same as this project.
"""Paper broker used by research runs and execution tests.

Fills are explicit. A market entry staged at a signal close fills at the next
bar's open, with adverse slippage. Resting reduce-only stops can fill later
in a bar. If stop and target are both inside the same bar, the stop fills.
External quantity is visible but never closed by our reduce-only orders.
"""

from __future__ import annotations

from typing import Optional

from .config import StrategyConfig
from .models import Bar, FillEvent, Order, SubmitResult


class PrivateAPIError(RuntimeError):
    pass


class PaperBroker:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.symbol = cfg.symbol or ""
        self.orders: dict[str, Order] = {}
        self.our_qty = 0.0
        self.external_qty = 0.0
        self.avg_entry = 0.0
        self.applied_open_times: set[int] = set()
        self.funded_open_times: set[int] = set()
        self.cancels: list[str] = []
        self.place_count = 0
        self.events: list[FillEvent] = []
        self.reject_purposes: set[str] = set()
        self.unknown_purposes: set[str] = set()
        self.store_unknown = True
        self.partial_ratio: Optional[float] = None
        self.private_fail = False
        self.query_fail = False
        self.hedge_rows: Optional[list[dict]] = None

    def position_qty(self, symbol: str) -> float:
        self._check_symbol(symbol)
        return self.our_qty + self.external_qty

    def position_rows(self, symbol: str) -> list[dict]:
        self._check_symbol(symbol)
        if self.hedge_rows is not None:
            return list(self.hedge_rows)
        return [{
            "symbol": symbol,
            "positionSide": "BOTH",
            "positionAmt": self.position_qty(symbol),
        }]

    def get_order(self, client_id: str) -> Optional[Order]:
        if self.query_fail:
            raise PrivateAPIError("query failed")
        return self.orders.get(client_id)

    def place(self, order: Order) -> SubmitResult:
        if self.private_fail:
            raise PrivateAPIError("private api failed")
        if not order.client_id.startswith("srz"):
            return SubmitResult("rejected", error="foreign_client_id")
        if order.symbol != self.symbol:
            return SubmitResult("rejected", error="symbol")
        self.place_count += 1
        existing = self.orders.get(order.client_id)
        if existing is not None:
            return SubmitResult("acked", existing)
        if order.purpose in self.reject_purposes:
            rejected = order
            rejected.status = "rejected"
            return SubmitResult("rejected", rejected, error="rejected")
        if order.purpose in self.unknown_purposes:
            if self.store_unknown:
                order.status = "unknown"
                self.orders[order.client_id] = order
            return SubmitResult("unknown", error="timeout")
        order.status = "open" if order.type != "MARKET" else "staged"
        self.orders[order.client_id] = order
        return SubmitResult("acked", order)

    def cancel(self, client_id: str) -> SubmitResult:
        if not client_id.startswith("srz"):
            return SubmitResult("rejected", error="not_owned")
        self.cancels.append(client_id)
        order = self.orders.get(client_id)
        if order is None:
            return SubmitResult("acked", error="absent")
        if order.status in ("filled", "canceled", "rejected"):
            return SubmitResult("acked", order)
        order.status = "canceled"
        return SubmitResult("acked", order)

    def fill_open_orders(self, bar: Bar) -> list[FillEvent]:
        """Fill staged market orders whose activate time is this bar's open."""

        if bar.open_time in self.applied_open_times:
            return []
        self.applied_open_times.add(bar.open_time)
        events = []
        for order in list(self.orders.values()):
            if order.type != "MARKET" or order.status != "staged":
                continue
            if order.activate_open_time != bar.open_time:
                continue
            events.extend(self._fill_market(order, bar))
        return events

    def apply_funding(self, bar: Bar, rate: Optional[float], expected: bool) -> list[FillEvent]:
        if bar.open_time in self.funded_open_times:
            return []
        self.funded_open_times.add(bar.open_time)
        if not expected:
            return []
        if rate is None:
            return [FillEvent("funding_gap", "", "funding", "", 0.0, bar.open, 0.0, 0.0, bar.open_time)]
        if self.our_qty == 0:
            return []
        # Signed qty: long pays a positive rate. External qty is not our cashflow.
        cash = -self.our_qty * bar.open * rate
        return [FillEvent(
            "funding", "funding", "funding", "", self.our_qty, bar.open, 0.0, cash, bar.open_time,
        )]

    def apply_protection(self, bar: Bar) -> list[FillEvent]:
        if self.our_qty == 0:
            return []
        sl = self._resting("sl")
        tp = self._resting("tp")
        if sl is None and tp is None:
            return []
        sl_hit = sl is not None and self._triggered(sl, bar)
        tp_hit = tp is not None and self._triggered(tp, bar)
        # Conservative: a bar that trades both the stop and the target stops out.
        chosen = sl if sl_hit else (tp if tp_hit else None)
        if chosen is None:
            return []
        if sl_hit:
            kind = "exit_sl"
        else:
            kind = "exit_tp"
        price = self._protection_fill_price(chosen, bar)
        return self._reduce(chosen, price, bar.open_time, kind)

    def market_now(self, order: Order, price: float, open_time: int) -> list[FillEvent]:
        """Immediate remediation fill at a known price (the open just traded)."""

        existing = self.orders.get(order.client_id)
        if existing is not None and existing.status == "filled":
            return []
        result = self.place(order)
        if result.state != "acked" or result.order is None:
            return []
        stored = result.order
        if stored.status == "filled":
            return []
        stored.activate_open_time = open_time
        stored.status = "staged"
        return self._fill_market(stored, Bar(open_time, price, price, price, price, 0.0, open_time))

    def _fill_market(self, order: Order, bar: Bar) -> list[FillEvent]:
        if order.status == "filled":
            return []
        ratio = 1.0 if self.partial_ratio is None or order.purpose != "entry" else self.partial_ratio
        qty = order.qty * ratio
        if qty <= 0:
            order.status = "canceled"
            return []
        price = self._slip(order.side, bar.open)
        if order.reduce_only:
            return self._reduce(order, price, bar.open_time, "exit_" + order.purpose, force_qty=qty)
        if order.side == "BUY":
            self._apply_open("long", qty, price)
        elif order.side == "SELL":
            self._apply_open("short", qty, price)
        else:
            order.status = "rejected"
            return []
        order.filled_qty = qty
        order.avg_price = price
        order.status = "filled" if ratio >= 1 else "canceled"
        fee = self._fee(qty, price)
        return [FillEvent(
            "entry", order.client_id, order.purpose, order.side, qty, price, fee, -fee,
            bar.open_time, "long" if order.side == "BUY" else "short",
        )]

    def _reduce(self, order: Order, price: float, open_time: int, kind: str, force_qty: Optional[float] = None) -> list[FillEvent]:
        if self.our_qty == 0:
            order.status = "canceled"
            return []
        closing_long = self.our_qty > 0
        if closing_long and order.side != "SELL":
            order.status = "rejected"
            return []
        if (not closing_long) and order.side != "BUY":
            order.status = "rejected"
            return []
        qty = abs(self.our_qty) if force_qty is None else min(abs(self.our_qty), force_qty)
        if order.qty > 0:
            qty = min(qty, order.qty)
        if qty <= 0:
            order.status = "canceled"
            return []
        entry = self.avg_entry
        if closing_long:
            gross = (price - entry) * qty
            self.our_qty -= qty
        else:
            gross = (entry - price) * qty
            self.our_qty += qty
        if abs(self.our_qty) < 1e-12:
            self.our_qty = 0.0
            self.avg_entry = 0.0
        fee = self._fee(qty, price)
        order.filled_qty = qty
        order.avg_price = price
        order.status = "filled"
        # The sibling protection is done once we are flat; caller cancels it.
        return [FillEvent(
            kind, order.client_id, order.purpose, order.side, qty, price, fee, gross - fee,
            open_time, "long" if closing_long else "short",
        )]

    def _apply_open(self, side: str, qty: float, price: float) -> None:
        signed = qty if side == "long" else -qty
        if self.our_qty == 0:
            self.avg_entry = price
            self.our_qty = signed
            return
        # One position per symbol: do not add. Caller must not send a second entry.
        if (self.our_qty > 0 and signed > 0) or (self.our_qty < 0 and signed < 0):
            new_abs = abs(self.our_qty) + qty
            self.avg_entry = (abs(self.our_qty) * self.avg_entry + qty * price) / new_abs
            self.our_qty = new_abs if self.our_qty > 0 else -new_abs
            return
        raise RuntimeError("paper broker refused to flip a position")

    def _resting(self, purpose: str) -> Optional[Order]:
        for order in self.orders.values():
            if order.purpose == purpose and order.status == "open" and order.reduce_only:
                return order
        return None

    def _triggered(self, order: Order, bar: Bar) -> bool:
        trigger = order.trigger_price
        if trigger is None:
            return False
        if order.type == "STOP_MARKET":
            if order.side == "SELL":
                return bar.low <= trigger
            return bar.high >= trigger
        if order.type == "TAKE_PROFIT_MARKET":
            if order.side == "SELL":
                return bar.high >= trigger
            return bar.low <= trigger
        return False

    def _protection_fill_price(self, order: Order, bar: Bar) -> float:
        trigger = float(order.trigger_price)
        if order.type == "STOP_MARKET":
            # A gap through the stop fills at the worse open, not at the trigger.
            if order.side == "SELL":
                raw = min(bar.open, trigger)
            else:
                raw = max(bar.open, trigger)
            return self._slip(order.side, raw)
        # Target fills at the trigger. A gap does not improve the fill.
        return self._slip(order.side, trigger)

    def _slip(self, order_side: str, price: float) -> float:
        slip = self.cfg.slippage_rate or 0.0
        if order_side == "BUY":
            return price * (1.0 + slip)
        return price * (1.0 - slip)

    def _fee(self, qty: float, price: float) -> float:
        return abs(qty) * price * (self.cfg.fee_rate_per_side or 0.0)

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self.symbol:
            raise KeyError(symbol)
