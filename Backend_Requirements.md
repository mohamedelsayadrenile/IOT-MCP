# Backend Requirements: MCP OAuth + Token Exchange

**The backend** runs the OAuth server (login, consent, tokens) and a token-exchange endpoint.

**The MCP server** (`https://41.32.195.157/mcp`) only does this:
1. Receives the client's OAuth token.
2. Exchanges it with the backend for a short-lived ReNile JWT.
3. Calls the existing ReNile API with that JWT.

```
Client ──OAuth login──► Backend ──access token──► Client ──► MCP
MCP ──token exchange──► Backend ──ReNile JWT──► MCP ──► ReNile API (user's own data)
```

## 1. What to send us
- [ ] **Issuer URL**: an exact HTTPS public hostname, with no path and no trailing slash, e.g. `https://renile-iot.com`
- [ ] **Token-exchange URL**, e.g. `https://renile-iot.com/oauth/token`
- [ ] **Exchange `audience` value**, e.g. `renile-api`
- [ ] **MCP `client_id` + `client_secret`**, sent securely. The secret must use only URL-safe characters.
- [ ] **Token lifetimes** for the access token, the refresh token, and the exchanged JWT (≤ 15 min)
- [ ] **Staging environment** plus test users: **2 normal users with different devices** and **1 admin**
- [ ] **One working sample exchange request** (`curl`)

## 2. OAuth server endpoints

### `GET /.well-known/oauth-authorization-server`
The response must contain these fields:
```json
{
  "issuer": "https://renile-iot.com",
  "authorization_endpoint": "https://renile-iot.com/oauth/authorize",
  "token_endpoint": "https://renile-iot.com/oauth/token",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
  "scopes_supported": ["devices:read", "readings:read"],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true
}
```

### `GET /oauth/authorize`
It opens in the user's browser and must:
- **Sign in** with the **existing ReNile login**.
- **Refuse admin accounts.**
- **Show a consent page**: "Allow Claude to read your ReNile devices and readings?"
- **Require PKCE S256.**
- **Check `resource`**: it must be `https://41.32.195.157/mcp`.
- **Issue a code** that is single-use and lasts 10 minutes or less.
- **Redirect** to `redirect_uri?code=…&state=…&iss=<issuer>`.
- **Support CIMD**: when `client_id` is a URL, fetch the JSON document there (only from `claude.ai` or `chatgpt.com`).
  - The document's `client_id` must equal the URL.
  - `redirect_uri` must be listed in the document. Ignore the port for `localhost` and `127.0.0.1`.

| Client | client_id | Redirect |
|---|---|---|
| Claude | `https://claude.ai/oauth/mcp-oauth-client-metadata` | `https://claude.ai/api/mcp/auth_callback` |
| Claude Code | `https://claude.ai/oauth/claude-code-client-metadata` | `http://localhost/callback` (any port) |
| ChatGPT | `https://chatgpt.com/oauth/client.json` | `https://chatgpt.com/connector_platform_oauth_redirect` |
| Codex | `https://chatgpt.com/oauth/codex/client.json` | `http://127.0.0.1/callback` (any port) |

### `POST /oauth/token`: code and refresh (called by the clients)
- **`authorization_code`**: check the code, `redirect_uri` and PKCE `code_verifier`. Public clients send no secret.
- **`refresh_token`**: issue a new refresh token each time (rotation).
- **Response:**

  ```json
  {"access_token":"…","token_type":"Bearer","expires_in":3600,"refresh_token":"…","scope":"devices:read readings:read"}
  ```
- **The access token must not be the ReNile JWT.**

## 3. Token exchange (called by the MCP only)
```
POST <TOKEN_EXCHANGE_URL>
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<client's OAuth access token>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&audience=<audience>
```

**The backend validates:** the MCP's credentials, that the token is valid and not revoked, that it was issued for `https://41.32.195.157/mcp`, and that the user is not an admin.

**Success (`200`).** These exact field names are required:
```json
{
  "access_token": "<ReNile JWT for the same user, ≤ 15 min>",
  "expires_in": 900,
  "sub": "<ReNile user _id>",
  "scope": "devices:read readings:read",
  "client_id": "<original client's client_id>"
}
```

**Errors:**

| Case | Status |
|---|---|
| Token invalid, expired or revoked, or the user is an admin | `400` `{"error":"invalid_grant"}` |
| Wrong MCP credentials | `401` `{"error":"invalid_client"}` |

## 4. Existing ReNile API: no changes
- The MCP calls `/api/v1/devices/names/` and `/api/v1/snapshot/` with `Authorization: JWT <exchanged JWT>`.
- It must return only that user's data: `401` if the JWT has expired, `403` if access is forbidden.

## 5. Rules
- The ReNile JWT is **never** sent to Claude, ChatGPT or Codex. It appears only in the exchange response to the MCP.
- The backend must be public HTTPS with a valid certificate. The authorize, token and metadata endpoints must all be on the issuer's domain.
- Revoke a user's tokens when their password changes, they are disabled, or they become an admin.
- Not needed: DCR (`/register`), introspection, OIDC.

## 6. Staging tests
1. The metadata URL returns the JSON in §2.
2. `codex mcp login` completes (login, consent, token).
3. The sample exchange returns a JWT that works on `/api/v1/devices/names/`.
4. User A and user B see different devices. The admin is refused.
5. After revoking a user's tokens, the exchange returns `400`.
