"""Halt: one persisted flag that blocks every order, and the conditions that set it.

``halt`` sets the flag in the state store and cancels all open orders;
``resume`` clears it. The flag lives in the database, so it survives restarts,
and the gate and the paper executor check it before every order.

``stop_reasons`` lists the self-halt conditions that hold now; ``enforce``
halts when any does. The conditions, each reported as a short reason that
starts with its name:

- ``daily loss stop``: equity fell by ``caps.daily_loss_stop_fraction`` of the
  day's starting equity at some point today (``pmfa.decision.risk_state``).
- ``drawdown``: equity is ``caps.drawdown_halt_fraction`` or more below its peak.
- ``fill failures``: among the last ``oversight.fill_failure_window`` orders
  (once that many exist), more than ``oversight.fill_failure_halt_fraction`` of
  them were ``UNFILLED`` or ``REJECTED`` (with the defaults: two or more in ten).
  An order counts only once its outcome is known (``outcome_at``, else
  ``placed_at``, at or before now): a backtest order's outcome comes from a
  candle after it was sent.
- ``source changed`` / ``source withdrawn``: a document cited by an episode
  that holds an open position (its passages, or its trigger document) has a
  remembered content hash in the store, and its source now gives a different
  hash for it, or no longer gives it at all. When the Evidence port has a
  ``recheck(doc_id, as_of=...)`` method (the live modes' ``LiveEvidence``,
  which fetches the page again because its stored copy never changes), that
  is asked; otherwise ``get_document`` (recorded and frozen worlds). Documents
  the store never remembered cannot be compared and are skipped. As for fill
  failures, an order counts only once its outcome is known.
- ``calibration``: for a category, the calibration error of the pipeline's
  resolved forecasts (``store.calibration``) is above
  ``routing.calibration_error_max`` at every weekly checkpoint ``now - k weeks``
  for ``k = 0 .. oversight.calibration_halt_weeks``, each checkpoint counting
  the forecasts resolved before it and holding at least
  ``routing.calibration_min_resolved`` of them. With the default of two weeks
  that is three checkpoints: two weeks ago, one week ago, and now.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pmfa.config import Config
from pmfa.decision import risk_state
from pmfa.errors import NotFound
from pmfa.execution.portfolio import account
from pmfa.ports import Clock, Evidence, Executor, StateStore
from pmfa.records.decision import OrderStatus
from pmfa.world.evidence.common import content_hash

FAILED_ORDERS = frozenset({OrderStatus.UNFILLED, OrderStatus.REJECTED})


def halt(store: StateStore, executor: Executor, clock: Clock, reason: str) -> int:
    """Set the persisted halt flag, then cancel every open order. Returns how many were cancelled."""
    store.set_halt(reason, clock.now())
    return executor.cancel_all()


def resume(store: StateStore) -> None:
    store.clear_halt()


def stop_reasons(
    store: StateStore,
    config: Config,
    clock: Clock,
    evidence: Evidence,
    *,
    bankroll: Decimal | None = None,
) -> list[str]:
    """The self-halt conditions that hold now, one short reason each (see the module docstring).

    ``bankroll`` is the current equity, passed on to ``risk_state`` (where it is
    the dollar caps' base and does not move either stop) and used to seed the
    account that finds open positions; it defaults to the last equity point and
    to ``config.execution.notional_bankroll`` respectively. The daily loss stop
    is always a fraction of the day's starting equity
    (``RiskState.day_start_equity``), whatever ``bankroll`` is.
    """
    now = clock.now()
    reasons: list[str] = []

    series = store.equity_series()
    if series:
        risk = risk_state(series, now, config, bankroll=bankroll)
        if risk.loss_stop_tripped:
            fraction = config.caps.daily_loss_stop_fraction
            reasons.append(
                f"daily loss stop: equity fell {fraction * risk.day_start_equity} or more "
                f"({fraction} of the day's starting equity {risk.day_start_equity}) "
                f"below that start today; day P&L now {risk.day_pnl}"
            )
        if risk.drawdown_tripped:
            reasons.append(
                f"drawdown: {risk.drawdown_fraction:.4f} below the peak "
                f"{risk.peak_equity}, limit {config.caps.drawdown_halt_fraction}"
            )

    window = config.oversight.fill_failure_window
    orders = [o for o in store.list_orders() if (o.outcome_at or o.placed_at) <= now]
    if window > 0 and len(orders) >= window:
        recent = orders[-window:]
        failed = sum(o.status in FAILED_ORDERS for o in recent)
        if failed > config.oversight.fill_failure_halt_fraction * window:
            reasons.append(f"fill failures: {failed} of the last {window} orders did not fill")

    reasons.extend(_source_reasons(store, evidence, now, bankroll=bankroll, config=config))
    reasons.extend(_calibration_reasons(store, config, now))
    return reasons


def enforce(
    store: StateStore,
    executor: Executor,
    config: Config,
    clock: Clock,
    evidence: Evidence,
    *,
    bankroll: Decimal | None = None,
) -> list[str]:
    """Halt when any stop condition holds, and return the reasons.

    ``bankroll`` is the current equity, as for :func:`stop_reasons`; it does not
    change the daily loss stop, which is a fraction of the day's starting equity.
    An agent that is already halted keeps its original reason and time.
    """
    reasons = stop_reasons(store, config, clock, evidence, bankroll=bankroll)
    if reasons and not store.get_halt().halted:
        halt(store, executor, clock, "; ".join(reasons))
    return reasons


def doc_id_of_passage(passage_id: str) -> str:
    """The document behind a passage id (``pmfa.knowledge.chunking`` writes ``{doc_id}#{n}``)."""
    doc_id, sep, _ = passage_id.rpartition("#")
    return doc_id if sep else passage_id


def _source_reasons(
    store: StateStore,
    evidence: Evidence,
    now: datetime,
    *,
    bankroll: Decimal | None,
    config: Config,
) -> list[str]:
    acct = account(
        store.list_fills(),
        store.list_settlements(),
        bankroll=bankroll if bankroll is not None else config.execution.notional_bankroll,
        as_of=now,
    )
    open_markets = {h.market_ticker for h in acct.holdings if h.contracts > 0}
    if not open_markets:
        return []
    episode_ids = dict.fromkeys(
        o.episode_id
        for o in store.list_orders()
        if (o.outcome_at or o.placed_at) <= now and o.market_ticker in open_markets and o.fills
    )
    cited: dict[str, None] = {}
    for episode_id in episode_ids:
        episode = store.get_episode(episode_id)
        if episode is None:
            continue
        ids = list(episode.passage_ids)
        if episode.forecast is not None:
            ids.extend(episode.forecast.passage_ids)
        for pid in ids:
            cited.setdefault(doc_id_of_passage(pid))
        if episode.trigger.doc_id:
            cited.setdefault(episode.trigger.doc_id)

    # The live port's stored copy never changes, so it is asked to fetch the source again.
    recheck = getattr(evidence, "recheck", None)
    read = recheck if callable(recheck) else evidence.get_document
    reasons = []
    for doc_id in cited:
        remembered = store.document_hash(doc_id)
        if remembered is None:
            continue
        try:
            doc = read(doc_id, as_of=now)
        except NotFound:
            reasons.append(f"source withdrawn: {doc_id} behind an open position is gone")
            continue
        current = doc.content_hash or content_hash(doc.text)
        if current != remembered:
            reasons.append(f"source changed: {doc_id} behind an open position has new content")
    return reasons


def _calibration_reasons(store: StateStore, config: Config, now: datetime) -> list[str]:
    weeks = config.oversight.calibration_halt_weeks
    limit = config.routing.calibration_error_max
    minimum = config.routing.calibration_min_resolved
    categories = dict.fromkeys(e.category for e in store.ledger(system="pipeline"))
    reasons = []
    for category in categories:
        errors = []
        for k in range(weeks + 1):
            cal = store.calibration(category, as_of=now - timedelta(weeks=k))
            if cal.calibration_error is None or cal.n_resolved < minimum:
                break
            if cal.calibration_error <= limit:
                break
            errors.append(cal.calibration_error)
        else:
            reasons.append(
                f"calibration: {category} error above {limit} for {weeks} weeks "
                f"(now {errors[0]:.3f})"
            )
    return reasons


__all__ = ["FAILED_ORDERS", "doc_id_of_passage", "enforce", "halt", "resume", "stop_reasons"]
