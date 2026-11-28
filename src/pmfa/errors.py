"""Exceptions that cross module boundaries."""

from __future__ import annotations


class PmfaError(Exception):
    """Base class for the project's own errors."""


class ConfigError(PmfaError):
    """The configuration is invalid or inconsistent."""


class BudgetExhausted(PmfaError):
    """The episode's cap on model calls, seconds, or dollars was reached.

    The forecast step catches this and aggregates the scenarios scored so far.
    """


class RunBudgetExhausted(PmfaError):
    """The run's dollar cap was reached. The run stops; nothing more is sent."""


class ModelOutputError(PmfaError):
    """A model answer failed validation twice (the answer and its one repair retry),
    or the model refused. The episode exits ``error`` and places no order."""


class CassetteMiss(PmfaError):
    """Replay asked for a model call that the cassette does not hold, or with a
    different input than was recorded. The replay is not faithful, so it stops."""


class AsOfViolation(PmfaError):
    """Something tried to read data published or priced after the as-of time."""


class ExchangeError(PmfaError):
    """The exchange refused or failed a request."""


class NotFound(PmfaError):
    """A requested series, event, market, document, or record does not exist."""
