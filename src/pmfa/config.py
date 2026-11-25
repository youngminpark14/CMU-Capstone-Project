"""One typed configuration: every limit, threshold, source, model assignment, and followed series.

Behavior changes are diffs of ``pmfa.toml``. Secrets are never configuration:
they are read from the environment, and only the variable *names* live here.
The configuration is validated when it is loaded, including that the caps are
consistent with each other.
"""

from __future__ import annotations

import os
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from pmfa.errors import ConfigError
from pmfa.records.roles import ModelSlot

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeriesConfig(Section):
    """A followed series and how its events turn into an outcome space."""

    ticker: str
    category: str
    enabled: bool = True
    outcome_kind: Literal["categorical", "ladder"]
    variable: str = ""
    unit: str = ""
    increment: float | None = None
    """Ladders: the step in which the resolving number is reported."""
    primary_sources: list[str]
    """Official publishers whose documents are primary origins for this category."""
    recency_half_life_days: float = 30.0
    shrinkage: float = 0.5
    """How far the forecast is pulled toward the market price: 0 keeps it, 1 replaces it."""


class FollowConfig(Section):
    series: list[SeriesConfig] = [
        SeriesConfig(
            ticker="KXFEDDECISION",
            category="fed_decision",
            outcome_kind="categorical",
            primary_sources=["federalreserve.gov"],
        ),
        SeriesConfig(
            ticker="KXCPIYOY",
            category="cpi",
            outcome_kind="ladder",
            variable="CPI-U all items, 12-month percent change, not seasonally adjusted",
            unit="percent",
            increment=0.1,
            primary_sources=["bls.gov"],
        ),
        SeriesConfig(
            ticker="KXFED",
            category="fed_rate",
            enabled=False,
            outcome_kind="ladder",
            variable="upper bound of the federal funds target range after the meeting",
            unit="percent",
            increment=0.25,
            primary_sources=["federalreserve.gov"],
        ),
        SeriesConfig(
            ticker="KXPAYROLLS",
            category="payrolls",
            enabled=False,
            outcome_kind="ladder",
            variable="change in total nonfarm payroll employment",
            unit="jobs",
            increment=1000.0,
            primary_sources=["bls.gov"],
        ),
    ]
    blocklist: list[str] = []
    """Series or market tickers the agent must never trade."""

    def enabled_series(self) -> list[SeriesConfig]:
        return [s for s in self.series if s.enabled]

    def for_ticker(self, series_ticker: str) -> SeriesConfig | None:
        for s in self.series:
            if s.ticker == series_ticker:
                return s
        return None


class SourcesConfig(Section):
    federal_reserve: bool = True
    bls: bool = True
    user_agent: str = "pmfa-research-client/0.1"
    """Sent to source sites. Generic on purpose: no personal contact details."""
    min_seconds_between_requests: float = 2.0
    manual_dir: str = "data/manual"
    """Hand-downloaded releases go here when a site refuses automated clients."""


class TriggerConfig(Section):
    price_move_cents: int = 3
    price_move_window_minutes: int = 30
    daily_recheck_open_positions: bool = False
    schedule_horizons_days: list[int] = [7, 3, 1]


class RetrievalConfig(Section):
    event_candidates: int = 5
    passage_words: int = 250
    passage_overlap_words: int = 50
    top_k: int = 8
    max_per_origin: int = 3
    similarity_floor: float = 0.05
    near_duplicate_similarity: float = 0.92
    ledger_k: int = 5
    ranking: Literal["recency_origin", "similarity"] = "recency_origin"
    embedder: Literal["hashing", "minilm"] = "hashing"
    """``hashing`` is deterministic and needs no download; ``minilm`` is the local ONNX sentence model."""


class ModelAssignment(Section):
    model: str
    effort: Effort
    max_tokens: int = 16000


class ModelInfo(Section):
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    knowledge_cutoff: date
    """Last day of the reliable-knowledge cutoff the vendor lists."""


