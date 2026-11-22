# The binding gate as an MCP server

binding-gate ships a small [Model Context Protocol](https://modelcontextprotocol.io)
server, so any MCP client (Claude Desktop, Claude Code or another agent) can call
the gate as a tool. The tool runs **the same deterministic checks and the same
verdict** as the research runs (PREREGISTRATION.md section 5.1): it builds the
`binding` gate through the registry, exactly as the `gated` arm does, and calls
the same code.

Code: `src/binding_gate/mcp/` (`core.py` holds the tools, `server.py` the MCP
layer, `schemas.py` the tool definitions, `config.py` the server configuration).
Tests: `tests/test_mcp_core.py`, `tests/test_mcp_config.py`,
`tests/test_mcp_server.py`.

## Honest limits: read these first

- **It checks binding, not truth.** ACCEPT means: the quote is verbatim in the
  cited document (after the normalisation of PREREGISTRATION.md section 6.1),
  and the passage around it mentions the question's entity and conditions, with
  no other document's subject closer to it, under fixed string rules. It does
  **not** mean the source is correct, or that the answer value was read
  correctly from the quote.
- **The parameters are not final.** The default gate parameters
  (`window_tokens = 40`, `min_quote_tokens = 5`, `lead_max_tokens = 4`,
  `lead_min_repeats = 2`, `premise_scope = "document"`,
  `competitor_tie = "fail"`) are the **PROPOSED** values of a pre-registration
  that is still DRAFT. They may change on dev data before the freeze. Every
  result states the parameters used and this status.
- **No results yet.** The study that measures whether the gate reduces
  wrong-entity answers, and what it costs in correct answers, has not been run.
  Nothing here is evidence that the gate helps.
- **String rules have known weaknesses**: synonyms and paraphrases that are not
  listed as aliases are not recognised; a competing entity is found only as the
  leading subject of a provided document (section 5.1 of the pre-registration
  describes the definition and its measured misses on dev data).
- **Not production-hardened** (ARCHITECTURE.md section 9): stdio only, one
  client per process, no rate limiting or authentication (a stdio server is
  started by, and talks only to, the client that launched it).

## Install

The server needs the optional extra `mcp`, which installs the official MCP
Python SDK (package `mcp`, version 2.x; tested with 2.3.0). The core package
keeps no runtime dependencies: nothing outside `binding_gate.mcp.server` imports
the SDK, and that module imports it only when the server is started.

```bash
git clone https://github.com/giladscore494/binding-gate
cd binding-gate
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[mcp]'
```

Python 3.11 or newer.

## Run

```bash
python -m binding_gate.mcp.server --config configs/mcp/server.example.toml
```

The server speaks MCP over stdin/stdout; an MCP client normally starts it for
you (see below). Diagnostics go to stderr. Other modes, which need no SDK:

```bash
python -m binding_gate.mcp.server --check-config --config configs/mcp/server.example.toml
python -m binding_gate.mcp.server --print-tools        # the tool definitions as JSON
```

`--config` is optional; without it the built-in limits apply and no decomposer
is configured.

## Configuration

One TOML file, given on the command line. Example and reference:
[`configs/mcp/server.example.toml`](../configs/mcp/server.example.toml). It lives
in `configs/mcp/` rather than `configs/` because every `configs/*.toml` file is
loaded as a run config by `tests/test_config.py`.

| Table | Keys | Meaning |
|---|---|---|
| (top) | `config_version = 1` | required |
| `[server]` | `name` | server name reported to clients (default `binding-gate`) |
| `[limits]` | `max_documents` (50), `max_document_chars` (200000), `max_total_document_chars` (1000000), `max_question_chars` (4000), `max_id_chars` (200), `max_url_chars` (2048), `max_term_chars` (500), `max_list_items` (50) | input size limits; inputs over a limit are refused, never truncated |
| `[decomposer]` | `name`, `options` | optional; same format as `options.decomposer` of the `gated` arm in a run config |

Gate parameters and checks are deliberately **not** configurable in this file:
their defaults come from one place in the code
(`binding_gate.gates.binding.DEFAULT_PARAMETERS` and `ALL_CHECKS`). A caller can
override them per request; the values used are echoed in every result.

No setting is read from an environment variable. The only environment variable
the server can read is the API key of the decomposer's model client (for
example `ANTHROPIC_API_KEY`, listed in `binding_gate/secrets.py`), and only when
a decomposer is configured and called.

