# MCP APIs Contract

## Scope

The Nojo backend is the **OAuth authorization server**: it runs login, consent, and the token endpoints documented here. The MCP server is only a **resource server** — it issues no tokens, stores no passwords, and serves its own `/.well-known/oauth-protected-resource/mcp`. The backend must **not** build a protected-resource metadata endpoint, and the reverse proxy must route that one path to the MCP server rather than to the backend.

The Nojo platform APIs the MCP server calls with the exchanged JWT are existing and unchanged; they are out of scope for this contract.

### Flow

```text
Client  -> Backend: OAuth login and consent          (APIs 1-3)
Client  -> MCP:     /mcp with the OAuth access token
MCP     -> Backend: token exchange for a Nojo JWT    (API 5)
MCP     -> Nojo:    existing platform APIs with the JWT
```

The authorization server and the MCP resource server share the host `nojo.ai`, so the reverse proxy in front of it must split the traffic between two services:

| Path on `nojo.ai` | Served by |
| --- | --- |
| `/.well-known/oauth-authorization-server` | Backend (this contract, API 1) |
| `/oauth/authorize`, `/oauth/token` | Backend (APIs 2-5) |
| `/.well-known/oauth-protected-resource/mcp` | **MCP server** |
| `/mcp` | **MCP server** |

If a backend catch-all answers `/.well-known/oauth-protected-resource/mcp`, discovery fails silently and clients never connect. All OAuth endpoints must be public HTTPS with a valid certificate.

## API 1: OAuth Authorization Server Metadata

### `GET /.well-known/oauth-authorization-server`

Returns OAuth server metadata for clients such as Claude, Claude Code, ChatGPT, and Codex.

### Request

No request body.

### Response `200 application/json`

```json
{
  "issuer": "https://nojo.ai",
  "authorization_endpoint": "https://nojo.ai/oauth/authorize",
  "token_endpoint": "https://nojo.ai/oauth/token",
  "response_types_supported": ["code"],
  "grant_types_supported": [
    "authorization_code",
    "refresh_token",
    "urn:ietf:params:oauth:grant-type:token-exchange"
  ],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
  "scopes_supported": [],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true
}
```

### Response Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `issuer` | string | Yes | OAuth issuer URL. No trailing slash — RFC 8414 compares issuers by exact string |
| `authorization_endpoint` | string | Yes | Browser authorization endpoint |
| `token_endpoint` | string | Yes | Token endpoint for code exchange, refresh, and token exchange |
| `response_types_supported` | string[] | Yes | Must include `code` |
| `grant_types_supported` | string[] | Yes | Must include `authorization_code`, `refresh_token`, and token exchange grant |
| `code_challenge_methods_supported` | string[] | Yes | Must include `S256` |
| `token_endpoint_auth_methods_supported` | string[] | Yes | Must include `none` and `client_secret_basic` |
| `scopes_supported` | string[] | Yes | Empty array. This integration uses no scopes |
| `client_id_metadata_document_supported` | boolean | Yes | Must be `true` |
| `authorization_response_iss_parameter_supported` | boolean | Yes | Must be `true` |

### Required Headers

`Access-Control-Allow-Origin: *`. Browser-hosted clients fetch this document cross-origin; without CORS the connection fails with no visible error.

### Notes

There are no scopes in this integration. A client may still send a `scope` parameter; accept it and ignore it. The MCP server performs no scope checks — a valid token has full access to its own user's data.

## API 2: OAuth Authorization

### `GET /oauth/authorize`

Starts browser-based OAuth login and consent.

### Request Query Parameters

| Field | Type | Required | Description |
|---|---|---|---|
| `response_type` | string | Yes | Must be `code` |
| `client_id` | string | Yes | OAuth client ID. Always a Client ID Metadata Document URL — see below |
| `redirect_uri` | string | Yes | Client callback URL. Must be listed in the client's metadata document |
| `state` | string | No | Opaque value. When sent, it must be returned unchanged in the redirect |
| `code_challenge` | string | Yes | PKCE challenge |
| `code_challenge_method` | string | Yes | Must be `S256` |
| `resource` | string | Yes | Must equal `https://nojo.ai/mcp`. The issued access token is bound to this audience |
| `scope` | string | No | Accepted and ignored |