class ModelsConfig(Section):
    slots: dict[ModelSlot, ModelAssignment] = {
        ModelSlot.MONITOR: ModelAssignment(model="claude-haiku-5-5", effort="low", max_tokens=4000),
        ModelSlot.RESEARCHER: ModelAssignment(
            model="claude-haiku-5-5", effort="medium", max_tokens=8000
        ),
        ModelSlot.GENERATOR: ModelAssignment(
            model="claude-haiku-5-5", effort="medium", max_tokens=8000
        ),
        ModelSlot.CRITIC_DEEP: ModelAssignment(model="claude-sonnet-5-5", effort="high"),
        ModelSlot.CRITIC_DEPTH1: ModelAssignment(model="claude-fable-5-2", effort="high"),
        ModelSlot.FAST_PATH: ModelAssignment(
            model="claude-haiku-5-5", effort="low", max_tokens=4000
        ),
        ModelSlot.BASELINE: ModelAssignment(model="claude-fable-5-2", effort="high"),
    }
    catalog: dict[str, ModelInfo] = {
        "claude-fable-5-2": ModelInfo(
            input_per_mtok=Decimal("8"),
            output_per_mtok=Decimal("40"),
            knowledge_cutoff=date(2026, 6, 30),
        ),
        "claude-opus-5-5": ModelInfo(
            input_per_mtok=Decimal("4"),
            output_per_mtok=Decimal("20"),
            knowledge_cutoff=date(2026, 6, 30),
        ),
        "claude-sonnet-5-5": ModelInfo(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("10"),
            knowledge_cutoff=date(2026, 6, 30),
        ),
        "claude-haiku-5-5": ModelInfo(
            input_per_mtok=Decimal("0.10"),
            output_per_mtok=Decimal("0.50"),
            knowledge_cutoff=date(2026, 6, 30),
        ),
    }
    api_key_env: str = "ANTHROPIC_API_KEY"
    refusal_fallback: Literal["default", "off"] = "default"
    """Server-side fallback when a model declines a request. The serving model is recorded per call."""
    request_timeout_seconds: float = 180.0
    roles_runner: Literal["auto", "crewai", "gateway"] = "auto"
    """``auto`` runs roles through CrewAI when it is installed, else calls the gateway directly."""


class BudgetConfig(Section):
    max_calls_per_episode: int = 30
    max_seconds_per_episode: float = 90.0
    """Applies in live modes. Backtests use the call budget alone."""
    max_usd_per_episode: Decimal = Decimal("2.00")
    max_usd_per_run: Decimal = Decimal("25.00")


class ForecastConfig(Section):
    max_depth: int = 3
    beam_width: int = 3
    max_thoughts_per_node: int = 3
    coverage_floor: float = 0.6
    tie_band: float = 0.05
    confidence_full_origins: int = 3
    max_evidence_followups: int = 3
    fast_path_within_minutes: int = 60
    quantile_levels: list[float] = [0.05, 0.25, 0.5, 0.75, 0.95]


class SizingConfig(Section):
    kelly_fraction: Decimal = Decimal("0.25")
    min_edge: Decimal = Decimal("0.02")


class CapsConfig(Section):
    per_market_fraction: Decimal = Decimal("0.05")
    per_event_fraction: Decimal = Decimal("0.10")
    max_contracts_per_order: int = 25
    daily_loss_stop_fraction: Decimal = Decimal("0.03")
    drawdown_halt_fraction: Decimal = Decimal("0.10")
    liquidity_floor_dollars: Decimal = Decimal("500")
    liquidity_band: Decimal = Decimal("0.02")
    order_group_contracts_limit: int = 50
    """The exchange-side breaker: contracts per rolling 15 seconds for the order group."""
    risk_unit_fraction: Decimal = Decimal("0.01")


class RoutingConfig(Section):
    auto_max_risk_units: Decimal = Decimal("1")
    calibration_min_resolved: int = 30
    calibration_error_max: float = 0.10
    max_price_move: Decimal = Decimal("0.03")


class OversightConfig(Section):
    proposal_ttl_minutes: int = 10
    fill_failure_halt_fraction: float = 0.10
    fill_failure_window: int = 10
    calibration_halt_weeks: int = 2
    timezone: str = "America/New_York"
    """The trading day used by the daily loss stop."""


class StageConfig(Section):
    backtest_min_markets: int = 200
    shadow_min_weeks: int = 4
    shadow_max_drawdown_fraction: Decimal = Decimal("0.10")
    approval_min_trades: int = 100
    approval_max_override_fraction: float = 0.15
    demote_after_daily_stops_in_week: int = 2


