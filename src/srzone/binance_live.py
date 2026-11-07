# Licensed under GNU GPL v3.0, same as this project.
"""USD-M live adapter. Hard off unless ``validate_live`` is clean.

Public docs used for the paths (no account was signed in while writing this):

- ``GET /fapi/v1/exchangeInfo`` and ``GET /fapi/v1/klines`` as documented by
  the official ``binance-futures-connector-python`` UM market client
  (https://github.com/binance/binance-futures-connector-python).
- ``POST /fapi/v1/order`` for MARKET entry and reduce-only flatten.
- Conditional orders follow the Binance USD-M changelog of 2025-11-06:
  effective 2025-12-09, ``STOP_MARKET`` and ``TAKE_PROFIT_MARKET`` move to
  ``POST /fapi/v1/algoOrder`` (``algoType=CONDITIONAL``). ``POST /fapi/v1/order``
  returns ``-4120`` for those types. Query and cancel use
  ``GET`` / ``DELETE /fapi/v1/algoOrder`` with ``clientAlgoId``.
- Positions: ``GET /fapi/v3/positionRisk`` (v2 deprecated in that connector).

This adapter never calls ``DELETE /fapi/v1/allOpenOrders``,
``DELETE /fapi/v1/algoOpenOrders``, or ``POST /fapi/v1/countdownCancelAll``.
It cancels only a client id it was given, and only ids with the ``srz`` prefix.
It does not read environment variables or credential files.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional
from urllib.parse import urlencode

from .broker import PrivateAPIError
from .config import LiveDisabled, validate_live
from .models import ContractFilters, Order, SubmitResult


FORBIDDEN_PATHS = (
    "/fapi/v1/allOpenOrders",
    "/fapi/v1/algoOpenOrders",
    "/fapi/v1/countdownCancelAll",
)


class AccountWideCancelForbidden(RuntimeError):
    pass


def sign_query(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def client_id(pos_id: str, purpose: str) -> str:
    """Binance client ids allow ``[A-Za-z0-9-_]`` and are limited to 36 chars."""

    suffix = {"entry": "en", "sl": "sl", "tp": "tp", "flatten": "fl"}[purpose]
    raw = f"srz{pos_id.replace('-', '')[:24]}{suffix}"
    if not raw.startswith("srz") or len(raw) > 36:
        raise ValueError(raw)
    return raw


class BinanceUsdmLiveBroker:
    """Signed USD-M requests. Construction does not send anything."""

    def __init__(self, cfg, filters: Optional[ContractFilters], transport, api_key: str = "", api_secret: str = ""):
        self.cfg = cfg
        self.filters = filters
        self.transport = transport
        self.api_key = api_key
        self.api_secret = api_secret
        self._problems = validate_live(cfg, filters)

    def gate(self) -> None:
        if self._problems:
            raise LiveDisabled(self._problems)
        if not self.api_key or not self.api_secret:
            raise LiveDisabled(self._problems + ["missing_injected_credentials"])

    def place(self, order: Order) -> SubmitResult:
        self.gate()
        if not order.client_id.startswith("srz"):
            return SubmitResult("rejected", error="foreign_client_id")
        if order.type == "MARKET":
            params = {
                "symbol": order.symbol,
                "side": order.side,
                "type": "MARKET",
                "quantity": _num(order.qty),
                "newClientOrderId": order.client_id,
                "positionSide": "BOTH",
                "newOrderRespType": "RESULT",
            }
            if order.reduce_only:
                params["reduceOnly"] = "true"
            return self._send("POST", "/fapi/v1/order", params, order)
        if order.type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if not order.reduce_only:
                return SubmitResult("rejected", error="protection_must_be_reduce_only")
            if order.trigger_price is None:
                return SubmitResult("rejected", error="trigger")
            # Quantity plus reduceOnly. closePosition is intentionally absent:
            # closePosition would close quantity we do not own.
            params = {
                "algoType": "CONDITIONAL",
                "symbol": order.symbol,
                "side": order.side,
                "type": order.type,
                "quantity": _num(order.qty),
                "triggerPrice": _num(order.trigger_price),
                "workingType": self.cfg.working_type,
                "reduceOnly": "true",
                "clientAlgoId": order.client_id,
                "positionSide": "BOTH",
            }
            return self._send("POST", "/fapi/v1/algoOrder", params, order)
        return SubmitResult("rejected", error="unsupported_type")

    def cancel(self, client_id: str, kind: str) -> SubmitResult:
        self.gate()
        if not client_id.startswith("srz"):
            return SubmitResult("rejected", error="not_owned")
        symbol = self.cfg.symbol
        if kind in ("sl", "tp"):
            path = "/fapi/v1/algoOrder"
            params = {"symbol": symbol, "clientAlgoId": client_id}
        else:
            path = "/fapi/v1/order"
            params = {"symbol": symbol, "origClientOrderId": client_id}
        try:
            self._request("DELETE", path, params, signed=True)
        except PrivateAPIError as exc:
            return SubmitResult("unknown", error=str(exc))
        return SubmitResult("acked")

    def get_order(self, client_id: str, kind: str) -> Optional[dict]:
        self.gate()
        symbol = self.cfg.symbol
        if kind in ("sl", "tp"):
            path, params = "/fapi/v1/algoOrder", {"symbol": symbol, "clientAlgoId": client_id}
        else:
            path, params = "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}
        try:
            return self._request("GET", path, params, signed=True)
        except PrivateAPIError as exc:
            if "absent" in str(exc):
                return None
            raise

    def position_rows(self, symbol: str) -> list[dict]:
        self.gate()
        rows = self._request("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True)
        if isinstance(rows, dict):
            return [rows]
        return [row for row in rows if row.get("symbol") == symbol]

    def _send(self, method: str, path: str, params: dict, order: Order) -> SubmitResult:
        try:
            payload = self._request(method, path, params, signed=True)
        except PrivateAPIError as exc:
            return SubmitResult("unknown", error=str(exc))
        if isinstance(payload, dict) and payload.get("code"):
            return SubmitResult("rejected", error=str(payload.get("msg") or payload.get("code")))
        order.status = "open"
        return SubmitResult("acked", order)

    def _request(self, method: str, path: str, params: dict, signed: bool):
        if path in FORBIDDEN_PATHS:
            raise AccountWideCancelForbidden(path)
        if any(piece in path for piece in ("allOpenOrders", "algoOpenOrders", "countdownCancelAll")):
            raise AccountWideCancelForbidden(path)
        return self.transport.request(method, path, params, signed, self.api_key, self.api_secret)


def _num(value: float) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
