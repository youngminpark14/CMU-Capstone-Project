"""Clients of the state service: an in-process MCP client, a sync bridge, and ``StateClient``.

``connect_in_memory(server)`` opens an in-process MCP client on either SDK line
and returns plain JSON results. ``SyncBridge`` runs async calls on one
dedicated event-loop thread, so synchronous code (the controller, tests) can
use the async client safely, even from inside another running loop.

``StateClient`` has the same method names as ``StateService`` and comes in two
flavours, so the controller can be configured either way:

- ``StateClient.direct(service)`` calls the service in-process;
- ``StateClient.mcp(service_or_server)`` calls the MCP tools through the
  in-memory client and turns the JSON back into the same records.
"""

from __future__ import annotations

import re
import typing
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager
from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import TypeAdapter

from pmfa.errors import AsOfViolation, NotFound, PmfaError
from pmfa.records.episode import (
    Calibration,
    EpisodeRecord,
    EpisodeStatus,
    HaltState,
    LedgerEntry,
    StageState,
    TraceStep,
)
from pmfa.records.evidence import EventHit, LedgerHit, Passage, RetrievedPassage
from pmfa.records.forecast import TreeNode
from pmfa.records.roles import RejectionBrief
from pmfa.state import mcp_compat
from pmfa.state.service import Beam, EventRules, PortUnavailable, StateService

T = TypeVar("T")


class ToolCallError(PmfaError):
    """A state tool failed with an error the client does not map to a project exception."""


_KNOWN_ERRORS: dict[str, type[Exception]] = {
    "NotFound": NotFound,
    "PortUnavailable": PortUnavailable,
    "AsOfViolation": AsOfViolation,
    "ValueError": ValueError,
}
_ERROR_PATTERN = re.compile(r"\[(\w+)\] (.*)", re.DOTALL)


def _raise_tool_error(name: str, text: str) -> typing.NoReturn:
    match = _ERROR_PATTERN.search(text)
    if match and match.group(1) in _KNOWN_ERRORS:
        raise _KNOWN_ERRORS[match.group(1)](match.group(2))
    raise ToolCallError(f"tool {name!r} failed: {text}")


def _is_wrapped(tool: mcp_compat.ToolInfo) -> bool:
    """True when the SDK wrapped a non-object return value as ``{"result": ...}``."""
    schema = tool.output_schema or {}
    return (
        set(schema.get("properties", {})) == {"result"}
        and schema.get("required") == ["result"]
        and str(schema.get("title", "")).endswith("Output")
    )


class PlainClient:
    """An MCP client session whose calls return plain JSON values."""

    def __init__(self, raw: mcp_compat.RawClient, tools: list[mcp_compat.ToolInfo]) -> None:
        self._raw = raw
        self.tools = {tool.name: tool for tool in tools}

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a tool and return its structured result, unwrapped. Tool errors raise."""
        outcome = await self._raw.call_tool(name, arguments or {})
        if outcome.is_error:
            _raise_tool_error(name, outcome.text)
        tool = self.tools.get(name)
        structured = outcome.structured
        if tool is not None and _is_wrapped(tool) and isinstance(structured, dict):
            return structured["result"]
        return structured

    async def read_resource(self, uri: str) -> str:
        """A resource's text."""
        return await self._raw.read_resource(uri)

    async def list_tools(self) -> list[mcp_compat.ToolInfo]:
        return await self._raw.list_tools()

    async def list_resource_templates(self) -> list[str]:
        return await self._raw.list_resource_templates()


@asynccontextmanager
async def connect_in_memory(server: Any) -> AsyncIterator[PlainClient]:
    """An in-process client to ``server`` (``MCPServer`` or ``FastMCP``) returning plain results."""
    async with mcp_compat.connect_in_memory(server) as raw:
        yield PlainClient(raw, await raw.list_tools())


@asynccontextmanager
async def connect_stdio(
    command: str, args: list[str], *, env: dict[str, str] | None = None
) -> AsyncIterator[PlainClient]:
    """A client to a state server launched as a subprocess over stdio."""
    async with mcp_compat.connect_stdio(command, args, env=env) as raw:
        yield PlainClient(raw, await raw.list_tools())


