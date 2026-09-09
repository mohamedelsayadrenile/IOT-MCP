# ReNile-MCP

A remote MCP server that exposes the [ReNile IoT](https://renile-iot.com)
platform over Streamable HTTP. Clients connect by URL and sign in with their own
ReNile account through OAuth; each user sees only their own devices.

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

## How authentication works

The server is an OAuth 2.1 **resource server**. It issues nothing and stores no
credentials:

1. A client connects to `/mcp` with no token and gets `401` plus
   `WWW-Authenticate: Bearer ..., resource_metadata="..."`.
2. It follows that to `/.well-known/oauth-protected-resource/mcp`, which names
   the ReNile authorization server, and runs the OAuth flow there.
3. It retries with the user's access token. The server forwards that token
   verbatim to the ReNile API.

**The server does not validate tokens** — no signature check, no audience check.
The ReNile API is the sole authority and answers `401` if a token is bad. The
token's `sub`, `iss` and `exp` claims *are* read (unverified), for two reasons
documented in `src/core/auth.py`: to bind an MCP session to one principal, so a
session id is not a usable credential for a different user; and to reject an
expired token at the door, where the client still gets a re-auth challenge.

Opaque (non-JWT) tokens work too — the session is bound to a hash of the token
instead — but expiry can then only be discovered upstream.

### What the ReNile backend must provide

- `/.well-known/oauth-authorization-server` (RFC 8414)
- `/authorize` and `/token`: `authorization_code` with PKCE `S256`, plus `refresh_token`
- Dynamic Client Registration (RFC 7591) — without it, every client product must
  be pre-registered by hand
- The `resource` parameter (RFC 8707), so tokens are audience-bound
- `/api/v1/devices/names/` and `/api/v1/snapshot/` accepting those access tokens,
  and **scoped to the authenticated user**
- `401` for an expired token, kept distinct from `403` for a permission failure

## Setup

```bash
uv sync
cp .env.example src/.env      # then set the two required URLs
chmod 600 src/.env
uv run uvicorn src.app:app --host 0.0.0.0 --port 8000
```

| Variable | Default | |
|---|---|---|
| `RENILE_ISSUER_URL` | — | **Required.** The ReNile authorization server. No trailing slash. |
| `RENILE_RESOURCE_SERVER_URL` | — | **Required.** The exact public URL clients use, including `/mcp`. |
| `ALLOWED_HOSTS` | *(empty)* | Comma-separated. Must include the public hostname or requests are rejected with 421. Each entry also matches that host on any port. |
| `ALLOWED_ORIGINS` | *(empty)* | Comma-separated. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |
| `STATELESS_HTTP` | `false` | See "Scaling" below. |
| `RENILE_UPSTREAM_AUTH_SCHEME` | `JWT` | Scheme used when forwarding the token upstream. The platform uses `JWT`, not `Bearer`. |
| `RENILE_API_BASE_URL` | `https://renile-iot.com` | |
| `RENILE_DEVICES_PATH` | `/api/v1/devices/names/` | |
| `RENILE_SNAPSHOT_PATH` | `/api/v1/snapshot/` | |
| `HTTP_TIMEOUT_SECONDS` | `15.0` | |
| `HTTP_MAX_ATTEMPTS` | `2` | Total attempts per request; `1` disables retrying. Timeouts are never retried. |
| `HTTP_MAX_CONNECTIONS` | `100` | Shared upstream pool size. |
| `ERROR_BODY_PREVIEW_CHARS` | `200` | How much of an upstream error body to quote back. |
| `LOG_LEVEL` | `INFO` | |
| `STALE_AFTER_SECONDS` | `3600` | Age past which a reading is flagged `is_stale`. |

## Deployment

```bash
docker build -t renile-mcp .
docker run -p 8000:8000 --env-file deploy.env renile-mcp
```

Routes: `/mcp`, `/.well-known/oauth-protected-resource/mcp`, and `/healthz`
(unauthenticated, liveness only — it makes no upstream call because it has no
token to make one with).

**Serve it over HTTPS.** The access token is a bearer credential on every
request. Terminate TLS at a proxy and keep `--proxy-headers` on.

Two settings break the first deploy if they are wrong:

- `RENILE_RESOURCE_SERVER_URL` must byte-match the URL clients actually use. It
  is what the 401 challenge points at; get it wrong and OAuth discovery
  dead-ends silently.
- `ALLOWED_HOSTS` must contain the public hostname. DNS-rebinding protection is
  always on, and an unlisted host is answered with `421 Invalid Host header`.
  A bare hostname also matches that host on any port, so `localhost` covers
  `localhost:8000`.

### Scaling

Sessions are held in a per-process dict, so stateful serving needs one worker per
process and sticky routing at the load balancer. For several replicas behind a
round-robin balancer, set `STATELESS_HTTP=true` — but note that this also removes
the session-ownership check, which only matters for older clients (2025-era
protocol versions); newer ones are per-request either way.

## Connecting a client

Add `https://mcp.renile-iot.com/mcp` as a remote MCP server (Claude, ChatGPT and
Cursor all take a URL) and complete the ReNile sign-in when prompted.

## Development

```bash
uv run pytest                              # unit + HTTP protocol tests
uv run uvicorn src.app:app --reload        # local run
curl -i localhost:8000/mcp                 # expect 401 + WWW-Authenticate
```

The test suite drives the real ASGI app in-process — no uvicorn subprocess, no
port to race on. `tests/test_http.py` covers the assertions that are easy to
break silently: the 401 challenge carries `resource_metadata`, the caller's token
reaches the upstream API verbatim, two concurrent callers never cross tokens, and
one user's session id is refused to another.

## Layout

```
src/app.py                    ASGI entrypoint: transport security, uvicorn target
src/server.py                 MCP wiring: tools, lifespan, auth settings, errors
src/core/auth.py              the pass-through token verifier
src/core/config.py            pydantic-settings; the only reader of the environment
src/core/logging.py           stderr logging
src/services/processing.py    payload shaping: matching, staleness, responses
src/services/renile_client.py async httpx client; the token is a per-call argument
```