class ExecutionConfig(Section):
    notional_bankroll: Decimal = Decimal("1000")
    """Bankroll in backtest and shadow. Demo mode uses the subaccount's balance."""
    backtest_latency_seconds: int = 60
    backtest_volume_share: Decimal = Decimal("0.10")
    backtest_liquidity_window_minutes: int = 60


class KalshiConfig(Section):
    production_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    demo_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    demo_key_id_env: str = "KALSHI_DEMO_KEY_ID"
    demo_private_key_env: str = "KALSHI_DEMO_PRIVATE_KEY"
    demo_private_key_path_env: str = "KALSHI_DEMO_PRIVATE_KEY_PATH"
    demo_subaccount: int = 1
    max_requests_per_second: float = 2.0
    request_timeout_seconds: float = 30.0


class StateConfig(Section):
    transport: Literal["mcp", "direct"] = "mcp"
    """How the controller reaches the roles' shared state (tree nodes, beams, rejections):
    through the MCP state server in process, or by calling the state service directly."""


class EvaluationConfig(Section):
    window_start: date = date(2026, 7, 1)
    horizons_days: list[int] = [7, 3, 1]
    systems: list[str] = ["market", "prompt_only", "single_pass", "pipeline"]
    bootstrap_samples: int = 2000
    seed: int = 20261124
    reliability_bins: int = 10
    canary_text: str = (
        "CANARY-7f3a91 this document is dated after every forecast and must never be retrieved"
    )


class PathsConfig(Section):
    data_dir: str = "data"
    runs_dir: str = "runs"

    def state_db(self) -> Path:
        return Path(self.data_dir) / "state.sqlite"

    def checkpoint_db(self) -> Path:
        return Path(self.data_dir) / "checkpoints.sqlite"

    def snapshot_dir(self) -> Path:
        return Path(self.data_dir) / "snapshot"

    def corpus_dir(self) -> Path:
        return Path(self.data_dir) / "corpus"

    def index_dir(self) -> Path:
        return Path(self.data_dir) / "index"

    def cache_dir(self) -> Path:
        return Path(self.data_dir) / "cache"


