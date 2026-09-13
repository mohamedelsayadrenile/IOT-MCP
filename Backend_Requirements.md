# Backend API Requirements

The MCP server needs the ReNile backend to provide OAuth login and token exchange.
The MCP server does not create users, store passwords, or issue tokens.

## Flow

```text
Client -> Backend: OAuth login and consent
Client -> MCP: Calls /mcp with backend OAuth access token
MCP -> Backend: Exchanges OAuth access token for short-lived ReNile JWT
MCP -> ReNile API: Calls existing APIs with the ReNile JWT
```

## Required Values From Backend

Please provide these values before integration:

| Value | Example | Notes |
|---|---|---|
| Issuer URL | `https://renile-iot.com` | HTTPS, no path, no trailing slash |
| Authorization URL | `https://renile-iot.com/oauth/authorize` | Browser login endpoint |
| Token URL | `https://renile-iot.com/oauth/token` | Used for OAuth code, refresh, and token exchange |
| MCP client ID | `renile-mcp` | Confidential client used by MCP only |
| MCP client secret | secure random string | Send securely, URL-safe characters only |
| Access token lifetime | `3600` seconds | OAuth access token returned to client |
| Refresh token lifetime | backend choice | Must support rotation |
| Exchanged JWT lifetime | `900` seconds | Must be 15 minutes or less |
| Staging users | 2 normal users, 1 admin | Normal users must have different devices |

## API 1: OAuth Metadata

### `GET /.well-known/oauth-authorization-server`

Used by OAuth clients to discover backend OAuth endpoints.

### Request

No body.

### Response `200 application/json`

```json
{
  "issuer": "https://renile-iot.com",
  "authorization_endpoint": "https://renile-iot.com/oauth/authorize",
  "token_endpoint": "https://renile-iot.com/oauth/token",
  "response_types_supported": ["code"],
  "grant_types_supported": [
    "authorization_code",
    "refresh_token",
    "urn:ietf:params:oauth:grant-type:token-exchange"
  ],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true
}
```

## API 2: Start OAuth Login

### `GET /oauth/authorize`

Opens in the user's browser. The backend signs the user in with the existing ReNile login and asks for consent.

### Request Query Parameters

| Field | Type | Required | Notes |
|---|---|---|---|
| `response_type` | string | Yes | Must be `code` |
| `client_id` | string | Yes | May be a Client ID Metadata Document URL |
| `redirect_uri` | string | Yes | Must match the client metadata |
| `state` | string | Yes | Return unchanged in redirect |
| `code_challenge` | string | Yes | PKCE challenge |
| `code_challenge_method` | string | Yes | Must be `S256` |
| `resource` | string | Yes | Must be `https://41.32.195.157/mcp` |

### Successful Response

Redirect the browser to:

```text
<redirect_uri>?code=<authorization_code>&state=<state>&iss=<issuer>
```

### Error Response

Redirect the browser to:

```text
<redirect_uri>?error=<error_code>&state=<state>
```

### Required Behavior

| Rule | Requirement |
|---|---|
| Login | Use existing ReNile user login |
| Consent | Show: `Allow this client to read your ReNile devices and readings?` |
| Admin users | Must be refused |
| Authorization code | Single-use, expires in 10 minutes or less |
| PKCE | Require `S256` |
| Resource | Must equal `https://41.32.195.157/mcp` |
| Access scope | User can only access their own devices/readings |

### Supported Public Clients

| Client | `client_id` | Redirect URI |
|---|---|---|
| Claude | `https://claude.ai/oauth/mcp-oauth-client-metadata` | `https://claude.ai/api/mcp/auth_callback` |
| Claude Code | `https://claude.ai/oauth/claude-code-client-metadata` | `http://localhost/callback` with any port |
| ChatGPT | `https://chatgpt.com/oauth/client.json` | `https://chatgpt.com/connector_platform_oauth_redirect` |
| Codex | `https://chatgpt.com/oauth/codex/client.json` | `http://127.0.0.1/callback` with any port |

When `client_id` is a URL, fetch the metadata document only from `claude.ai` or `chatgpt.com`.
The metadata document's `client_id` must match the URL, and the `redirect_uri` must be listed in the document.

## API 3: Exchange Authorization Code

### `POST /oauth/token`

Used by Claude, ChatGPT, Codex, and similar clients after login.

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

### Request Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `grant_type` | string | Yes | Must be `authorization_code` |
| `code` | string | Yes | Single-use authorization code |
| `redirect_uri` | string | Yes | Must match authorize request |
| `client_id` | string | Yes | Original OAuth client ID |
| `code_verifier` | string | Yes | Must validate against `code_challenge` |

### Response `200 application/json`

```json
{
  "access_token": "<oauth_access_token>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "<refresh_token>"
}
```

### Response Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `access_token` | string | Yes | OAuth token for the MCP resource, not a ReNile JWT |
| `token_type` | string | Yes | Must be `Bearer` |
| `expires_in` | integer | Yes | Lifetime in seconds |
| `refresh_token` | string | Yes | Used to get new OAuth access tokens |

