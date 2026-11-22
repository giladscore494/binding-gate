"""Server configuration file of the MCP server (PR 9) and the command line that reads it. No SDK needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from binding_gate.mcp import server
from binding_gate.mcp.config import (
    DEFAULT_SERVER_NAME,
    Limits,
    ServerConfigError,
    load_server_config,
    parse_server_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "configs" / "mcp" / "server.example.toml"
SNAPSHOT = Path(__file__).resolve().parent / "mcp_tools.snapshot.json"


def test_no_file_gives_defaults_without_a_decomposer():
    config = load_server_config(None)
    assert config.name == DEFAULT_SERVER_NAME and config.limits == Limits() and config.decomposer is None


def test_example_config_is_not_in_the_run_config_directory():
    # tests/test_config.py loads every configs/*.toml (non-recursive) as a run config.
    assert EXAMPLE_CONFIG.parent.name == "mcp" and EXAMPLE_CONFIG.parent.parent.name == "configs"
    assert not list((REPO_ROOT / "configs").glob("mcp*.toml"))


def test_full_config_is_parsed():
    config = parse_server_config(
        {
            "config_version": 1,
            "server": {"name": "my-gate"},
            "limits": {"max_documents": 5},
            "decomposer": {"name": "model_decomposer", "options": {"prompt_version": 1}},
        },
        source="x.toml",
    )
    assert config.name == "my-gate" and config.limits.max_documents == 5
    assert config.limits.max_document_chars == Limits().max_document_chars
    assert config.decomposer == {"name": "model_decomposer", "options": {"prompt_version": 1}}
    assert config.source == "x.toml"


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({}, "config_version must be 1"),
        ({"config_version": True}, "config_version must be 1"),
        ({"config_version": 2}, "config_version must be 1"),
        ({"config_version": 1, "gate": {}}, "unknown top-level key(s) ['gate']"),
        ({"config_version": 1, "server": "x"}, "[server] must be a table"),
        ({"config_version": 1, "server": {"port": 1}}, "[server]: unknown key(s) ['port']"),
        ({"config_version": 1, "server": {"name": " "}}, "server.name must be a non-empty string"),
        ({"config_version": 1, "limits": []}, "[limits] must be a table"),
        ({"config_version": 1, "limits": {"max_docs": 1}}, "[limits]: unknown key(s) ['max_docs']"),
        ({"config_version": 1, "limits": {"max_documents": 0}}, "limits.max_documents must be an integer >= 1"),
        ({"config_version": 1, "limits": {"max_documents": True}}, "limits.max_documents must be an integer >= 1"),
        ({"config_version": 1, "decomposer": {"options": {}}}, "decomposer.name must be a non-empty string"),
        ({"config_version": 1, "decomposer": {"name": "x", "model": {}}}, "unknown key(s) ['model']"),
    ],
)
def test_invalid_configs_are_refused(data, fragment):
    with pytest.raises(ServerConfigError) as info:
        parse_server_config(data)
    assert fragment in str(info.value)


def test_unreadable_and_malformed_files(tmp_path):
    with pytest.raises(ServerConfigError, match="cannot read"):
        load_server_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("config_version = = 1\n", encoding="utf-8")
    with pytest.raises(ServerConfigError, match="not valid TOML"):
        load_server_config(bad)


# -- command line (no SDK needed for these modes) ------------------------------- #


def test_print_tools_writes_the_snapshot(capsys):
    assert server.main(["--print-tools"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_check_config_reports_on_stderr_only(capsys):
    assert server.main(["--check-config", "--config", str(EXAMPLE_CONFIG)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration OK" in captured.err and "no decomposer" in captured.err


def test_bad_config_exits_with_status_2(tmp_path, capsys):
    path = tmp_path / "server.toml"
    path.write_text("config_version = 1\n[limits]\nmax_documents = -1\n", encoding="utf-8")
    assert server.main(["--check-config", "--config", str(path)]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_unknown_decomposer_name_is_a_configuration_error(tmp_path, capsys):
    path = tmp_path / "server.toml"
    path.write_text('config_version = 1\n[decomposer]\nname = "no_such_decomposer"\n', encoding="utf-8")
    assert server.main(["--check-config", "--config", str(path)]) == 2
    assert "no_such_decomposer" in capsys.readouterr().err


def test_configured_decomposer_names_its_key_variable_never_its_value(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    path = tmp_path / "server.toml"
    path.write_text(
        "config_version = 1\n[decomposer]\nname = \"model_decomposer\"\n"
        "[decomposer.options.model]\nname = \"anthropic_messages\"\n"
        "[decomposer.options.model.options]\nmodel_id = \"example-model\"\nmax_output_tokens = 256\n",
        encoding="utf-8",
    )
    assert server.main(["--check-config", "--config", str(path)]) == 0
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "sk-test" not in err


def test_importing_the_server_module_does_not_import_the_sdk():
    import subprocess
    import sys

    code = "import sys, binding_gate.mcp.server, binding_gate.mcp.core; print('mcp' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert out.stdout.strip() == "False"


def test_starting_without_the_sdk_explains_how_to_install_it(monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "mcp", None)  # makes "import mcp" fail, as without the extra
    monkeypatch.setitem(sys.modules, "mcp.types", None)
    assert server.main([]) == 2
    assert "pip install 'binding-gate[mcp]'" in capsys.readouterr().err
