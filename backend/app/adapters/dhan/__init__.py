"""Dhan-specific REST/reference-data implementation below the provider boundary."""

from app.adapters.base.errors import (
    ProviderRateLimitError,
    ProviderTimeoutError,
    UnsupportedProviderRequestError,
)
from app.adapters.dhan.adapter import (
    DhanRequestPacer,
    DhanRestAdapter,
    DhanRestContractDiscrepancyError,
)
from app.adapters.dhan.auth import (
    DhanAccessTokenProvider,
    DhanAuthManager,
    DhanStaticAccessTokenProvider,
)
from app.adapters.dhan.live import (
    DhanLiveFeedMode,
    DhanLiveReconnectPolicy,
    DhanLiveSocket,
    DhanLiveSubscriptionBatch,
    DhanLiveSubscriptionPlan,
    DhanLiveTransport,
    WebsocketsDhanLiveTransport,
    build_standard_live_url,
    decode_standard_live_packet,
    encode_live_request,
    plan_live_subscription_batches,
)
from app.adapters.dhan.models import (
    DhanCashEquityLiveUniverse,
    DhanFnoStockUniverse,
    DhanInstrumentReference,
)
from app.adapters.dhan.normalizer import (
    derive_equity_fno_universe,
    normalize_historical_payload,
    normalize_instrument_master,
    resolve_nse_cash_equity_live_universe,
)

__all__ = [
    "DhanCashEquityLiveUniverse",
    "DhanFnoStockUniverse",
    "DhanInstrumentReference",
    "DhanLiveFeedMode",
    "DhanLiveReconnectPolicy",
    "DhanLiveSocket",
    "DhanLiveSubscriptionBatch",
    "DhanLiveSubscriptionPlan",
    "DhanLiveTransport",
    "WebsocketsDhanLiveTransport",
    "build_standard_live_url",
    "decode_standard_live_packet",
    "encode_live_request",
    "DhanRequestPacer",
    "DhanRestAdapter",
    "DhanRestContractDiscrepancyError",
    "DhanAccessTokenProvider",
    "DhanAuthManager",
    "DhanStaticAccessTokenProvider",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "UnsupportedProviderRequestError",
    "derive_equity_fno_universe",
    "normalize_historical_payload",
    "normalize_instrument_master",
    "plan_live_subscription_batches",
    "resolve_nse_cash_equity_live_universe",
]