class SyncBridge:
    """Runs coroutines on one dedicated event-loop thread (an anyio blocking portal).

    Synchronous code calls ``bridge.call(async_fn, *args)``; it never starts a
    loop of its own, so it is safe even when the caller's thread already runs one.
    """

    def __init__(self) -> None:
        from anyio.from_thread import start_blocking_portal

        self._stack = ExitStack()
        self._portal = self._stack.enter_context(start_blocking_portal())

    def call(self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        return self._portal.call(fn, *args)

    def enter(self, context: AbstractAsyncContextManager[T]) -> T:
        """Enter an async context manager on the loop thread; it is exited on ``close``."""
        return self._stack.enter_context(self._portal.wrap_async_context_manager(context))

    def close(self) -> None:
        self._stack.close()


class _McpInvoker:
    """Calls service operations as MCP tools, converting records to JSON and back."""

    def __init__(self, server: Any) -> None:
        self._bridge = SyncBridge()
        try:
            self._client = self._bridge.enter(connect_in_memory(server))
        except BaseException:
            self._bridge.close()
            raise
        self._adapters: dict[str, tuple[dict[str, TypeAdapter[Any]], TypeAdapter[Any]]] = {}

    def _adapters_for(self, name: str) -> tuple[dict[str, TypeAdapter[Any]], TypeAdapter[Any]]:
        if name not in self._adapters:
            hints = typing.get_type_hints(getattr(StateService, name))
            result = TypeAdapter(hints.pop("return"))
            self._adapters[name] = ({k: TypeAdapter(v) for k, v in hints.items()}, result)
        return self._adapters[name]

    def __call__(self, name: str, kwargs: dict[str, Any]) -> Any:
        params, result = self._adapters_for(name)
        arguments = {k: params[k].dump_python(v, mode="json") for k, v in kwargs.items()}
        return result.validate_python(self._bridge.call(self._client.call_tool, name, arguments))

    def close(self) -> None:
        self._bridge.close()


class StateClient:
    """The state service's operations, called directly or through the MCP server.

    Both flavours take the same arguments and return the same records, and
    raise ``NotFound``, ``PortUnavailable``, or ``ValueError`` alike.
    """

    def __init__(
        self,
        invoke: Callable[[str, dict[str, Any]], Any],
        *,
        flavour: Literal["direct", "mcp"],
        close: Callable[[], None] | None = None,
    ) -> None:
        self._invoke = invoke
        self.flavour = flavour
        self._close = close

    @classmethod
    def direct(cls, service: StateService) -> StateClient:
        """Calls ``service`` in-process."""

        def invoke(name: str, kwargs: dict[str, Any]) -> Any:
            return getattr(service, name)(**kwargs)

        return cls(invoke, flavour="direct")

    @classmethod
    def mcp(cls, target: StateService | Any) -> StateClient:
        """Calls the MCP tools of ``target`` (a server, or a service to build one over).

        Opens an in-memory client on a dedicated loop thread; ``close`` ends it.
        """
        if isinstance(target, StateService):
            from pmfa.state.server import build_server

            target = build_server(target)
        invoker = _McpInvoker(target)
        return cls(invoker, flavour="mcp", close=invoker.close)

    def close(self) -> None:
        if self._close is not None:
            self._close()
            self._close = None

    def __enter__(self) -> StateClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- scenario tree

    def put_node(self, node: TreeNode) -> TreeNode:
        return self._invoke("put_node", {"node": node})

    def get_node(self, episode_id: str, node_id: str) -> TreeNode:
        return self._invoke("get_node", {"episode_id": episode_id, "node_id": node_id})

    def get_nodes(self, episode_id: str, depth: int | None = None) -> list[TreeNode]:
        return self._invoke("get_nodes", {"episode_id": episode_id, "depth": depth})

    def get_beam(self, episode_id: str) -> Beam:
        return self._invoke("get_beam", {"episode_id": episode_id})

    def record_rejection(
        self, episode_id: str, event_ticker: str, claim: str, reason: str
    ) -> RejectionBrief:
        return self._invoke(
            "record_rejection",
            {
                "episode_id": episode_id,
                "event_ticker": event_ticker,
                "claim": claim,
                "reason": reason,
            },
        )

    def get_rejections(self, event_ticker: str) -> list[RejectionBrief]:
        return self._invoke("get_rejections", {"event_ticker": event_ticker})

    # -- rules and retrieval

    def get_rules(self, event_ticker: str) -> EventRules:
        return self._invoke("get_rules", {"event_ticker": event_ticker})

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        return self._invoke("get_passages", {"passage_ids": passage_ids, "as_of": as_of})

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        return self._invoke("search_events", {"query": query, "as_of": as_of, "k": k})

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        return self._invoke(
            "search_evidence",
            {
                "queries": queries,
                "as_of": as_of,
                "category": category,
                "k": k,
                "published_after": published_after,
            },
        )

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        return self._invoke(
            "search_ledger", {"query": query, "as_of": as_of, "category": category, "k": k}
        )

    # -- ledger and calibration

    def query_ledger(
        self,
        resolved_before: datetime | None = None,
        category: str | None = None,
        system: str | None = None,
    ) -> list[LedgerEntry]:
        return self._invoke(
            "query_ledger",
            {"resolved_before": resolved_before, "category": category, "system": system},
        )

    def calibration(self, category: str, *, as_of: datetime) -> Calibration:
        return self._invoke("calibration", {"category": category, "as_of": as_of})

    # -- episodes

    def list_episodes(
        self,
        status: EpisodeStatus | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
    ) -> list[EpisodeRecord]:
        return self._invoke(
            "list_episodes", {"status": status, "event_ticker": event_ticker, "limit": limit}
        )

    def get_episode(self, episode_id: str) -> EpisodeRecord:
        return self._invoke("get_episode", {"episode_id": episode_id})

    def get_trace(self, episode_id: str) -> list[TraceStep]:
        return self._invoke("get_trace", {"episode_id": episode_id})

    # -- oversight

    def get_halt(self) -> HaltState:
        return self._invoke("get_halt", {})

    def get_stage(self) -> StageState:
        return self._invoke("get_stage", {})
