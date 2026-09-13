"""Errors raised while talking to the ReNile platform and its sign-in service."""


class RenileAPIError(Exception):
    """Raised when the ReNile API cannot be reached or rejects the request."""


class RenileAuthExpiredError(RenileAPIError):
    """The caller's token was rejected (401). They need to sign in again."""


class RenilePermissionError(RenileAPIError):
    """The token is good but does not grant access to what was asked for (403)."""


class TokenExchangeRejectedError(RenileAPIError):
    """The backend refused to exchange the OAuth token (400/401)."""
