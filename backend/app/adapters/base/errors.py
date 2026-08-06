"""Safe, provider-independent errors for the adapter boundary."""

from __future__ import annotations


class ProviderBoundaryError(RuntimeError):
    """Base error for a failure crossing the provider adapter boundary."""


class InvalidProviderDataError(ProviderBoundaryError):
    """Raised when provider data cannot meet a canonical contract."""

    def __init__(self) -> None:
        super().__init__("Provider data is invalid")


class NormalizationError(InvalidProviderDataError):
    """Raised when provider-shaped input cannot become canonical data safely."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Provider payload could not be normalized",)


class ProviderContractViolationError(ProviderBoundaryError):
    """Raised when an adapter is used outside its shared contract."""

    def __init__(self) -> None:
        super().__init__("Provider contract was violated")


class ProviderAuthenticationError(ProviderBoundaryError):
    """Raised when provider authentication or authorization is unavailable."""

    def __init__(self) -> None:
        super().__init__("Provider authentication failed")


class ProviderNetworkError(ProviderBoundaryError):
    """Raised when a provider request cannot reach its remote service."""

    def __init__(self) -> None:
        super().__init__("Provider network request failed")


class ProviderRateLimitError(ProviderBoundaryError):
    """Raised when a provider rejects a request because of its rate limit."""

    def __init__(self) -> None:
        super().__init__("Provider rate limit exceeded")


class ProviderTimeoutError(ProviderBoundaryError):
    """Raised when a provider request exceeds its configured timeout."""

    def __init__(self) -> None:
        super().__init__("Provider request timed out")


class ProviderUnavailableError(ProviderBoundaryError):
    """Raised when a provider is temporarily unable to serve a request."""

    def __init__(self) -> None:
        super().__init__("Provider is temporarily unavailable")


class UnsupportedProviderRequestError(ProviderBoundaryError):
    """Raised when a canonical request is outside a provider's supported scope."""

    def __init__(self) -> None:
        super().__init__("Provider request is unsupported")


class UnknownProviderReferenceError(ProviderBoundaryError):
    """Raised when a provider packet cannot resolve to a known canonical instrument."""

    def __init__(self) -> None:
        super().__init__("Provider reference is unknown")