### Client ID Metadata Documents

`client_id` is a URL pointing to a Client ID Metadata Document. Supported clients:

| Client | `client_id` | Redirect URI |
|---|---|---|
| Claude | `https://claude.ai/oauth/mcp-oauth-client-metadata` | `https://claude.ai/api/mcp/auth_callback` |
| Claude Code | `https://claude.ai/oauth/claude-code-client-metadata` | `http://localhost/callback` with any port |
| ChatGPT | `https://chatgpt.com/oauth/client.json` | `https://chatgpt.com/connector_platform_oauth_redirect` |
| Codex | `https://chatgpt.com/oauth/codex/client.json` | `http://127.0.0.1/callback` with any port |

Validation rules:

| Rule | Requirement |
|---|---|
| Host allowlist | Fetch the metadata document only from `claude.ai` or `chatgpt.com`. Reject any other host |
| Self-consistency | The document's `client_id` must exactly equal the URL it was fetched from |
| Redirect URI | The request's `redirect_uri` must be listed in the document's `redirect_uris` |

### Successful Response `302 Found`

Redirects the browser to the provided `redirect_uri`.

```text
<redirect_uri>?code=<authorization_code>&state=<state>&iss=<issuer>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | Single-use authorization code |
| `state` | string | Conditional | Same `state` received in the request, when one was sent |
| `iss` | string | Yes | OAuth issuer URL |

### Error Response `302 Found`

Only when `client_id` and `redirect_uri` are both valid. Redirects the browser to the provided `redirect_uri`.

```text
<redirect_uri>?error=<error_code>&state=<state>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string | Yes | OAuth error code, for example `invalid_request`, `access_denied`, or `server_error` |
| `error_description` | string | No | Human-readable detail |
| `state` | string | Conditional | Same `state` received in the request, when one was sent |

### Invalid Client Or Redirect URI

If `client_id` is unknown, its metadata document fails validation, or `redirect_uri` is not listed in that document, the backend must **not** redirect. Render an error page in the browser instead. Redirecting in this case turns the endpoint into an open redirect (RFC 6749 section 4.1.2.1).

## API 3: OAuth Token - Authorization Code Exchange

### `POST /oauth/token`

Exchanges an OAuth authorization code for an OAuth access token and refresh token.

### Request Headers

```http
Content-Type: application/x-www-form-urlencoded
```

### Request Body

