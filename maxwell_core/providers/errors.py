"""Safe provider-independent failures consumed by request orchestration."""


class ProviderError(RuntimeError):
    """An upstream failure with a safe, user-visible description."""

    retryable = True
    cooldown = False


class ProviderRequestError(ProviderError):
    """A deterministic rejection; retrying an unchanged request cannot help."""

    retryable = False


class ProviderAuthenticationError(ProviderRequestError):
    cooldown = True


class ProviderInvalidRequestError(ProviderRequestError):
    pass


class ProviderMediaUnsupportedError(ProviderInvalidRequestError):
    pass


class ProviderRateLimitError(ProviderError):
    cooldown = True


class ProviderUsageExhaustedError(ProviderError):
    user_message = "The api is down cuz yall drained the usage and im not rich so wait like 2 hours"
    cooldown = True


class ProviderUnavailableError(ProviderError):
    def __init__(self, message: str, *, cooldown: bool = False):
        super().__init__(message)
        self.cooldown = cooldown


class ProviderEmptyResponseError(ProviderUnavailableError):
    user_message = "The model returned an empty response after retries. Please try again in a moment."
