"""The MCP layer with the official SDK (PR 9): tools/list and tools/call in-process and over a real stdio subprocess.

Skipped when the optional extra is not installed (``pip install -e '.[mcp]'``).
No model and no network: the stdio test talks to a child process through pipes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp", reason="optional extra 'mcp' not installed")
anyio = pytest.importorskip("anyio")

import mcp_fixtures as f  # noqa: E402
from binding_gate.mcp.config import ServerConfig  # noqa: E402
from binding_gate.mcp.core import GateService  # noqa: E402
from binding_gate.mcp.server import build_server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "configs" / "mcp" / "server.example.toml"


def wire(tool) -> dict:
    """The fields of an SDK Tool that come from our definitions, in wire (camelCase) form."""
    data = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
    return {k: data[k] for k in ("name", "title", "description", "inputSchema", "annotations")}


async def _list_and_call(service: GateService, calls: list[tuple[str, dict | None]]):
    async with mcp.Client(build_server(service)) as client:
        tools = await client.list_tools()
        results = [await client.call_tool(name, arguments) for name, arguments in calls]
    return tools, results


def run_in_process(service: GateService, calls):
    return anyio.run(_list_and_call, service, calls)


def test_tools_list_exposes_exactly_the_core_definitions():
    service = GateService()
    tools, _ = run_in_process(service, [])
    assert [wire(t) for t in tools.tools] == service.tool_definitions()


def test_tools_call_returns_the_core_payload_as_structured_and_canonical_text():
    service = GateService()
    calls = [("verify_answer", f.args()), ("verify_answer", f.wrong_entity_args())]
    _, results = run_in_process(service, calls)
    for (name, arguments), result in zip(calls, results):
        expected = service.call_tool(name, arguments)
        assert result.is_error is False
        assert result.structured_content == expected.payload
        assert [c.type for c in result.content] == ["text"]
        assert result.content[0].text == expected.text()
    assert results[0].structured_content["verdict"] == "ACCEPT"
    assert results[1].structured_content["verdict"] == "RESEARCH"


def test_errors_are_tool_results_the_agent_can_read_not_protocol_errors():
    calls = [
        ("verify_answer", {"question": "q"}),
        ("decompose_question", {"question": f.QUESTION}),
        ("verify_answer", None),
    ]
    _, results = run_in_process(GateService(), calls)
    codes = [r.structured_content["error"]["code"] for r in results]
    assert codes == ["invalid_input", "decomposer_not_configured", "invalid_input"]
    assert all(r.is_error for r in results)
    assert json.loads(results[1].content[0].text)["error"]["code"] == "decomposer_not_configured"


def test_decomposer_path_through_the_sdk():
    service = GateService(ServerConfig(decomposer={"name": "fake_mcp_fixed_decomposer", "options": {}}))
    no_constraints = {k: v for k, v in f.args().items() if k != "constraints"}
    _, results = run_in_process(
        service, [("decompose_question", {"question": f.QUESTION}), ("verify_answer", no_constraints)]
    )
    assert results[0].structured_content["constraints"]["entity"]["name"] == "lisinopril"
    assert results[1].structured_content["constraints_source"] == "decomposer"
    assert results[1].structured_content["verdict"] == "ACCEPT"


async def _call_unknown_tool():
    async with mcp.Client(build_server(GateService())) as client:
        try:
            await client.call_tool("no_such_tool", {})
        except mcp.MCPError as exc:
            return exc
    return None


def test_unknown_tool_is_a_protocol_error_as_the_specification_prescribes():
    exc = anyio.run(_call_unknown_tool)
    assert exc is not None, "expected a JSON-RPC error"
    assert exc.code == -32602 and "no_such_tool" in exc.message


async def _over_stdio(arguments: dict):
    params = mcp.StdioServerParameters(
        command=sys.executable,
        args=["-m", "binding_gate.mcp.server", "--config", str(EXAMPLE_CONFIG)],
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": os.environ.get("PATH", "")},
        cwd=str(REPO_ROOT),
    )
    async with mcp.Client(params) as client:
        tools = await client.list_tools()
        result = await client.call_tool("verify_answer", arguments)
    return tools, result


def test_end_to_end_over_a_stdio_subprocess():
    tools, result = anyio.run(_over_stdio, f.wrong_entity_args())
    assert [t.name for t in tools.tools] == ["verify_answer", "decompose_question"]
    expected = GateService().call_tool("verify_answer", f.wrong_entity_args())
    assert result.is_error is False
    assert result.content[0].text == expected.text()
    assert result.structured_content["verdict"] == "RESEARCH"