```text
grant_type=authorization_code
&code=<authorization_code>
&redirect_uri=<redirect_uri>
&client_id=<client_id>
&code_verifier=<pkce_code_verifier>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `grant_type` | string | Yes | Must be `authorization_code` |
| `code` | string | Yes | Authorization code returned from `/oauth/authorize` |
| `redirect_uri` | string | Yes | Same redirect URI used in `/oauth/authorize` |
| `client_id` | string | Yes | Same client ID used in `/oauth/authorize` |
| `code_verifier` | string | Yes | PKCE verifier matching the original `code_challenge` |

### Response `200 application/json`

```json
{
  "access_token": "<oauth_access_token>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "<refresh_token>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | Yes | OAuth access token for the MCP resource. This is not a Nojo JWT |
| `token_type` | string | Yes | Must be `Bearer` |
| `expires_in` | integer | Yes | Access token lifetime in seconds |
| `refresh_token` | string | Yes | Refresh token used to obtain new OAuth access tokens |
| `scope` | string | No | Omit, or return an empty string |

### Error Responses

All error bodies are `application/json` and may carry an optional `error_description`.

| Status | Body | Description |
|---|---|---|
| `400` | `{"error":"invalid_grant"}` | Invalid, expired, already-used, or mismatched authorization code |
| `400` | `{"error":"invalid_request"}` | Missing or invalid request fields |
| `500` | `{"error":"server_error"}` | Unexpected backend error |

### Required CORS Behavior

`/oauth/token` is called cross-origin by browser-hosted clients. This applies to APIs 3, 4, and 5 — they share the same endpoint.

| Requirement | Detail |
|---|---|
| Answer `OPTIONS` preflight | `204` with the headers below |
| `Access-Control-Allow-Origin` | `*` |
| `Access-Control-Allow-Methods` | `POST, OPTIONS` |
| `Access-Control-Allow-Headers` | `Authorization, Content-Type` |

## API 4: OAuth Token - Refresh Token Exchange

### `POST /oauth/token`

Exchanges a refresh token for a new OAuth access token and rotated refresh token.

### Request Headers

```http
Content-Type: application/x-www-form-urlencoded
```

### Request Body

```text
grant_type=refresh_token
&refresh_token=<refresh_token>
&client_id=<client_id>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `grant_type` | string | Yes | Must be `refresh_token` |
| `refresh_token` | string | Yes | Existing refresh token |
| `client_id` | string | Yes | Original OAuth client ID |

### Response `200 application/json`

```json
{
  "access_token": "<new_oauth_access_token>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "<new_refresh_token>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | Yes | New OAuth access token for the MCP resource |
| `token_type` | string | Yes | Must be `Bearer` |
| `expires_in` | integer | Yes | Access token lifetime in seconds |
| `refresh_token` | string | Yes | New rotated refresh token. The old refresh token must stop working |
| `scope` | string | No | Omit, or return an empty string |

### Error Responses

All error bodies are `application/json` and may carry an optional `error_description`.

| Status | Body | Description |
|---|---|---|
| `400` | `{"error":"invalid_grant"}` | Invalid, expired, revoked, or already-used refresh token |
| `400` | `{"error":"invalid_request"}` | Missing or invalid request fields |
| `500` | `{"error":"server_error"}` | Unexpected backend error |

## API 5: OAuth Token - MCP Token Exchange

### `POST /oauth/token`

Exchanges the user's OAuth access token for a short-lived Nojo JWT. This endpoint is called by the MCP server only.

### Request Headers

```http
Authorization: Basic base64(<mcp_client_id>:<mcp_client_secret>)
Content-Type: application/x-www-form-urlencoded
```

### Request Body

```text
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<oauth_access_token_from_client>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
```

| Field | Type | Required | Description |
|---|---|---|---|
| `grant_type` | string | Yes | Must be `urn:ietf:params:oauth:grant-type:token-exchange` |
| `subject_token` | string | Yes | OAuth access token received by the MCP server from the client |
| `subject_token_type` | string | Yes | Must be `urn:ietf:params:oauth:token-type:access_token` |

### Response `200 application/json`

```json
{
  "access_token": "<nojo_jwt>",
  "expires_in": 900,
  "sub": "<nojo_user_id>",
  "client_id": "<original_oauth_client_id>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | Yes | Short-lived Nojo JWT for the same user represented by `subject_token` |
| `expires_in` | integer | Yes | JWT lifetime in seconds. Maximum expected value is `900` |
| `sub` | string | Yes | Nojo user ID |
| `client_id` | string | Yes | Original OAuth public client ID, for auditing |

**This response shape is intentionally not RFC 8693.** The MCP server reads exactly `access_token`, `sub`, and `expires_in`, with `client_id` optional. Do not rename them to the RFC 8693 fields `issued_token_type` / `token_type` — the MCP server will reject the response and every tool call will fail.

### Error Responses

All error bodies are `application/json` and may carry an optional `error_description`. The `401` response must include a `WWW-Authenticate: Basic` header.

| Status | Body | Description |
|---|---|---|
| `401` | `{"error":"invalid_client"}` | Invalid MCP client credentials |
| `400` | `{"error":"invalid_grant"}` | Invalid, expired, revoked, wrong-resource, or unauthorized subject token |
| `400` | `{"error":"invalid_request"}` | Missing or invalid request fields |
| `500` | `{"error":"server_error"}` | Unexpected backend error |

The status codes matter. The MCP server treats `400` and `401` as "this token is no longer usable" and returns `401` to the client so it re-runs OAuth. Any other failure returns `500`, so the client keeps its token and retries. Returning `500` for a rejected token would loop; returning `400` for a backend outage would log users out.
