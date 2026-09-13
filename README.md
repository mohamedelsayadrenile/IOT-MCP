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

This server is an OAuth 2.1 **resource server** and nothing more. The ReNile
backend is the authorization server: it runs the login, consent and token
endpoints. This server issues no tokens, sees no passwords and stores nothing.

1. A client connects to `/mcp` with no token and gets `401` plus
   `WWW-Authenticate: Bearer ..., resource_metadata="..."`.
2. It follows that to `/.well-known/oauth-protected-resource/mcp`, which names
   the ReNile authorization server, and runs the OAuth flow there: the user signs
   in with their ReNile account and approves access.
3. It retries with its OAuth access token.
4. This server hands that token to the backend's token-exchange endpoint
   (RFC 8693), authenticating as its own confidential client. The backend alone
   decides whether the token is valid, whose it is and what scopes it carries.
   If it is good, the backend returns a short-lived ReNile JWT for that user.
5. Tools call the ReNile API with `Authorization: JWT <that JWT>`, so the API
   keeps every user to their own data. No tool takes a user id from the client.

The exchange result is cached (keyed by a hash of the token) until shortly
before the ReNile JWT expires, so the backend is asked once per token lifetime.

What never leaves this server: the ReNile JWT, the exchange client secret, and
raw backend error bodies. The OAuth token is only ever sent to the exchange
endpoint, never to the ReNile API.

| Situation | Response |
|---|---|
| No token, or the backend refuses the exchange (400/401) | `401` + challenge: the client refreshes or re-runs OAuth |
| Token lacks a required scope | `403 insufficient_scope` |
| Exchange endpoint down (5xx / unreachable) | `500`, not `401`, so clients keep their token |
| ReNile API rejects an exchanged JWT | Tool error asking to reconnect; the cached exchange is dropped |

### What the ReNile backend must provide

- `/.well-known/oauth-authorization-server` (RFC 8414) advertising
  `client_id_metadata_document_supported: true`, `"none"` in
  `token_endpoint_auth_methods_supported`, `S256`, and
  `authorization_response_iss_parameter_supported: true`
- `/oauth/authorize` and `/oauth/token`: `authorization_code` with PKCE `S256`,
  `refresh_token`, and Client ID Metadata Documents for Claude and ChatGPT/Codex
- Access tokens bound to the `resource` (this server's URL) and the user
- A token-exchange grant for this server only, returning
  `{access_token: <ReNile JWT>, expires_in, sub, scope}`
- The existing ReNile API unchanged: data scoped to the JWT's user, `401` for an
  expired JWT kept distinct from `403` for a permission failure

## Setup

```bash
uv sync
cp .env.example src/.env      # then set the URLs and exchange credentials
chmod 600 src/.env
uv run uvicorn src.app:app --host 0.0.0.0 --port 8000
```

| Variable | Default | |
|---|---|---|
| `RENILE_ISSUER_URL` | — | **Required.** The ReNile backend's authorization server issuer, exactly as it advertises it. No trailing slash. |
| `RENILE_RESOURCE_SERVER_URL` | — | **Required.** The exact public URL clients use, including `/mcp`. |
| `OAUTH_REQUIRED_SCOPES` | `devices:read readings:read` | Scopes every token must carry; advertised in the resource metadata. |
| `TOKEN_EXCHANGE_URL` | — | **Required.** The backend's token-exchange endpoint. |
| `TOKEN_EXCHANGE_AUDIENCE` | — | `audience` sent with the exchange, as agreed with the backend. Omitted if empty. |
| `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` | — | **Required.** This server's confidential-client credentials at the backend. Keep the secret in a 0600 file. |
| `ALLOWED_HOSTS` | *(empty)* | Comma-separated. Must include the public hostname or requests are rejected with 421. Each entry also matches that host on any port. |
| `ALLOWED_ORIGINS` | *(empty)* | Comma-separated. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |
| `STATELESS_HTTP` | `false` | See "Scaling" below. |
| `RENILE_UPSTREAM_AUTH_SCHEME` | `JWT` | Scheme used for the exchanged ReNile JWT upstream. The platform uses `JWT`, not `Bearer`. |
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
request. Terminate TLS at a proxy and keep `--proxy-headers` on. The proxy must
forward `/.well-known/oauth-protected-resource/mcp` as well as `/mcp`, and must
not buffer `/mcp` (`proxy_buffering off;` in nginx) or streamed responses stall.

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
break silently: the 401 challenge carries `resource_metadata`, the ReNile API
only ever receives the exchanged JWT (never the OAuth token), the exchange is
authenticated as this server, two concurrent callers never cross tokens, and one
user's session id is refused to another. The backend is faked with
`httpx.MockTransport`, so no real authorization server is needed.

## Layout

```
src/app.py                    ASGI entrypoint: transport security, uvicorn target
src/server.py                 MCP wiring: lifespan, auth settings, /healthz
src/tools.py                  MCP tools and their shared error handling
src/core/config.py            pydantic-settings; the only reader of the environment
src/core/logging.py           stderr logging
src/services/processing.py    payload shaping: matching, staleness, responses
src/services/auth.py          the token-exchange verifier and its cache
src/services/errors.py        exception types for upstream failures
src/services/token_exchange.py RFC 8693 exchange: OAuth token -> ReNile JWT
src/services/renile_client.py async httpx client for the ReNile API
```
