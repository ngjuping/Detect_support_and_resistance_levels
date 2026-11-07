# Licensed under GNU GPL v3.0, same as this project.
"""Explicit srzone-v1 configuration.

Paper research defaults are a labeled hypothetical profile, not a live
recommendation and not the upstream author's parameters. Live start refuses
to run while any required risk field or opt-in is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .models import ContractFilters

# Must be set verbatim on confirm_live. "补" is not this phrase.
LIVE_CONFIRM_PHRASE = "I_AUTHORIZE_USD_M_LIVE_ORDERS"
CLEAR_RISK_HALT = "CLEAR_RISK_HALT"
CLEAR_ENTRY_HALT = "CLEAR_ENTRY_HALT"
LIVE_FILTER_SOURCE = "exchangeInfo"


@dataclass
class StrategyConfig:
    """New-design parameters. None means unset, which paper and live both reject."""

    design_id: str = "srzone-v1"
    design_note: str = (
        "NEW DESIGN srzone-v1. Not the upstream S/R author's strategy, "
        "not an A-share hold-rate trade, and not s2bt."
    )
    execution: str = "paper"  # paper | live
    enable_live: bool = False
    allow_private_api: bool = False
    confirm_live: str = ""
    confirm_symbol: str = ""
    symbol: Optional[str] = None
    mode: str = "both"  # long | short | both
    interval: str = "1h"
    interval_ms: int = 3_600_000
    min_bars: int = 120
    causal_lookback_bars: int = 900
    atr_period: int = 14
    sl_atr_buffer: float = 0.25
    reward_risk: float = 1.5
    max_hold_bars: int = 24
    cooldown_bars: int = 6
    # Assumed costs. Not read from an account fee tier.
    fee_rate_per_side: Optional[float] = None
    slippage_rate: Optional[float] = None
    capital_usdt: Optional[float] = None
    leverage: Optional[float] = None
    max_total_notional_usdt: Optional[float] = None
    risk_per_trade_frac: Optional[float] = None
    portfolio_loss_cap_frac: Optional[float] = None
    working_type: str = "CONTRACT_PRICE"
    # Optional research window. Signals whose bar opens before this are ignored.
    trade_start_ms: Optional[int] = None
    trade_end_ms: Optional[int] = None

    def to_public_dict(self) -> dict:
        data = asdict(self)
        data["ashare_calibration_used"] = False
        data["not_s2bt"] = True
        data["not_original_author_strategy"] = True
        return data


def paper_research_config(**overrides) -> StrategyConfig:
    """Frozen research profile.

    ETHUSDT is only the public-data sample symbol. It does not select a live
    market, capital, or leverage, and it does not enable private API calls.
    """

    cfg = StrategyConfig(
        execution="paper",
        enable_live=False,
        allow_private_api=False,
        confirm_live="",
        confirm_symbol="",
        symbol="ETHUSDT",
        mode="both",
        interval="1h",
        interval_ms=3_600_000,
        min_bars=120,
        causal_lookback_bars=900,
        atr_period=14,
        sl_atr_buffer=0.25,
        reward_risk=1.5,
        max_hold_bars=24,
        cooldown_bars=6,
        fee_rate_per_side=0.0005,
        slippage_rate=0.0001,
        capital_usdt=10_000.0,
        leverage=1.0,
        max_total_notional_usdt=10_000.0,
        risk_per_trade_frac=0.005,
        portfolio_loss_cap_frac=0.20,
        working_type="CONTRACT_PRICE",
    )
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise TypeError(f"unknown config field {key}")
        setattr(cfg, key, value)
    return cfg


def _risk_problems(cfg: StrategyConfig) -> list[str]:
    problems = []
    if cfg.mode not in ("long", "short", "both"):
        problems.append("mode")
    if cfg.interval_ms <= 0:
        problems.append("interval_ms")
    if cfg.min_bars < 2 or cfg.causal_lookback_bars < cfg.atr_period + 2:
        problems.append("history_window")
    if cfg.atr_period < 1:
        problems.append("atr_period")
    if cfg.sl_atr_buffer is None or cfg.sl_atr_buffer <= 0:
        problems.append("sl_atr_buffer")
    if cfg.reward_risk is None or cfg.reward_risk <= 0:
        problems.append("reward_risk")
    if cfg.max_hold_bars < 1 or cfg.cooldown_bars < 0:
        problems.append("hold_or_cooldown")
    if cfg.fee_rate_per_side is None or cfg.fee_rate_per_side < 0:
        problems.append("fee_rate_per_side")
    if cfg.slippage_rate is None or cfg.slippage_rate < 0:
        problems.append("slippage_rate")
    if cfg.capital_usdt is None or cfg.capital_usdt <= 0:
        problems.append("capital_usdt")
    if cfg.leverage is None or cfg.leverage <= 0:
        problems.append("leverage")
    if cfg.max_total_notional_usdt is None or cfg.max_total_notional_usdt <= 0:
        problems.append("max_total_notional_usdt")
    if cfg.risk_per_trade_frac is None or not (0 < cfg.risk_per_trade_frac <= 1):
        problems.append("risk_per_trade_frac")
    if cfg.portfolio_loss_cap_frac is None or not (0 < cfg.portfolio_loss_cap_frac < 1):
        problems.append("portfolio_loss_cap_frac")
    if not cfg.symbol:
        problems.append("symbol")
    return problems


def validate_paper(cfg: StrategyConfig) -> list[str]:
    """Paper also requires explicit risk numbers. It never implies live consent."""

    problems = []
    if cfg.execution != "paper":
        problems.append("execution")
    if cfg.enable_live or cfg.allow_private_api:
        problems.append("paper_config_must_not_enable_live")
    problems.extend(_risk_problems(cfg))
    return problems


def validate_live(cfg: StrategyConfig, filters: Optional[ContractFilters]) -> list[str]:
    """All of these must pass. A single confirm flag is not enough."""

    problems = []
    if cfg.execution != "live":
        problems.append("execution")
    if cfg.enable_live is not True:
        problems.append("enable_live")
    if cfg.allow_private_api is not True:
        problems.append("allow_private_api")
    if cfg.confirm_live != LIVE_CONFIRM_PHRASE:
        problems.append("confirm_live")
    if not cfg.symbol or cfg.confirm_symbol != cfg.symbol:
        problems.append("confirm_symbol")
    if cfg.working_type not in ("CONTRACT_PRICE", "MARK_PRICE"):
        problems.append("working_type")
    problems.extend(_risk_problems(cfg))
    if filters is None:
        problems.append("filters")
    else:
        if filters.source != LIVE_FILTER_SOURCE:
            problems.append("filters_source")
        if filters.symbol != cfg.symbol:
            problems.append("filters_symbol")
        if filters.status != "TRADING":
            problems.append("filters_status")
        if filters.contract_type != "PERPETUAL":
            problems.append("filters_contract_type")
        if filters.quote_asset != "USDT" or filters.margin_asset != "USDT":
            problems.append("filters_usdt_margin")
    return problems


class LiveDisabled(RuntimeError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("live USD-M path refused: " + ",".join(problems))
