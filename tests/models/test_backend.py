"""The Anthropic backend's wire requests, captured by a transport that never leaves the process."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pmfa.models.backend import (
    FALLBACK_BETA,
    WORKSPACE_ENV,
    WORKSPACE_HEADER,
    AnthropicBackend,
    MissingApiKey,
)
from pmfa.models.gateway import Gateway
from pmfa.models.roles.prompts import render
from pmfa.models.roles.runner import DirectRunner
from pmfa.records.roles import TASKS, RoleTask

from .fixtures import INPUTS, OUTPUTS, make_config

httpx2 = pytest.importorskip("httpx2")

KEY_ENV = "PMFA_TEST_ANTHROPIC_KEY"
FAKE_KEY = "test-key-not-real-0123"


def message(text: str | None, *, model: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    """A Messages API response in the documented shape."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [] if text is None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 1234, "output_tokens": 56},
    }


class Capture:
    """An httpx transport handler that records requests and answers from a list."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(
            {
                "path": request.url.path,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(200, json=self.bodies.pop(0))


def backend(capture: Capture, **models: Any) -> AnthropicBackend:
    config = make_config(models={"api_key_env": KEY_ENV, **models})
    client = httpx2.Client(transport=httpx2.MockTransport(capture))
    return AnthropicBackend.from_config(config, http_client=client, max_retries=0)


def complete(b: AnthropicBackend, task: RoleTask, *, model: str, effort: str = "high"):
    prompt = render(task, INPUTS[task])
    return b.complete(
        system=prompt.system,
        user=prompt.user,
        output_type=TASKS[task].output_type,
        model=model,
        effort=effort,
        max_tokens=16000,
        timeout=42.0,
    )


def objects(schema: Any) -> list[dict[str, Any]]:
    """Every object schema inside a JSON schema."""
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found.extend(objects(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(objects(value))
    return found


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)


def test_request_carries_effort_and_json_schema_and_no_sampling_or_thinking():
    out = OUTPUTS[RoleTask.SCORE]
    capture = Capture(message(out.model_dump_json(), model="claude-sonnet-5-5"))
    completion = complete(backend(capture), RoleTask.SCORE, model="claude-sonnet-5-5")

    (request,) = capture.requests
    body = request["body"]
    assert request["path"] == "/v1/messages"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 16000
    assert body["output_config"]["effort"] == "high"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert all(o.get("additionalProperties") is False for o in objects(fmt["schema"]))
    for absent in ("temperature", "top_p", "top_k", "thinking"):
        assert absent not in body
    prompt = render(RoleTask.SCORE, INPUTS[RoleTask.SCORE])
    assert body["system"] == prompt.system
    assert body["messages"] == [{"role": "user", "content": prompt.user}]
    assert request["headers"]["x-stainless-read-timeout"] == "42.0"

    assert completion.parsed == out
    assert (completion.input_tokens, completion.output_tokens) == (1234, 56)
    assert completion.stop_reason == "end_turn"


def test_server_side_fallback_is_requested_for_models_that_support_it():
    out = OUTPUTS[RoleTask.BASELINE].model_dump_json()
    capture = Capture(message(out, model="claude-opus-4-8"))
    completion = complete(backend(capture), RoleTask.BASELINE, model="claude-fable-5-2")
    request = capture.requests[0]
    assert FALLBACK_BETA in request["headers"]["anthropic-beta"]
    assert request["body"]["fallbacks"] == "default"
    assert completion.served_model == "claude-opus-4-8"


def test_no_fallback_for_haiku_or_when_turned_off():
    out = OUTPUTS[RoleTask.MATCH].model_dump_json()
    haiku = Capture(message(out, model="claude-haiku-5-5"))
    complete(backend(haiku), RoleTask.MATCH, model="claude-haiku-5-5", effort="low")
    off = Capture(message(out, model="claude-sonnet-5-5"))
    complete(backend(off, refusal_fallback="off"), RoleTask.MATCH, model="claude-sonnet-5-5")
    for capture in (haiku, off):
        request = capture.requests[0]
        assert "fallbacks" not in request["body"]
        assert FALLBACK_BETA not in request["headers"].get("anthropic-beta", "")
    assert haiku.requests[0]["body"]["output_config"]["effort"] == "low"


def test_the_key_is_read_from_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    capture = Capture(message(OUTPUTS[RoleTask.MATCH].model_dump_json(), model="m"))
    b = backend(capture)
    with pytest.raises(MissingApiKey, match=KEY_ENV):
        complete(b, RoleTask.MATCH, model="claude-haiku-5-5")
    assert capture.requests == []

    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    complete(b, RoleTask.MATCH, model="claude-haiku-5-5")
    headers = capture.requests[0]["headers"]
    assert FAKE_KEY in headers.get("x-api-key", "") + headers.get("authorization", "")
    assert FAKE_KEY not in repr(vars(b))


def test_a_workspace_id_in_the_environment_is_sent_with_every_request(monkeypatch):
    """A key that is not scoped to one workspace is refused unless each request names one."""
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    answer = message(OUTPUTS[RoleTask.MATCH].model_dump_json(), model="m")
    capture = Capture(answer, answer)
    b = backend(capture)
    complete(b, RoleTask.MATCH, model="claude-haiku-5-5")
    assert WORKSPACE_HEADER not in capture.requests[0]["headers"]

    monkeypatch.setenv(WORKSPACE_ENV, "wrkspc_test")
    complete(b, RoleTask.MATCH, model="claude-haiku-5-5")
    assert capture.requests[1]["headers"][WORKSPACE_HEADER] == "wrkspc_test"


def test_a_refusal_comes_back_as_a_stop_reason():
    capture = Capture(message(None, model="claude-sonnet-5-5", stop_reason="refusal"))
    completion = complete(backend(capture), RoleTask.SCORE, model="claude-sonnet-5-5")
    assert completion.parsed is None
    assert completion.stop_reason == "refusal"


@pytest.mark.parametrize("task", list(RoleTask))
def test_every_output_record_round_trips_through_the_wire(task):
    out = OUTPUTS[task]
    capture = Capture(message(out.model_dump_json(), model="claude-sonnet-5-5"))
    completion = complete(backend(capture), task, model="claude-sonnet-5-5")
    assert completion.parsed == out
    schema = capture.requests[0]["body"]["output_config"]["format"]["schema"]
    text = json.dumps(schema)
    for constraint in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
        assert f'"{constraint}"' not in text


def test_gateway_over_the_wire_retries_a_refusal_once():
    out = OUTPUTS[RoleTask.SCORE]
    capture = Capture(
        message(None, model="claude-sonnet-5-5", stop_reason="refusal"),
        message(out.model_dump_json(), model="claude-sonnet-5-5"),
    )
    gw = Gateway(make_config(), runner=DirectRunner(backend(capture)))
    result = gw.call(RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e")
    assert result.output == out and result.meta.repaired
    assert len(capture.requests) == 2
