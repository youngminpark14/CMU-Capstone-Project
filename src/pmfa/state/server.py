"""The MCP state server: the state service's operations as MCP tools.

Every tool is one ``StateService`` method of the same name, with the same
arguments and a structured result. Read tools carry the read-only annotation.
Two resources let any MCP client browse episodes: ``episode://{episode_id}``
and ``episode://{episode_id}/trace``.

The same code runs on both MCP SDK lines; what differs lives in ``mcp_compat``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from pmfa.errors import PmfaError
from pmfa.records.episode import TraceStep
from pmfa.state import mcp_compat
from pmfa.state.service import READ_OPERATIONS, WRITE_OPERATIONS, StateService

SERVER_NAME = "pmfa-state"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "State of the pmfa forecasting agent (mock funds only): episodes and their traces, "
    "scenario-tree nodes and beams, critic rejections, the ledger and calibration, the halt "
    "flag and the stage. Searches take a required as_of time and never return anything "
    "published or resolved after it."
)

_SHOWN_ERRORS = (PmfaError, ValueError, LookupError)
"""Failures a caller can act on. Their class name and message reach the caller."""

_TRACE = TypeAdapter(list[TraceStep])


def error_message(exc: BaseException) -> str:
    """The text a tool error carries: ``[ClassName] message``, which the client maps back."""
    return f"[{type(exc).__name__}] {exc}"


def _description(method: Callable[..., Any]) -> str:
    return inspect.cleandoc(method.__doc__ or "").strip()


def _title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def _as_tool(method: Callable[..., Any], tool_error: type[Exception]) -> Callable[..., Any]:
    """Wrap a bound service method as a tool function with the method's own signature."""

    @functools.wraps(method)
    def tool(**kwargs: Any) -> Any:
        try:
            return method(**kwargs)
        except _SHOWN_ERRORS as exc:
            raise tool_error(error_message(exc)) from exc

    # The SDKs build the argument and output schemas from this signature.
    tool.__signature__ = inspect.signature(method, eval_str=True)  # type: ignore[attr-defined]
    return tool


def build_server(service: StateService) -> Any:
    """An MCP server (``MCPServer`` on SDK 2.x, ``FastMCP`` on 1.x) over ``service``.

    A factory, not a module-level object, so each test, the controller, and the
    stdio entry point hand in their own service and database.
    """
    server = mcp_compat.new_server(SERVER_NAME, instructions=INSTRUCTIONS, version=SERVER_VERSION)
    tool_error = mcp_compat.tool_error_class()
    for name in WRITE_OPERATIONS + READ_OPERATIONS:
        method = getattr(service, name)
        read_only = name in READ_OPERATIONS
        mcp_compat.add_tool(
            server,
            _as_tool(method, tool_error),
            name=name,
            title=_title(name),
            description=_description(method),
            annotations=mcp_compat.tool_annotations(read_only=read_only, title=_title(name)),
        )

    def episode(episode_id: str) -> str:
        try:
            return service.get_episode(episode_id).model_dump_json()
        except PmfaError as exc:
            raise mcp_compat.resource_not_found(error_message(exc)) from exc

    def trace(episode_id: str) -> str:
        return _TRACE.dump_json(service.get_trace(episode_id)).decode()

    mcp_compat.add_resource(
        server,
        "episode://{episode_id}",
        episode,
        name="episode",
        title="Episode",
        description="One episode record with its full ordered trace, as JSON.",
    )
    mcp_compat.add_resource(
        server,
        "episode://{episode_id}/trace",
        trace,
        name="episode_trace",
        title="Episode trace",
        description="An episode's trace steps (thought, action, observation) in order, as JSON.",
    )
    return server