### Error Response `400 application/json`

```json
{
  "error": "invalid_grant"
}
```

## API 4: Refresh OAuth Token

### `POST /oauth/token`

Used by clients when the OAuth access token expires.

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

### Request Schema

| Field | Type | Required | Notes |
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

### Response Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `access_token` | string | Yes | New OAuth access token |
| `token_type` | string | Yes | Must be `Bearer` |
| `expires_in` | integer | Yes | Lifetime in seconds |
| `refresh_token` | string | Yes | New rotated refresh token |

### Error Response `400 application/json`

```json
{
  "error": "invalid_grant"
}
```

Refresh tokens must be rotated. After a refresh token is used, the old refresh token must stop working.

## API 5: Token Exchange For MCP

### `POST /oauth/token`

Called only by the MCP server. This exchanges the user's OAuth access token for a short-lived ReNile JWT.

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

### Request Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `grant_type` | string | Yes | Must be `urn:ietf:params:oauth:grant-type:token-exchange` |
| `subject_token` | string | Yes | OAuth access token received from the client |
| `subject_token_type` | string | Yes | Must be `urn:ietf:params:oauth:token-type:access_token` |

### Backend Validation

The backend must validate:

| Check | Requirement |
|---|---|
| MCP credentials | Basic auth client ID and secret are valid |
| OAuth token | Valid, not expired, not revoked |
| Resource | Token was issued for `https://41.32.195.157/mcp` |
| User | User exists and is not an admin |
| JWT subject | Returned ReNile JWT belongs to the same user |

### Response `200 application/json`

```json
{
  "access_token": "<renile_jwt>",
  "expires_in": 900,
  "sub": "<renile_user_id>",
  "client_id": "<original_oauth_client_id>"
}
```

### Response Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `access_token` | string | Yes | Short-lived ReNile JWT for the same user |
| `expires_in` | integer | Yes | JWT lifetime in seconds, max `900` |
| `sub` | string | Yes | ReNile user `_id` |
| `client_id` | string | Yes | Original OAuth client ID, for auditing |

### Error Responses

| Case | Status | Body |
|---|---|---|
| Invalid MCP credentials | `401` | `{"error":"invalid_client"}` |
| Invalid, expired, or revoked OAuth token | `400` | `{"error":"invalid_grant"}` |
| Token issued for wrong resource | `400` | `{"error":"invalid_grant"}` |
| User is admin or disabled | `400` | `{"error":"invalid_grant"}` |

## Existing ReNile APIs Used By MCP

These APIs already exist and should not change.

## API 6: Device Names

### `GET /api/v1/devices/names/`

### Request Headers

```http
Authorization: JWT <renile_jwt_from_token_exchange>
```

### Response `200 application/json`

The current backend response format can stay the same. It must include only devices owned by the JWT user.

Example:

```json
[
  {
    "_id": "<device_id>",
    "name": "Greenhouse 1"
  }
]
```

### Error Responses

| Case | Status |
|---|---|
| JWT expired or invalid | `401` |
| User cannot access resource | `403` |

## API 7: Latest Snapshot

### `GET /api/v1/snapshot/`

### Request Headers

```http
Authorization: JWT <renile_jwt_from_token_exchange>
```

### Response `200 application/json`

The current backend response format can stay the same. It must include only readings for devices owned by the JWT user.

Example fields used by MCP:

```json
[
  {
    "device": "Greenhouse 1",
    "sensor": "temperature",
    "value": 24.5,
    "unit": "C",
    "lower_limit": 10,
    "upper_limit": 35,
    "status": "normal",
    "timestamp": "2026-09-13T10:00:00Z"
  }
]
```

### Error Responses

| Case | Status |
|---|---|
| JWT expired or invalid | `401` |
| User cannot access resource | `403` |

## Security Rules

| Rule | Requirement |
|---|---|
| HTTPS | All OAuth endpoints must be public HTTPS with a valid certificate |
| Token separation | OAuth access token must not be the ReNile JWT |
| JWT exposure | ReNile JWT is returned only to the MCP server during token exchange |
| Admin users | Admin users cannot authorize MCP access |
| Revocation | Revoke active OAuth tokens when password changes, user is disabled, or user becomes admin |
| Data isolation | Every API response must be limited to the authenticated user |

## Acceptance Tests

1. Metadata endpoint returns all required OAuth fields.
2. OAuth login works for a normal user and refuses an admin user.
3. Authorization code exchange returns an OAuth access token and refresh token.
4. Refresh token exchange rotates the refresh token.
5. Token exchange returns a ReNile JWT that works on `/api/v1/devices/names/`.
6. User A and User B receive different devices/readings.
7. Revoked, expired, disabled, or admin users return `400 {"error":"invalid_grant"}` during token exchange.
8. Wrong MCP client credentials return `401 {"error":"invalid_client"}`.
