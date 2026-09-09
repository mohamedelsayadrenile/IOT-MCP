"""Bearer-token handling for the remote server.

This server is an OAuth 2.1 resource server that does **not** validate tokens:
the ReNile platform is the sole authority on whether a token is good, and a bad
one fails upstream with 401. What lives here is the bookkeeping the MCP SDK needs
in order to behave correctly, nothing more.
"""

import hashlib
import logging
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

# Used when a token carries no client identity of its own. The value is not
# meaningful on its own -- `subject` is what separates one caller from another.
_FALLBACK_CLIENT_ID = "renile-mcp"


def _fingerprint(token: str) -> str:
    """A stable, non-reversible id for a token with no readable `sub`.

    Binding a session to this is stricter than binding it to a user id: it ties
    the session to that one token rather than to the person behind it.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class PassthroughTokenVerifier(TokenVerifier):
    """Reads a bearer token's claims without validating it.

    No signature check and no audience check: this server forwards the token
    upstream and lets the ReNile API decide. `sub`, `iss` and `exp` are decoded
    only because the SDK needs them to do two things correctly:

    * **Session ownership.** A Streamable HTTP session is bound to the principal
      that created it, derived from (client_id, iss, subject). Returning a
      constant for all three would make every user the same principal and
      silently disable that check -- one user's session id would then be a
      working credential for another user. `subject` is therefore never None.
    * **Expiry.** A token whose `expires_at` has passed is rejected by the SDK's
      bearer middleware, which answers with the 401 + WWW-Authenticate challenge
      that prompts a client to refresh or re-run the OAuth flow. Without it, an
      expired token reaches a tool and dies as an opaque error string instead.

    A forged `exp` buys an attacker nothing: the request still fails at the
    ReNile API. This is bookkeeping, not verification.
    """

    def __init__(self, issuer_url: str) -> None:
        self._issuer_url = issuer_url.rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        token = token.strip()
        if not token:
            return None

        claims = self._decode(token)
        if claims is None:
            # Opaque (non-JWT) token: nothing is readable, so bind the session to
            # the token itself and let expiry be discovered upstream.
            return AccessToken(
                token=token,
                client_id=_FALLBACK_CLIENT_ID,
                scopes=[],
                subject=_fingerprint(token),
            )

        issuer = str(claims.get("iss", "")).rstrip("/")
        if issuer != self._issuer_url:
            # A routing check, not a security check: it stops a token minted for
            # somewhere else from being blindly forwarded to the ReNile API.
            logger.warning("token_rejected reason=issuer_mismatch issuer=%r", issuer)
            return None

        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(
                claims.get("azp") or claims.get("client_id") or _FALLBACK_CLIENT_ID
            ),
            scopes=[],
            # Never None: see the class docstring.
            subject=str(claims.get("sub") or _fingerprint(token)),
            expires_at=int(expires_at) if isinstance(expires_at, (int, float)) else None,
            claims={"iss": issuer},
        )

    def _decode(self, token: str) -> dict[str, Any] | None:
        """Read a JWT's claims without verifying it. None if it is not a JWT."""
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            return None
