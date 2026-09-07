# ReNile-MCP

An MCP server that exposes the [ReNile IoT](https://renile-iot.com) platform to
Claude Desktop over stdio.

## Tools

| Tool | Purpose |
|---|---|
| `get_all_devices()` | Lists every device (`_id`, `name`). Use it to find the exact name or id to filter by. |
| `get_latest_readings(device=None)` | Latest sensor readings. Omit `device` for everything, or pass a name or id — matching is case-insensitive and accepts partial names like `"greenhouse"`. |

Each reading carries `sensor`, `value`, `unit`, `lower_limit`, `upper_limit`,
`status` (`normal`/`high`/`low`/`unknown`), `timestamp`, `age_seconds`, and a
derived **`is_stale`** flag.

`is_stale` matters: the platform serves last-known values indefinitely, so a
reading can report `status: normal` while being over a year old. Anything older
than `STALE_AFTER_SECONDS` (default 1 hour) is flagged so it is not mistaken for
a current condition.

An unmatched or ambiguous `device` returns `matched: false` plus the valid device
names, rather than failing — so the model can correct itself in the same turn.

## Setup

```bash
uv sync
cp .env.example src/.env      # then set RENILE_API_TOKEN
chmod 600 src/.env
```

| Variable | Default | |
|---|---|---|
| `RENILE_API_TOKEN` | — | **Required.** Sent as `Authorization: JWT <token>`. |
| `RENILE_API_BASE_URL` | `https://renile-iot.com` | |
| `RENILE_DEVICES_PATH` | `/api/v1/devices/names/` | |
| `RENILE_SNAPSHOT_PATH` | `/api/v1/snapshot/` | |
| `HTTP_TIMEOUT_SECONDS` | `15.0` | |
| `HTTP_MAX_ATTEMPTS` | `2` | Total attempts per request; `1` disables retrying. Timeouts are never retried. |
| `ERROR_BODY_PREVIEW_CHARS` | `200` | How much of an upstream error body to quote back. |
| `LOG_LEVEL` | `INFO` | |
| `STALE_AFTER_SECONDS` | `3600` | Age past which a reading is flagged `is_stale`. |

## Claude Desktop

Add to `~/.config/Claude/claude_desktop_config.json`, then **fully quit and
relaunch** Claude Desktop (it does not hot-reload):

```json
{
  "mcpServers": {
    "renile-iot": {
      "command": "/home/renile/.local/bin/uv",
      "args": ["--directory", "/home/renile/Desktop/projects/ReNile-MCP",
               "run", "--frozen", "python", "-m", "src.server"]
    }
  }
}
```

Use an absolute path to `uv` — Claude Desktop does not inherit your shell `PATH`.
Run `uv sync` once beforehand so the first launch doesn't spend the startup
window resolving dependencies. Logs land in
`~/.config/Claude/logs/mcp-server-renile-iot.log`.

## Development

```bash
uv run pytest                      # unit + protocol smoke tests
uv run python -m src.server        # dry run; silence on stdin means success
```

**stdout belongs to the JSON-RPC stream.** Never `print()` in the server path —
a single stray byte breaks the server in Claude Desktop with an opaque error.
Logging is pinned to stderr in `src/core/logging.py`, and
`tests/test_stdio_smoke.py` asserts stdout stays pure JSON.

## Layout

```
src/server.py                 MCP wiring: transport, tool declarations, errors
src/services/processing.py    payload shaping: matching, staleness, responses
src/services/renile_client.py async httpx client for the ReNile API
src/core/config.py            pydantic-settings; the only reader of the environment
src/core/logging.py           stderr-only logging
```
