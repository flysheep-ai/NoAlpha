class StockAgentError(Exception):
    """Base error for the stock agent."""


class ProviderError(StockAgentError):
    """Provider failed after retries or returned invalid data."""


class ProviderTimeoutError(ProviderError):
    """Provider request timed out."""


class RateLimitError(ProviderError):
    """Provider rate limit was reached."""


class MalformedResponseError(ProviderError):
    """Provider returned malformed data."""


class NotFoundError(ProviderError):
    """Requested resource was not found."""


class PointInTimeViolation(StockAgentError):
    """Code attempted to use data unavailable as of the requested time."""


class LLMUnavailableError(StockAgentError):
    """Optional LLM synthesis service was unavailable."""