## Client configuration

The server is a local stdio server: the client runs the command above. Use
absolute paths, and the Python of the environment where you installed the
extra.

**Claude Code** (from the Claude Code MCP documentation,
<https://code.claude.com/docs/en/mcp>, read on 2026-11-22; everything after `--`
is the command that starts the server):

```bash
claude mcp add --transport stdio binding-gate -- \
  /path/to/binding-gate/.venv/bin/python -m binding_gate.mcp.server \
  --config /path/to/binding-gate/configs/mcp/server.example.toml
```

Add `--scope project` to store it in the project's `.mcp.json`, shared with
everyone working in that project.

**`mcpServers` JSON** (the shape used by Claude Desktop's configuration file and
by a project's `.mcp.json`; the Claude Code documentation describes this block
as the format other clients such as Claude Desktop use). The location of
Claude Desktop's file depends on the operating system and was **not verified**
for this document; check the client's own documentation.

```json
{
  "mcpServers": {
    "binding-gate": {
      "command": "/path/to/binding-gate/.venv/bin/python",
      "args": [
        "-m", "binding_gate.mcp.server",
        "--config", "/path/to/binding-gate/configs/mcp/server.example.toml"
      ]
    }
  }
}
```

If you enable a decomposer, the server needs its API key in its environment,
for example `"env": {"ANTHROPIC_API_KEY": "..."}` in the entry above. Do not
commit a file that contains a key.

## Tools

The definitions an agent sees (descriptions, JSON input schemas, annotations)
are committed in [`tests/mcp_tools.snapshot.json`](../tests/mcp_tools.snapshot.json)
(default limits, no decomposer); a test fails if they change unnoticed.

### `verify_answer`

Input:

| Field | Required | Meaning |
|---|---|---|
| `question` | yes | the question as asked |
| `answer` | yes | the answer value the agent intends to give |
| `quote` | yes | one continuous passage copied verbatim from the cited document |
| `source_id` | yes | id of the document the quote is from |
| `documents` | yes | every document the agent read: `{id, text, url?}`; ids unique. The other documents matter: their subjects are the candidate competing entities. `url` is recorded, never fetched. |
| `constraints` | no* | `{entity: {name, aliases?, kind?}, attribute, qualifiers?: [{kind, text, aliases?}], presuppositions?: [{text, key_terms?}]}`; `kind` of a qualifier is one of `time`, `place`, `version`, `unit`, `other` |
| `checks` | no | subset of `quote_verbatim`, `entity_in_window`, `qualifiers_in_window`, `no_closer_competitor`, `premise_present` (default: all). Leaving one out is an ablation. They always run in this canonical order. |
| `parameters` | no | overrides of the gate parameters listed above |

\* Required unless the server has a decomposer; with explicit constraints the
call is fully deterministic and makes no network call.

Output (a JSON object; the text content is the same object as canonical JSON,
sorted keys):

| Field | Meaning |
|---|---|
| `verdict` | `ACCEPT`, `RESEARCH` or `ABSTAIN` |
| `reasons` | one `"<check>: <reason code>"` per failed check, in check order (or a summary for ACCEPT) |
| `checks` | per check: `check`, `status` (`pass`, `fail`, `not_applicable`), `details` (positions, distances, matched aliases, competitors) |
| `research_request` | for RESEARCH: entity, aliases, attribute, qualifiers, failed checks and a `query` string to search with; otherwise `null` |
| `verdict_sha256` | SHA-256 of the gate's `Verdict.to_json()`: equal to the hash of the verdict a research run records for the same constraints, answer and documents |
| `constraints`, `constraints_source` | the constraints used, and whether they were `explicit` or from the `decomposer` |
| `documents` | id, SHA-256 and url of each document, in input order |
| `gate` | gate name, checks run, full parameters, whether both are the defaults, and the status of the parameters (PROPOSED) |
| `decomposition` | only if the decomposer was called: its description and every model call (prompt, reply, token usage) |
| `notice`, `binding_gate_version`, `schema_version` | the limits above in one sentence; versions |

