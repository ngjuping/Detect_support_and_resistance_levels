# Licensed under GNU GPL v3.0, same as this project.
"""JSON ledger. Restart reads this file; it does not adopt exchange state blindly."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Ledger:
    symbol: str
    initial_equity: float
    realized_pnl: float = 0.0
    risk_halt: bool = False
    risk_halt_reason: str = ""
    entry_halt: bool = False
    entry_halt_reason: str = ""
    position: Optional[dict] = None
    pending_entry: Optional[dict] = None
    pending_exit: Optional[dict] = None
    cooldown_until_ms: int = 0
    owned_ids: list = field(default_factory=list)
    processed_open_times: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    funding_gaps: int = 0
    notes: list = field(default_factory=list)
    version: int = 1
    strategy: str = "srzone-v1"

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "initial_equity": self.initial_equity,
            "realized_pnl": self.realized_pnl,
            "risk_halt": self.risk_halt,
            "risk_halt_reason": self.risk_halt_reason,
            "entry_halt": self.entry_halt,
            "entry_halt_reason": self.entry_halt_reason,
            "position": self.position,
            "pending_entry": self.pending_entry,
            "pending_exit": self.pending_exit,
            "cooldown_until_ms": self.cooldown_until_ms,
            "owned_ids": list(self.owned_ids),
            "processed_open_times": list(self.processed_open_times[-100000:]),
            "trades": list(self.trades),
            "funding_gaps": self.funding_gaps,
            "notes": list(self.notes[-1000:]),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Ledger":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        return cls(**payload)

    def save(self, path: str | os.PathLike) -> None:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, dest)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Ledger":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def remember_id(self, client_id: str) -> None:
        if client_id not in self.owned_ids:
            self.owned_ids.append(client_id)

    def mark_processed(self, open_time: int) -> None:
        if open_time not in self.processed_open_times:
            self.processed_open_times.append(open_time)

    def halt_entries(self, reason: str) -> None:
        self.entry_halt = True
        self.entry_halt_reason = reason
        self.notes.append(f"entry_halt:{reason}")

    def halt_risk(self, reason: str) -> None:
        self.risk_halt = True
        self.risk_halt_reason = reason
        self.entry_halt = True
        self.entry_halt_reason = reason
        self.notes.append(f"risk_halt:{reason}")
