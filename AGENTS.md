# moodle-mcp

A Python package that exposes a student's Moodle account as MCP tools (stdio), plus a `moodle` CLI. It talks to Moodle's REST web-service API (`webservice/rest/server.php`) with the user's mobile-app token.

## Layout

- `src/moodle_mcp/client.py`: the `Moodle` class. `call(wsfunction, **params)` is the only network path for API calls, and it flattens nested params via `_flatten`. Each helper method calls one or more web-service functions and returns plain dicts/lists trimmed to useful fields. Text helpers: `html_to_text`, `extract_text` (PDF/DOCX/PPTX/XLSX), `fmt_time`.
- `src/moodle_mcp/server.py`: FastMCP tools. Each tool is a thin wrapper, `return _guard(lambda: m().<method>(...))`, so client creation and every error (with the token redacted) stay inside the guard. `moodle-mcp --http` serves Streamable HTTP on 127.0.0.1 for OpenAI's Secure MCP Tunnel. Read-only tools use `annotations=READ`.
- `src/moodle_mcp/cli.py`: argparse front end over the same client.
- `tests/`: offline tests using `httpx.MockTransport`. They never need a real token.
- `docs/extending.md`: the step-by-step for adding a tool. `docs/openai-tunnel.md`: ChatGPT via OpenAI's Secure MCP Tunnel, running it as a service, and staying signed in.

## Adding a tool

1. `moodle functions <word>` to find a web-service function, and `moodle call <fn> '{...}'` to see its output.
2. Add a method on `Moodle` in `client.py` that keeps only the useful fields (use `_u()` for names, `fmt_time()` for timestamps).
3. Add an `@mcp.tool` in `server.py`. The docstring is what Claude reads, so make it specific about what comes back.
4. Bump the tool count in `tests/test_server.py`, then run `uv run pytest` and `uv run ruff check .`.

## Rules

- Never print, log, commit or hard-code a token. Config lives in `~/.config/moodle-mcp/config.json`.
- Any tool that writes to Moodle gets `readOnlyHint=False, destructiveHint=True`, and its docstring tells the model to show the user the exact content and get a yes first.
- The server is read-only by default. A tool that writes to Moodle must call `_require_writes(...)` first, so it only works when the server runs with `--allow-writes` or `MOODLE_MCP_ALLOW_WRITES=1`.
- This is a public repo. If you test against a real Moodle site, keep what you saw out of the repo: no site URLs, course names or ids, user names, file names or response samples in code, tests, docs or commit messages. Write fixes and tests from Moodle's documented API with made-up data (`moodle.example.edu`, `CHEM 111`).
- File URLs must stay same-origin with the configured site before a token is attached; local paths from the model (downloads, uploads) must be confined and checked. `tests/test_security.py` covers these.
- Keep it cross-platform (macOS, Windows, Linux): no `%-d` style strftime codes and no shell-outs.
- The user restarts their Claude session to pick up server changes. An editable install (`uv tool install --force --editable .`) makes the commands run this checkout.
