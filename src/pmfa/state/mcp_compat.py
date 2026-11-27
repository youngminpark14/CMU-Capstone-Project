"""Everything that differs between the two MCP Python SDK lines, in one place.

The default environment pins ``mcp`` 1.x (CrewAI requires ``mcp<2``); the
alternative installs 2.x. The server class is ``MCPServer`` from
``mcp.server`` on 2.x and ``FastMCP`` from ``mcp.server.fastmcp`` on 1.x; their
``tool()`` and ``resource()`` decorators match. The clients, the result field
names, the tool annotations, and the error classes differ, and this module
smooths those over so the rest of ``pmfa.state`` is line-agnostic.

``mcp`` is imported inside functions, so ``import pmfa`` stays fast.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


def sdk_major() -> int:
    """2 when ``mcp.server`` exports ``MCPServer``, else 1."""
    import mcp.server

    return 2 if hasattr(mcp.server, "MCPServer") else 1


def _is_v2() -> bool:
    return sdk_major() == 2


# -- server side


def new_server(name: str, *, instructions: str, version: str) -> Any:
    """A new, empty server object of whichever line is installed."""
    if _is_v2():
        from mcp.server import MCPServer

        return MCPServer(name, instructions=instructions, version=version, log_level="WARNING")
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(name, instructions=instructions, log_level="WARNING")
    # FastMCP has no version argument; the low-level server reports this one.
    server._mcp_server.version = version
    return server


def tool_annotations(*, read_only: bool, title: str | None = None) -> Any:
    """Tool annotations: read-only tools are idempotent; writes are additive and idempotent."""
    from mcp.types import ToolAnnotations

    hints: dict[str, Any] = {
        "title": title,
        "readOnlyHint": read_only,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    if not read_only:
        hints["destructiveHint"] = False
    # Both lines accept the wire (camelCase) names on validation; 2.x renamed the attributes.
    return ToolAnnotations.model_validate(hints)


def tool_error_class() -> type[Exception]:
    """The exception whose message a tool may show its caller (others are masked on 2.x)."""
    if _is_v2():
        from mcp.server.mcpserver.exceptions import ToolError

        return ToolError
    from mcp.server.fastmcp.exceptions import ToolError

    return ToolError


def resource_not_found(message: str) -> Exception:
    """The exception a resource handler raises for an unknown id."""
    if _is_v2():
        from mcp.server.mcpserver.exceptions import ResourceNotFoundError

        return ResourceNotFoundError(message)
    from mcp.server.fastmcp.exceptions import ResourceError

    return ResourceError(message)


def add_tool(
    server: Any,
    fn: Callable[..., Any],
    *,
    name: str,
    title: str,
    description: str,
    annotations: Any,
) -> None:
    """Register ``fn`` as a tool with structured output (same signature on both lines)."""
    server.add_tool(
        fn,
        name=name,
        title=title,
        description=description,
        annotations=annotations,
        structured_output=True,
    )


def add_resource(
    server: Any,
    uri: str,
    fn: Callable[..., str],
    *,
    name: str,
    title: str,
    description: str,
    mime_type: str = "application/json",
) -> None:
    """Register a templated resource (same decorator on both lines)."""
    server.resource(uri, name=name, title=title, description=description, mime_type=mime_type)(fn)


def run_stdio(server: Any) -> None:
    """Serve over stdin and stdout until stdin closes."""
    server.run("stdio")


# -- client side


@dataclass(frozen=True)
class ToolInfo:
    """A listed tool, with the fields this project reads, under one spelling."""

    name: str
    title: str | None
    description: str
    read_only: bool | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class ToolOutcome:
    """A tool call's result, under one spelling."""

    is_error: bool
    structured: Any
    text: str


class RawClient:
    """A connected client session of either line, behind four calls."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> list[ToolInfo]:
        result = await self._session.list_tools()
        tools = []
        for tool in result.tools:
            annotations = tool.annotations
            if annotations is None:
                read_only = None
            else:
                read_only = getattr(annotations, "read_only_hint", None)
                if read_only is None:
                    read_only = getattr(annotations, "readOnlyHint", None)
            tools.append(
                ToolInfo(
                    name=tool.name,
                    title=tool.title,
                    description=tool.description or "",
                    read_only=read_only,
                    input_schema=_first_attr(tool, "input_schema", "inputSchema") or {},
                    output_schema=_first_attr(tool, "output_schema", "outputSchema"),
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = await self._session.call_tool(name, arguments)
        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        return ToolOutcome(
            is_error=bool(_first_attr(result, "is_error", "isError")),
            structured=_first_attr(result, "structured_content", "structuredContent"),
            text=text,
        )

    async def read_resource(self, uri: str) -> str:
        result = await self._session.read_resource(uri)
        return "".join(getattr(c, "text", "") for c in result.contents)

    async def list_resource_templates(self) -> list[str]:
        result = await self._session.list_resource_templates()
        templates = _first_attr(result, "resource_templates", "resourceTemplates")
        return [str(_first_attr(t, "uri_template", "uriTemplate")) for t in templates]


def _first_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


@asynccontextmanager
async def connect_in_memory(server: Any) -> AsyncIterator[RawClient]:
    """An initialized in-process client session to ``server``."""
    if _is_v2():
        from mcp import Client

        async with Client(server, cache=None) as client:
            yield RawClient(client)
        return
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        yield RawClient(session)


@asynccontextmanager
async def connect_stdio(
    command: str, args: list[str], *, env: dict[str, str] | None = None
) -> AsyncIterator[RawClient]:
    """An initialized client session to a server launched as a subprocess over stdio."""
    from mcp import StdioServerParameters

    params = StdioServerParameters(command=command, args=args, env=env)
    if _is_v2():
        from mcp import Client

        async with Client(params, cache=None) as client:
            yield RawClient(client)
        return
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield RawClient(session)
