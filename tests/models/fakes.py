"""A fake backend that answers from a list and remembers every request it got."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pmfa.models.backend import Completion
from pmfa.records.base import Record


def ok(record: Record, *, tokens: tuple[int, int] = (1000, 200), model: str = "") -> Completion:
    """A completion that parsed."""
    return Completion(
        parsed=record,
        raw_text=record.model_dump_json(),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        served_model=model,
        stop_reason="end_turn",
    )


def raw(
    text: str, *, stop_reason: str = "end_turn", tokens: tuple[int, int] = (1000, 200)
) -> Completion:
    """A completion that did not parse (or was refused, or truncated)."""
    return Completion(
        parsed=None,
        raw_text=text,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        served_model="",
        stop_reason=stop_reason,
    )


@dataclass
class FakeBackend:
    """Answers each request with the next completion (or the result of calling it)."""

    answers: list[Completion | Callable[[dict[str, Any]], Completion]]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, **request: Any) -> Completion:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("the fake backend ran out of answers")
        answer = self.answers.pop(0)
        completion = answer(request) if callable(answer) else answer
        if not completion.served_model:
            completion = Completion(**{**completion.__dict__, "served_model": request["model"]})
        return completion
