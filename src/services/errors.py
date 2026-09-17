class NojoAPIError(Exception):
    pass


class TokenExchangeRejectedError(NojoAPIError):
    pass