class Config(Section):
    follow: FollowConfig = FollowConfig()
    sources: SourcesConfig = SourcesConfig()
    triggers: TriggerConfig = TriggerConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    models: ModelsConfig = ModelsConfig()
    budget: BudgetConfig = BudgetConfig()
    forecast: ForecastConfig = ForecastConfig()
    sizing: SizingConfig = SizingConfig()
    caps: CapsConfig = CapsConfig()
    routing: RoutingConfig = RoutingConfig()
    oversight: OversightConfig = OversightConfig()
    stages: StageConfig = StageConfig()
    execution: ExecutionConfig = ExecutionConfig()
    kalshi: KalshiConfig = KalshiConfig()
    state: StateConfig = StateConfig()
    evaluation: EvaluationConfig = EvaluationConfig()
    paths: PathsConfig = PathsConfig()

    @model_validator(mode="after")
    def _consistent(self) -> Config:
        problems: list[str] = []
        caps, sizing, forecast = self.caps, self.sizing, self.forecast

        def fraction(name: str, value: Decimal | float, *, allow_one: bool = True) -> None:
            upper_ok = value <= 1 if allow_one else value < 1
            if not (value > 0 and upper_ok):
                problems.append(f"{name} must be a fraction in (0, 1], got {value}")

        fraction("caps.per_market_fraction", caps.per_market_fraction)
        fraction("caps.per_event_fraction", caps.per_event_fraction)
        fraction("caps.daily_loss_stop_fraction", caps.daily_loss_stop_fraction)
        fraction("caps.drawdown_halt_fraction", caps.drawdown_halt_fraction)
        fraction("caps.risk_unit_fraction", caps.risk_unit_fraction)
        fraction("sizing.kelly_fraction", sizing.kelly_fraction)
        if caps.per_market_fraction > caps.per_event_fraction:
            problems.append("caps.per_market_fraction cannot exceed caps.per_event_fraction")
        if caps.daily_loss_stop_fraction > caps.drawdown_halt_fraction:
            problems.append(
                "caps.daily_loss_stop_fraction cannot exceed caps.drawdown_halt_fraction"
            )
        if caps.max_contracts_per_order < 1:
            problems.append("caps.max_contracts_per_order must be at least 1")
        if caps.max_contracts_per_order >= caps.order_group_contracts_limit:
            problems.append(
                "caps.max_contracts_per_order must stay below caps.order_group_contracts_limit, "
                "so a single order never trips the exchange-side breaker"
            )
        if caps.liquidity_floor_dollars < 0 or caps.liquidity_band < 0:
            problems.append(
                "caps.liquidity_floor_dollars and caps.liquidity_band cannot be negative"
            )
        if not (Decimal("0") < sizing.min_edge < Decimal("1")):
            problems.append("sizing.min_edge must be between 0 and 1 dollar")
        if not (1 <= forecast.max_depth <= 3):
            problems.append("forecast.max_depth must be 1, 2 or 3")
        if forecast.beam_width < 1 or forecast.max_thoughts_per_node < 1:
            problems.append(
                "forecast.beam_width and forecast.max_thoughts_per_node must be at least 1"
            )
        if not (0 < forecast.coverage_floor <= 1):
            problems.append("forecast.coverage_floor must be in (0, 1]")
        if self.budget.max_calls_per_episode < 4:
            problems.append(
                "budget.max_calls_per_episode must allow at least matching, research and one tree level"
            )
        if self.budget.max_usd_per_episode > self.budget.max_usd_per_run:
            problems.append("budget.max_usd_per_episode cannot exceed budget.max_usd_per_run")
        if self.execution.notional_bankroll <= 0:
            problems.append("execution.notional_bankroll must be positive")
        if not (0 < self.execution.backtest_volume_share <= 1):
            problems.append("execution.backtest_volume_share must be in (0, 1]")
        tickers = [s.ticker for s in self.follow.series]
        if len(set(tickers)) != len(tickers):
            problems.append("follow.series lists a ticker twice")
        if not self.follow.enabled_series():
            problems.append("follow.series has no enabled series")
        for s in self.follow.series:
            if not (0 <= s.shrinkage <= 1):
                problems.append(f"follow.series[{s.ticker}].shrinkage must be in [0, 1]")
            if s.outcome_kind == "ladder" and not s.increment:
                problems.append(f"follow.series[{s.ticker}] is a ladder and needs an increment")
            if not s.primary_sources:
                problems.append(f"follow.series[{s.ticker}] needs at least one primary source")
        for slot in ModelSlot:
            assignment = self.models.slots.get(slot)
            if assignment is None:
                problems.append(f"models.slots is missing {slot.value}")
            elif assignment.model not in self.models.catalog:
                problems.append(
                    f"models.catalog has no entry for {assignment.model} (slot {slot.value})"
                )
        if not (1 <= self.kalshi.demo_subaccount <= 63):
            problems.append("kalshi.demo_subaccount must be between 1 and 63")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def full_tree_calls(self) -> int:
        """Most generator plus critic calls a full tree can make at depth 3.

        One parent at the first level, then the beam plus the protected slot at
        each of the next two, with one generator and one critic call per parent.
        """
        parents = 1 + 2 * (self.forecast.beam_width + 1)
        return 2 * parents


CONFIG_ENV = "PMFA_CONFIG"
DEFAULT_CONFIG_FILE = "pmfa.toml"


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load and validate the configuration.

    Order: built-in defaults, then the TOML file (``path``, else ``$PMFA_CONFIG``,
    else ``./pmfa.toml`` when it exists), then ``overrides``.
    """
    chosen = path or os.environ.get(CONFIG_ENV)
    if chosen is None and Path(DEFAULT_CONFIG_FILE).exists():
        chosen = DEFAULT_CONFIG_FILE
    data: dict[str, Any] = {}
    if chosen is not None:
        file = Path(chosen)
        if not file.exists():
            raise ConfigError(f"configuration file not found: {file}")
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{file}: {exc}") from exc
    if overrides:
        data = _merge(data, overrides)
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def default_config() -> Config:
    return Config()


__all__ = ["CONFIG_ENV", "Config", "Effort", "default_config", "load_config"]
