class RenileAPIError(Exception):
    pass


class RenileAuthExpiredError(RenileAPIError):
    pass


class RenilePermissionError(RenileAPIError):
    pass


class TokenExchangeRejectedError(RenileAPIError):
    pass
