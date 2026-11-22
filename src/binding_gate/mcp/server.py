"""MCP server over stdio exposing the binding gate as tools (``verify_answer``, ``decompose_question``).

Run::

    pip install 'binding-gate[mcp]'
    python -m binding_gate.mcp.server --config configs/mcp/server.example.toml

Other options: ``--print-tools`` writes the tool definitions as JSON and exits
(no SDK needed); ``--check-config`` validates the configuration and exits.

This module is the only one that imports the MCP SDK (the official ``mcp``
package, low-level ``Server`` API of SDK version 2), and it does so lazily,
inside :func:`build_server` and :func:`serve_stdio`, so importing it, and the
rest of ``binding_gate``, needs no third-party package. Everything a tool does
is in :mod:`binding_gate.mcp.core`; this layer only converts:

* ``tools/list``  -> ``GateService.tool_definitions()`` as SDK ``Tool`` objects;
* ``tools/call``  -> ``GateService.call_tool(name, arguments)``, run in a worker
  thread, returned as a ``CallToolResult`` whose ``structured_content`` is the
  JSON object and whose single text content is the same object as canonical
  JSON. Failures are tool results with ``is_error = true`` (readable by the
  agent, so it can correct its input), as the MCP specification asks for input
  validation errors. The one exception is an unknown tool name, which the
  specification lists as a protocol error: it is raised as JSON-RPC error
  -32602 (invalid params).

Over stdio, stdout carries the protocol; this module writes diagnostics to
stderr only.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from binding_gate import __version__
from binding_gate.mcp.config import ServerConfigError, load_server_config
from binding_gate.mcp.core import GateService, ToolResult
from binding_gate.mcp.schemas import TOOL_NAMES

MCP_EXTRA_HINT = "the MCP server needs the optional extra: pip install 'binding-gate[mcp]'"

INSTRUCTIONS = (
    "binding-gate verifies that an answer taken from a source is bound to the question: the quote is verbatim "
    "in the cited document and the passage is about the asked entity and conditions. Call verify_answer before "
    "stating such an answer; pass explicit constraints for a fully deterministic check. It checks binding, "
    "not truth."
)


class MissingSDK(RuntimeError):
    """The optional MCP SDK is not installed."""


def _import_sdk() -> Any:
    try:
        import mcp.types as types  # the official MCP Python SDK (optional extra "mcp")
        from mcp import MCPError
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # without the extra (tests/test_mcp_config.py)
        raise MissingSDK(f"{MCP_EXTRA_HINT} ({exc})") from exc
    return types, Server, stdio_server, MCPError


def to_call_tool_result(result: ToolResult, types: Any) -> Any:
    """Convert a core :class:`ToolResult` into the SDK's ``CallToolResult``."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.text())],
        structured_content=result.payload,
        is_error=result.is_error,
    )


def to_sdk_tool(definition: dict[str, Any], types: Any) -> Any:
    """Convert one tool definition of :mod:`binding_gate.mcp.schemas` into the SDK's ``Tool``."""
    a = definition["annotations"]
    return types.Tool(
        name=definition["name"],
        title=definition["title"],
        description=definition["description"],
        input_schema=definition["inputSchema"],
        annotations=types.ToolAnnotations(
            title=a["title"],
            read_only_hint=a["readOnlyHint"],
            destructive_hint=a["destructiveHint"],
            idempotent_hint=a["idempotentHint"],
            open_world_hint=a["openWorldHint"],
        ),
    )


def build_server(service: GateService) -> Any:
    """The low-level MCP ``Server`` whose two handlers delegate to ``service``."""
    import anyio  # installed with the SDK

    types, Server, _, MCPError = _import_sdk()
    tools = [to_sdk_tool(definition, types) for definition in service.tool_definitions()]

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        if params.name not in TOOL_NAMES:
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")
        result = await anyio.to_thread.run_sync(service.call_tool, params.name, params.arguments)
        return to_call_tool_result(result, types)

    return Server(
        service.config.name,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(service: GateService) -> None:
    """Serve ``service`` over stdin/stdout until the client disconnects."""
    stdio_server = _import_sdk()[2]
    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m binding_gate.mcp.server",
        description="Serve the binding gate as MCP tools over stdio. See docs/MCP.md.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="server configuration TOML file (limits, optional decomposer); default: built-in limits, no decomposer",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print-tools", action="store_true", help="print the tool definitions as JSON and exit")
    mode.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_server_config(args.config)
        service = GateService(config)
    except (ServerConfigError, ValueError, LookupError) as exc:
        print(f"binding-gate MCP server: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.print_tools:
        print(json.dumps(service.tool_definitions(), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    key = service.decomposer_api_key_name()
    status = "no decomposer (explicit constraints required)"
    if service.decomposer_configured:
        status = f"decomposer configured (API key variable: {key or 'none'})"
    if args.check_config:
        print(f"configuration OK: {config.source or 'built-in defaults'}; {status}", file=sys.stderr)
        return 0
    try:
        import anyio

        _import_sdk()
    except (ImportError, MissingSDK):
        print(f"binding-gate MCP server: {MCP_EXTRA_HINT}", file=sys.stderr)
        return 2
    print(f"binding-gate MCP server {__version__} on stdio; {status}", file=sys.stderr)
    anyio.run(serve_stdio, service)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
