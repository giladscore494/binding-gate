"""The binding gate packaged as a Model Context Protocol (MCP) server (PR 9).

Any MCP client (an agent such as Claude, or any other) can call the gate as a
tool and gets exactly the deterministic checks and verdict of the research runs
(PREREGISTRATION.md section 5.1).

Modules
-------
``config``   server-level configuration from a TOML file (limits, optional decomposer)
``schemas``  the tool definitions (names, descriptions, JSON input schemas); no SDK needed
``core``     pure-Python tool implementations (validation, gate call, JSON output); no SDK needed
``server``   the thin MCP layer over stdio; the only module that imports the MCP SDK,
             and only when the server is started (optional extra ``mcp``)

The core package keeps zero runtime dependencies: nothing outside ``server``
imports the SDK, and ``server`` imports it lazily. See docs/MCP.md.
"""
