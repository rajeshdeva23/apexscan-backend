"""Shared broker-neutral adapter contracts and boundary errors."""

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
    LiveMarketDataAdapter,
    SessionStatisticsSource,
)
from app.adapters.base.errors import (
    InvalidProviderDataError,
    NormalizationError,
    ProviderAuthenticationError,
    ProviderBoundaryError,
    ProviderContractViolationError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnknownProviderReferenceError,
    UnsupportedProviderRequestError,
)
from app.adapters.base.normalizer import TickNormalizer
from app.adapters.base.provider_coordinator import (
    ProviderCleanupError,
    ProviderConfigurationError,
    ProviderCoordinator,
    ProviderHealthCheckError,
    ProviderInitializationError,
    ProviderLifecycleError,
    ProviderOperationTimeoutError,
)

__all__ = [
    "BrokerAdapter",
    "HistoricalDataAdapter",
    "InstrumentDataAdapter",
    "InvalidProviderDataError",
    "LiveMarketDataAdapter",
    "NormalizationError",
    "ProviderAuthenticationError",
    "ProviderBoundaryError",
    "ProviderCleanupError",
    "ProviderConfigurationError",
    "ProviderCoordinator",
    "ProviderContractViolationError",
    "ProviderHealthCheckError",
    "ProviderInitializationError",
    "ProviderLifecycleError",
    "ProviderOperationTimeoutError",
    "ProviderNetworkError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "SessionStatisticsSource",
    "TickNormalizer",
    "UnsupportedProviderRequestError",
    "UnknownProviderReferenceError",
]