What an agent should do with the verdict:

- **ACCEPT** – give the answer and cite the quote.
- **RESEARCH** – the quote is real, but the passage is not clearly about the
  asked entity or conditions, or another entity is closer to it. Look for a
  better source (`research_request.query`) and verify again.
- **ABSTAIN** – the quote is not in the cited document, is too short, or the
  evidence lacks what the question presupposes. Do not give this answer; copy
  the quote exactly, or say that it could not be verified.

The decision rule is the one of PREREGISTRATION.md section 5.1: a failed
`quote_verbatim` or `premise_present` gives ABSTAIN; otherwise any failed check
gives RESEARCH; no failed check gives ACCEPT.

### `decompose_question`

Input: `{question}`. If the server has a `[decomposer]`, it calls that model
(PR 3's `model_decomposer` with its fixed prompt) and returns `constraints` in
exactly the format `verify_answer` accepts, plus the model calls. This is **not
deterministic** and its API cost is paid with the operator's key; review the
constraints before using them. Without a decomposer it returns an error that
explains how to write the constraints by hand.

### Errors

Problems with a call are returned as tool results with `isError: true` and
`{"error": {"code", "message"}}`, so the agent can read and correct them, as the
MCP specification asks for input validation errors. Codes: `invalid_input`,
`too_large`, `constraints_required`, `decomposer_not_configured`,
`api_key_missing` (names the variable, never a value), `decomposition_failed`
(with the logged model calls), `internal_error`. An unknown tool name is a
JSON-RPC protocol error (-32602), as the specification lists it.

A non-empty quote is required. Under the study's protocol an answer without a
quote or source id is `unparseable` and is never judged by the gate (section
6.2), so the tool refuses it with an explanation rather than inventing a
verdict.

## Example

```json
{
  "question": "What is the maximum daily dose of lisinopril for hypertension in adults?",
  "answer": "40 mg",
  "quote": "the maximum daily dose is 40 mg, given in one or two doses",
  "source_id": "enalapril_page",
  "documents": [
    {"id": "lisinopril_page", "text": "Lisinopril is an angiotensin-converting enzyme inhibitor. ... the maximum daily dose of lisinopril is 80 mg. ..."},
    {"id": "enalapril_page", "text": "Enalapril is an angiotensin-converting enzyme inhibitor. Enalapril is used to treat hypertension. In adults with hypertension, the usual starting dose is 5 mg once daily, and the maximum daily dose is 40 mg, given in one or two doses. ..."}
  ],
  "constraints": {
    "entity": {"name": "lisinopril"},
    "attribute": "maximum daily dose",
    "qualifiers": [{"kind": "other", "text": "hypertension"}]
  }
}
```

The quote is verbatim, but it comes from the enalapril page: the verdict is
`RESEARCH` with reasons `entity_in_window: entity_not_in_document` and
`no_closer_competitor: competitor_closer`, and the research query
`lisinopril maximum daily dose hypertension`. (Invented texts; the full versions
are in `tests/mcp_fixtures.py`.)

## How identity with the research gate is guaranteed

- One implementation: `core.py` calls `registry.create("gate", "binding",
  checks=..., **parameters).evaluate(...)`, the same call the `gated` arm makes.
  Defaults, check names and allowed parameter values are imported from the gate
  and check modules, not repeated.
- Tests compare, on accepted, researched, abstained, ablated and re-parameterised
  cases, the tool's output with the registry gate's verdict and with the verdict
  the `gated` arm records when given the same candidate and constraints
  (`verdict_sha256` equal, verdict rebuilt from the output byte-identical).
- The deterministic path is tested with sockets disabled.

## Performance

About 0.3 s for five documents near the limits (about 1 MB of text) on the test
machine. Texts in which the quote occurs hundreds of times are slower (the
anchor search compares every occurrence with every entity mention): a 170,000
character text repeating the same paragraph about 600 times took about 2.4 s.
