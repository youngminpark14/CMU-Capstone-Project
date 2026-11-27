"""Backends: the one place a prompt becomes a request to a model.

A backend takes a system text, a user text, and the output record type, and
returns a ``Completion``: the parsed record when the answer parsed, else the raw
text, plus usage, the model that actually served the answer, and the stop
reason. Validation, repair, budgets, caching, and cassettes live in the gateway,
not here.

``AnthropicBackend`` goes through LangChain's ``ChatAnthropic`` with native
JSON-schema structured output. Facts checked against the pinned versions
(langchain-anthropic 1.7.12, anthropic 2.14):

- Effort goes in ``output_config.effort``. It is passed through the
  ``output_config`` field rather than ChatAnthropic's ``effort`` field, because
  the ``effort`` field also makes LangChain add an adaptive ``thinking`` block,
  and this project sends no thinking parameter.
- ``temperature``, ``top_p`` and ``top_k`` are left unset, so none is sent.
- The server-side refusal fallback is the beta header
  ``server-side-fallback-2026-07-01`` with ``fallbacks: "default"``. Claude
  Haiku 5.5 has no server-side fallback (the API returns a 400 for a list and
  ignores ``"default"``), so it is sent only for the models listed in
  ``SERVER_FALLBACK_MODELS``.
- The answer's top-level ``model`` names the model that served it, which
  differs from the requested one after a fallback.
- A key that is not scoped to one workspace must name the workspace on every
  request in the ``anthropic-workspace-id`` header; the API answers 400
  without it (Claude API reference, 2026-11-27). A key created for one
  workspace needs nothing. The SDK reads ``ANTHROPIC_WORKSPACE_ID`` only for
  profile credentials, so the backend sends the header itself when that
  variable is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from pmfa.config import Config, ModelsConfig
from pmfa.errors import ConfigError
from pmfa.records.base import Record

FALLBACK_BETA = "server-side-fallback-2026-07-01"

WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"
"""Optional: the workspace id to send with a key that is not scoped to one workspace."""
WORKSPACE_HEADER = "anthropic-workspace-id"

SERVER_FALLBACK_MODELS: frozenset[str] = frozenset(
    {
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
    }
)
"""Models whose documentation lists the server-side refusal fallback (Claude API reference, 2026-11)."""


@dataclass(frozen=True)
class Completion:
    """One model answer as the backend saw it."""

    parsed: Record | None
    """The answer parsed as the output record, or None when it did not parse or validate."""
    raw_text: str
    """The answer's text, kept so a failed parse can be shown back to the model in the repair retry."""
    input_tokens: int
    output_tokens: int
    served_model: str
    stop_reason: str
    """``end_turn`` normally; ``refusal`` and ``max_tokens`` mean the answer cannot be used."""


class Backend(Protocol):
    """Sends one prompt to one model and returns what came back."""

    def complete(
        self,
        *,
        system: str,
        user: str,
        output_type: type[Record],
        model: str,
        effort: str,
        max_tokens: int,
        timeout: float,
    ) -> Completion: ...


class MissingApiKey(ConfigError):
    """The environment variable that should hold the Anthropic API key is not set."""


class AnthropicBackend:
    """The Claude API through ``langchain_anthropic.ChatAnthropic``.

    The API key is read from the environment variable named in
    ``config.models.api_key_env`` at call time, handed to the client, and never
    logged or stored on this object. ``http_client`` exists so tests can give
    the SDK a transport that captures requests; in normal use it is None.
    """

    def __init__(
        self,
        models: ModelsConfig,
        *,
        max_retries: int = 2,
        http_client: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key_env = models.api_key_env
        self.refusal_fallback = models.refusal_fallback
        self.max_retries = max_retries
        self._http_client = http_client
        self._base_url = base_url

    @classmethod
    def from_config(cls, config: Config, **kwargs: Any) -> AnthropicBackend:
        return cls(config.models, **kwargs)

    def _api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key.strip():
            raise MissingApiKey(
                f"no Anthropic API key: set the environment variable {self.api_key_env} "
                "(configured as models.api_key_env)"
            )
        return key

    def uses_fallback(self, model: str) -> bool:
        return self.refusal_fallback == "default" and model in SERVER_FALLBACK_MODELS

    def complete(
        self,
        *,
        system: str,
        user: str,
        output_type: type[Record],
        model: str,
        effort: str,
        max_tokens: int,
        timeout: float,
    ) -> Completion:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "output_config": {"effort": effort},
            "timeout": timeout,
            "max_retries": self.max_retries,
            "api_key": self._api_key(),
        }
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        workspace = os.environ.get(WORKSPACE_ENV, "").strip()
        if workspace:
            kwargs["default_headers"] = {WORKSPACE_HEADER: workspace}
        if self.uses_fallback(model):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["model_kwargs"] = {"fallbacks": "default"}
        llm = ChatAnthropic(**kwargs)
        if self._http_client is not None:
            import anthropic

            # ChatAnthropic builds its SDK client lazily in a cached property;
            # replacing it is how a test transport reaches the wire layer.
            llm._client = anthropic.Client(**llm._client_params, http_client=self._http_client)
        structured = llm.with_structured_output(output_type, method="json_schema", include_raw=True)
        result = structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        raw = result["raw"]
        parsed = result.get("parsed")
        metadata = raw.response_metadata or {}
        usage = raw.usage_metadata or {}
        return Completion(
            parsed=parsed if isinstance(parsed, output_type) else None,
            raw_text=raw.text,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            served_model=str(metadata.get("model") or metadata.get("model_name") or model),
            stop_reason=str(metadata.get("stop_reason") or ""),
        )


__all__ = [
    "FALLBACK_BETA",
    "SERVER_FALLBACK_MODELS",
    "WORKSPACE_ENV",
    "WORKSPACE_HEADER",
    "AnthropicBackend",
    "Backend",
    "Completion",
    "MissingApiKey",
]
