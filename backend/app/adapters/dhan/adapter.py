"""Direct asynchronous DhanHQ v2 REST adapter bounded by the provider contracts."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
    LiveMarketDataAdapter,
    SessionStatisticsSource,
)
from app.adapters.base.errors import (
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
from app.adapters.dhan.auth import (
    DhanAccessTokenProvider,
    DhanAuthManager,
    DhanStaticAccessTokenProvider,
)
from app.adapters.dhan.live import (
    DhanLiveReconnectPolicy,
    DhanLiveSocket,
    DhanLiveSubscriptionBatch,
    DhanLiveSubscriptionPlan,
    DhanLiveTransport,
    WebsocketsDhanLiveTransport,
    build_standard_live_url,
    decode_standard_live_packet,
    encode_live_disconnect_request,
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
    normalize_session_statistics_payload,
    resolve_nse_cash_equity_live_universe,
)
from app.core.config import Settings
from app.schemas.market_data import (
    Candle,
    FeedContinuity,
    FeedContinuityEvent,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
    SessionStatisticsObservation,
    SubscriptionRequest,
)

_DEFAULT_API_BASE_URL = "https://api.dhan.co/v2"
_MARKET_QUOTE_OHLC_ENDPOINT = "/marketfeed/ohlc"
_MARKET_QUOTE_MAX_INSTRUMENTS = 1000
_INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
_INDIAN_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
_SUPPORTED_INTRADAY_INTERVALS = frozenset({1, 5, 15, 25, 60})
_INTRADAY_MAX_RANGE = timedelta(days=90)
_DATA_API_MINIMUM_REQUEST_INTERVAL_SECONDS = 0.2
_AUTHENTICATION_ERROR_CODES = frozenset({"DH-901", "DH-902", "806", "807", "808", "809", "810"})
_RATE_LIMIT_ERROR_CODES = frozenset({"DH-904", "805"})
_UNSUPPORTED_REQUEST_ERROR_CODES = frozenset({"DH-905", "DH-907", "811", "812", "813", "814"})
_TRANSIENT_ERROR_CODES = frozenset({"DH-908", "DH-909", "800"})

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LiveConsumer:
    """One registered live-stream consumer with its own ordered fan-out buffer."""

    sequence: int
    request: SubscriptionRequest
    buffer: deque[MarketData] = field(default_factory=deque)


class DhanRestContractDiscrepancyError(ProviderBoundaryError):
    """Safe live-smoke signal that Dhan requires an undocumented client identifier."""

    def __init__(
        self,
        *,
        endpoint: str,
        http_status: int,
        error_code: str | None,
        error_type: str | None,
        observed_requirement: str,
    ) -> None:
        super().__init__("Dhan REST contract discrepancy detected")
        self.endpoint = endpoint
        self.http_status = http_status
        self.error_code = error_code
        self.error_type = error_type
        self.observed_requirement = observed_requirement
        self.documented_request_contract = "endpoint-specific DhanHQ v2 documented fields only"


class DhanRequestPacer:
    """Serialize documented Dhan Data API requests below five requests per second."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float = _DATA_API_MINIMUM_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Wait just enough to preserve the documented per-second request bound."""
        async with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                remaining = self._minimum_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at = self._clock()


class _LiveFeedStaleError(Exception):
    """Internal signal: an expected live feed produced no valid tick within the stale window."""


class DhanRestAdapter(
    BrokerAdapter,
    LiveMarketDataAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
    SessionStatisticsSource,
):
    """Dhan reference, historical, standard-feed, and market-quote behavior behind contracts."""

    capabilities = frozenset(
        {
            ProviderCapability.LIVE_MARKET_DATA,
            ProviderCapability.HISTORICAL_DATA,
            ProviderCapability.INSTRUMENTS,
            ProviderCapability.MARKET_QUOTE,
        }
    )

    def __init__(
        self,
        *,
        access_token: SecretStr | None = None,
        token_provider: DhanAccessTokenProvider | None = None,
        api_base_url: str = _DEFAULT_API_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        live_smoke_enabled: bool = False,
        request_pacer: DhanRequestPacer | None = None,
        live_client_id: SecretStr | None = None,
        websocket_transport: DhanLiveTransport | None = None,
        live_reconnect_policy: DhanLiveReconnectPolicy | None = None,
        live_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        live_random: Callable[[], float] = random.random,
        live_continuity_sink: Callable[[FeedContinuityEvent], None] | None = None,
        live_stale_timeout_seconds: float | None = None,
        live_hard_stale_timeout_seconds: float | None = None,
        live_session_predicate: Callable[[], bool] | None = None,
        live_clock: Callable[[], float] = monotonic,
    ) -> None:
        if (access_token is None) == (token_provider is None):
            raise ProviderAuthenticationError()

        if access_token is not None:
            self._token_provider: DhanAccessTokenProvider = DhanStaticAccessTokenProvider(
                access_token
            )
        elif token_provider is not None:
            self._token_provider = token_provider
        else:
            raise ProviderAuthenticationError()
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._live_smoke_enabled = live_smoke_enabled
        self._request_pacer = request_pacer or DhanRequestPacer()
        self._api_client: httpx.AsyncClient | None = None
        self._reference_client: httpx.AsyncClient | None = None
        self._references: dict[Instrument, DhanInstrumentReference] = {}
        self._live_client_id = live_client_id
        self._websocket_transport = websocket_transport or WebsocketsDhanLiveTransport()
        self._live_socket: DhanLiveSocket | None = None
        self._desired_live_requests: dict[SubscriptionRequest, int] = {}
        self._live_consumers: dict[int, _LiveConsumer] = {}
        self._live_consumer_sequence = 0
        self._live_cash_references: tuple[DhanInstrumentReference, ...] = ()
        self._desired_live_plan: DhanLiveSubscriptionPlan | None = None
        self._active_live_batches: tuple[DhanLiveSubscriptionBatch, ...] = ()
        self._live_status: ProviderStatus | None = None
        self._live_state_lock = asyncio.Lock()
        self._live_receive_lock = asyncio.Lock()
        self._live_reconnect_policy = live_reconnect_policy or DhanLiveReconnectPolicy()
        self._live_sleep = live_sleep
        self._live_random = live_random
        self._live_continuity_sink = live_continuity_sink
        self._live_stale_timeout_seconds = live_stale_timeout_seconds
        self._live_hard_stale_timeout_seconds = live_hard_stale_timeout_seconds
        self._live_session_predicate = live_session_predicate
        self._live_clock = live_clock
        self._last_valid_event_at: float | None = None
        self._suspect_stale_logged = False

    def _stale_watchdog_active(self) -> bool:
        """True only when a stale timeout is configured and an expected LIVE_SESSION is active."""
        return (
            self._live_stale_timeout_seconds is not None
            and self._live_session_predicate is not None
            and self._live_session_predicate()
        )

    def _emit_continuity(self, status: FeedContinuity) -> None:
        """Emit a broker-neutral live-continuity fact to the injected sink, if any."""
        if self._live_continuity_sink is not None:
            self._live_continuity_sink(
                FeedContinuityEvent(status=status, observed_at=datetime.now(UTC))
            )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        live_continuity_sink: Callable[[FeedContinuityEvent], None] | None = None,
        live_session_predicate: Callable[[], bool] | None = None,
    ) -> DhanRestAdapter:
        """Create the concrete adapter from centralized, redacted application settings.

        ``live_continuity_sink`` receives broker-neutral feed-continuity facts (ADR-006)
        from the live stream; the composition layer binds it to the Market Engine.
        """
        if settings.dhan_auth_mode == "totp":
            return cls(
                token_provider=DhanAuthManager.from_settings(settings, transport=transport),
                api_base_url=settings.dhan_rest_base_url,
                timeout_seconds=settings.dhan_rest_timeout_seconds,
                transport=transport,
                live_smoke_enabled=settings.dhan_live_smoke_enabled,
                live_client_id=settings.dhan_client_id,
                live_continuity_sink=live_continuity_sink,
                live_stale_timeout_seconds=settings.dhan_live_stale_timeout_seconds,
                live_hard_stale_timeout_seconds=settings.dhan_live_hard_stale_timeout_seconds,
                live_session_predicate=live_session_predicate,
            )
        if settings.dhan_access_token is None:
            raise ProviderAuthenticationError()
        return cls(
            access_token=settings.dhan_access_token,
            api_base_url=settings.dhan_rest_base_url,
            timeout_seconds=settings.dhan_rest_timeout_seconds,
            transport=transport,
            live_smoke_enabled=settings.dhan_live_smoke_enabled,
            live_client_id=settings.dhan_client_id,
            live_continuity_sink=live_continuity_sink,
            live_stale_timeout_seconds=settings.dhan_live_stale_timeout_seconds,
            live_hard_stale_timeout_seconds=settings.dhan_live_hard_stale_timeout_seconds,
            live_session_predicate=live_session_predicate,
        )

    async def connect(self) -> None:
        """Create adapter-owned HTTP clients without making a provider request."""
        if self._api_client is not None:
            return

        timeout = httpx.Timeout(self._timeout_seconds)
        self._api_client = httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=timeout,
            transport=self._transport,
        )
        self._reference_client = httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def disconnect(self) -> None:
        """Close every adapter-owned resource; a socket-close failure never leaks the clients.

        Each owned resource (live socket, HTTP API/reference clients, token provider) is
        closed independently so one failure cannot skip the rest. The first error is
        re-raised after all cleanup runs, so failures are surfaced rather than swallowed.
        """
        errors: list[BaseException] = []
        async with self._live_state_lock:
            try:
                await self._close_live_socket()
            except Exception as error:  # noqa: BLE001 - continue closing every owned resource
                errors.append(error)
        clients = (self._api_client, self._reference_client)
        self._api_client = None
        self._reference_client = None
        for client in clients:
            if client is not None:
                try:
                    await client.aclose()
                except Exception as error:  # noqa: BLE001 - continue closing every owned resource
                    errors.append(error)
        try:
            await self._token_provider.disconnect()
        except Exception as error:  # noqa: BLE001 - continue closing every owned resource
            errors.append(error)
        if errors:
            raise errors[0]

    async def get_health(self) -> ProviderHealth:
        """Use the documented authenticated profile endpoint as the bounded health probe."""
        if self._live_status is not None:
            return ProviderHealth(status=self._live_status, observed_at=datetime.now(UTC))
        await self._request_api_json("GET", "/profile")
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=datetime.now(UTC))

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Fetch the documented detailed Dhan master and retain private request references."""
        client = self._require_reference_client()
        try:
            response = await client.get(_INSTRUMENT_MASTER_URL)
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError() from error
        except httpx.RequestError as error:
            raise ProviderNetworkError() from error

        self._raise_for_error(response, _INSTRUMENT_MASTER_URL)
        try:
            references = normalize_instrument_master(response.text)
        except NormalizationError:
            raise
        except Exception as error:
            raise NormalizationError() from error

        self._references = {reference.instrument: reference for reference in references}
        return tuple(reference.instrument for reference in references)

    def load_fno_stock_universe(self) -> DhanFnoStockUniverse:
        """Return a deterministic Dhan-only F&O stock universe after master loading."""
        if not self._references:
            raise ProviderContractViolationError()
        return derive_equity_fno_universe(tuple(self._references.values()))

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        """Return the private, validated cash-equity mapping used by the V1 live scanner."""
        if not self._references:
            raise ProviderContractViolationError()
        return resolve_nse_cash_equity_live_universe(tuple(self._references.values()))

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        """Yield canonical standard-feed events for an already validated cash-equity request."""
        live_universe = self.load_nse_cash_equity_live_universe()
        if (
            live_universe.missing_underlyings
            or live_universe.ambiguous_underlyings
            or live_universe.symbol_mismatches
        ):
            raise ProviderContractViolationError()
        consumer: _LiveConsumer | None = None
        try:
            consumer = await self._register_live_consumer(request, live_universe.cash_references)
            while True:
                try:
                    event = await self._next_live_event(consumer)
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except ProviderBoundaryError:
                    self._live_status = ProviderStatus.DEGRADED
                    raise
                except Exception as error:
                    self._live_status = ProviderStatus.DEGRADED
                    raise ProviderNetworkError() from error
                yield event
        finally:
            if consumer is not None:
                await self._unregister_live_consumer(consumer, live_universe.cash_references)

    async def _next_live_event(self, consumer: _LiveConsumer) -> MarketData:
        """Return the next event for one consumer, driving a single shared receive loop.

        The first idle consumer reads a frame, fans its canonical events out to
        every matching consumer's buffer, then all consumers drain their own
        buffers. Each frame is read once and each event reaches each consumer
        exactly once regardless of which consumer performed the read.
        """
        while True:
            if consumer.buffer:
                return consumer.buffer.popleft()
            async with self._live_receive_lock:
                if consumer.buffer:
                    continue
                try:
                    packet = await self._receive_live_frame()
                    self._distribute_live_packet(packet)
                except (ConnectionError, OSError, TimeoutError):
                    await self._reconnect_live_subscription()
                except _LiveFeedStaleError:
                    logger.warning("Dhan live feed stale during live session; reconnecting")
                    await self._reconnect_live_subscription()

    async def _receive_live_frame(self) -> bytes | str:
        """Receive one frame, enforcing a session-gated two-threshold stale deadline.

        During an expected ``LIVE_SESSION`` with an active subscription, elapsed time since
        the last VALID canonical market event is measured on a monotonic clock. Crossing the
        SOFT threshold (``dhan_live_stale_timeout_seconds``) logs a single *suspected stale*
        warning but does NOT reconnect — this tolerates the legitimate low-tick lulls seen at
        session boundaries. Only crossing the HARD threshold
        (``dhan_live_hard_stale_timeout_seconds``) raises :class:`_LiveFeedStaleError`, which
        the caller resolves with one bounded reconnect. Outside a live session the deadline is
        not enforced, so market-closed/holiday silence never triggers a reconnect.
        """
        socket = self._require_live_socket()
        soft = self._live_stale_timeout_seconds
        if soft is None or not self._stale_watchdog_active():
            return await socket.recv()
        hard = self._live_hard_stale_timeout_seconds
        if hard is None:
            hard = soft
        now = self._live_clock()
        if self._last_valid_event_at is None:
            self._last_valid_event_at = now
        elapsed = now - self._last_valid_event_at
        if elapsed >= hard:
            raise _LiveFeedStaleError()
        self._note_suspect_stale(elapsed, soft, hard)
        try:
            return await asyncio.wait_for(socket.recv(), hard - elapsed)
        except TimeoutError as error:
            if self._stale_watchdog_active():
                raise _LiveFeedStaleError() from error
            # The session ended while waiting: reset the baseline and wait without a
            # deadline so a legitimately quiet closed market never reconnects.
            self._last_valid_event_at = None
            self._suspect_stale_logged = False
            return await socket.recv()

    def _note_suspect_stale(self, elapsed: float, soft: float, hard: float) -> None:
        """Log one degraded 'suspected stale' warning per episode (no reconnect).

        Fires once when the soft threshold is first crossed within a stale episode; the flag
        is cleared whenever a valid canonical event resets the freshness window (or a
        reconnect is marked), so each distinct episode warns at most once (no log spam).
        """
        if self._suspect_stale_logged or elapsed < soft:
            return
        logger.warning(
            "Dhan live feed suspected stale: no canonical market event for %.0fs "
            "(monitoring; hard-reconnect threshold %.0fs)",
            elapsed,
            hard,
        )
        self._suspect_stale_logged = True

    def _distribute_live_packet(self, packet: bytes | str) -> None:
        """Decode one frame and buffer its events per consumer, discarding bad frames.

        Malformed, unsupported, or unresolvable frames are logged and dropped so
        the stream keeps processing later valid frames (05 §12.2). A feed
        disconnect surfaces as a ``ConnectionError`` for the caller to reconnect.
        """
        if not isinstance(packet, bytes):
            logger.warning("Discarded a non-binary Dhan live frame")
            return
        try:
            events = decode_standard_live_packet(packet, self._live_cash_references)
        except (
            NormalizationError,
            UnsupportedProviderRequestError,
            UnknownProviderReferenceError,
        ):
            logger.warning("Discarded a malformed Dhan live frame")
            return
        if events:
            # A frame that decodes to >=1 canonical event is a valid market tick; reset the
            # stale window here (not merely on any received frame) so silent-but-alive feeds
            # are still detected. Also end any in-progress suspect-stale episode.
            self._last_valid_event_at = self._live_clock()
            self._suspect_stale_logged = False
        for event in events:
            for consumer in self._live_consumers.values():
                if _event_matches_request(event, consumer.request):
                    consumer.buffer.append(event)

    async def _register_live_consumer(
        self,
        request: SubscriptionRequest,
        cash_references: tuple[DhanInstrumentReference, ...],
    ) -> _LiveConsumer:
        """Register one fan-out consumer and reconcile the deduplicated provider intent."""
        async with self._live_state_lock:
            self._live_cash_references = cash_references
            consumer = _LiveConsumer(sequence=self._live_consumer_sequence, request=request)
            self._live_consumer_sequence += 1
            self._live_consumers[consumer.sequence] = consumer
            self._desired_live_requests[request] = self._desired_live_requests.get(request, 0) + 1
            try:
                plan = self._effective_live_plan(cash_references)
                self._desired_live_plan = plan
                await self._connect_live_socket()
                await self._reconcile_live_subscriptions(plan)
            except Exception:
                self._live_consumers.pop(consumer.sequence, None)
                self._discard_desired_live_request(request)
                self._desired_live_plan = (
                    self._effective_live_plan(cash_references)
                    if self._desired_live_requests
                    else None
                )
                raise
        return consumer

    async def _unregister_live_consumer(
        self,
        consumer: _LiveConsumer,
        cash_references: tuple[DhanInstrumentReference, ...],
    ) -> None:
        """Release one consumer without removing subscriptions others still require."""
        async with self._live_state_lock:
            self._live_consumers.pop(consumer.sequence, None)
            self._discard_desired_live_request(consumer.request)

            if not self._desired_live_requests:
                self._desired_live_plan = None
                await self._unsubscribe_live_plan()
                return

            plan = self._effective_live_plan(cash_references)
            self._desired_live_plan = plan
            await self._reconcile_live_subscriptions(plan)

    def _discard_desired_live_request(self, request: SubscriptionRequest) -> None:
        """Release one request reference without performing provider I/O."""
        request_count = self._desired_live_requests.get(request, 0)
        if request_count <= 1:
            self._desired_live_requests.pop(request, None)
        else:
            self._desired_live_requests[request] = request_count - 1

    def _effective_live_plan(
        self,
        cash_references: tuple[DhanInstrumentReference, ...],
    ) -> DhanLiveSubscriptionPlan:
        """Combine all consumers into one deterministic provider subscription intent."""
        instruments = tuple(
            sorted(
                {
                    instrument
                    for request in self._desired_live_requests
                    for instrument in request.instruments
                },
                key=lambda instrument: (
                    instrument.exchange,
                    instrument.symbol,
                    instrument.instrument_class.value,
                ),
            )
        )
        data_types = frozenset(
            data_type for request in self._desired_live_requests for data_type in request.data_types
        )
        return plan_live_subscription_batches(
            SubscriptionRequest(instruments=instruments, data_types=data_types),
            cash_references,
        )

    async def _connect_live_socket(self) -> None:
        if self._live_socket is not None:
            return
        client_id = self._live_client_id
        if client_id is None or not client_id.get_secret_value().strip():
            raise ProviderAuthenticationError()
        token = await self._token_provider.get_access_token()
        try:
            self._live_socket = await self._websocket_transport.connect(
                build_standard_live_url(
                    access_token=token.get_secret_value(),
                    client_id=client_id.get_secret_value(),
                ),
                self._timeout_seconds,
            )
        except Exception as error:
            self._live_status = ProviderStatus.DOWN
            raise ProviderNetworkError() from error
        self._emit_continuity(FeedContinuity.CONNECTED)

    async def _reconcile_live_subscriptions(self, plan: DhanLiveSubscriptionPlan) -> None:
        socket = self._require_live_socket()
        if self._active_live_batches and self._active_live_batches != plan.batches:
            await self._unsubscribe_live_plan()
        active_batches = list(self._active_live_batches)
        try:
            for batch in plan.batches:
                if batch in active_batches:
                    continue
                await socket.send(encode_live_request(batch.as_request_payload()))
                active_batches.append(batch)
        except Exception as error:
            self._active_live_batches = tuple(active_batches)
            self._live_status = ProviderStatus.DEGRADED
            raise ProviderNetworkError() from error
        self._active_live_batches = tuple(active_batches)
        self._live_status = ProviderStatus.HEALTHY

    async def _reconnect_live_subscription(self) -> None:
        """Reconnect only a bounded number of times and restore desired state on success."""
        async with self._live_state_lock:
            await self._mark_live_connection_lost()
            logger.warning("Dhan live feed reconnect started")
            for attempt in range(1, self._live_reconnect_policy.maximum_attempts + 1):
                await self._live_sleep(
                    self._live_reconnect_policy.delay_for_attempt(attempt, self._live_random())
                )
                try:
                    await self._connect_live_socket()
                    plan = self._desired_live_plan
                    if plan is None:
                        return
                    await self._reconcile_live_subscriptions(plan)
                    self._emit_continuity(FeedContinuity.RECONNECTED)
                    logger.info("Dhan live feed reconnect succeeded on attempt %d", attempt)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._mark_live_connection_lost()
            logger.error(
                "Dhan live feed reconnect failed after %d attempts",
                self._live_reconnect_policy.maximum_attempts,
            )
            raise ProviderUnavailableError()

    async def _unsubscribe_live_plan(self) -> None:
        socket = self._live_socket
        active_batches = self._active_live_batches
        self._active_live_batches = ()
        if socket is None:
            return
        for batch in active_batches:
            try:
                await socket.send(encode_live_request(batch.as_request_payload(unsubscribe=True)))
            except Exception:
                self._live_status = ProviderStatus.DEGRADED
                return

    async def _close_live_socket(self) -> None:
        socket = self._live_socket
        self._live_socket = None
        self._live_consumers.clear()
        self._desired_live_requests.clear()
        self._desired_live_plan = None
        self._active_live_batches = ()
        if socket is not None:
            try:
                await socket.send(encode_live_disconnect_request())
            except Exception:
                self._live_status = ProviderStatus.DEGRADED
            try:
                await socket.close()
            except Exception as error:
                raise ProviderNetworkError() from error
        if self._live_status is not None:
            self._live_status = ProviderStatus.DOWN

    async def _mark_live_connection_lost(self) -> None:
        """Drop only actual provider state while retaining desired subscriptions for recovery."""
        socket = self._live_socket
        self._live_socket = None
        self._active_live_batches = ()
        self._live_status = ProviderStatus.DEGRADED
        self._last_valid_event_at = None
        self._suspect_stale_logged = False
        if socket is not None:
            self._emit_continuity(FeedContinuity.CONTINUITY_LOST)
            try:
                await socket.close()
            except Exception:
                return

    def _require_live_socket(self) -> DhanLiveSocket:
        if self._live_socket is None:
            raise ProviderContractViolationError()
        return self._live_socket

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Load documented daily or intraday history and return canonical candles only."""
        endpoint = _historical_endpoint(request.interval)
        reference = self._references.get(request.instrument)
        if reference is None:
            raise UnsupportedProviderRequestError()

        candles: list[Candle] = []
        for chunk in _partition_historical_request(request, endpoint):
            payload = _historical_payload(
                reference,
                chunk,
                endpoint,
                expiry_code=_derivative_expiry_code(reference, self._references.values()),
            )
            response_payload = await self._request_api_json("POST", endpoint, payload)
            normalized = normalize_historical_payload(chunk, response_payload)
            candles.extend(normalized.candles)
        return HistoricalResult(request=request, candles=tuple(candles))

    async def load_session_statistics(
        self,
        instruments: Sequence[Instrument],
        *,
        trading_date: date,
        observed_at: datetime,
    ) -> tuple[SessionStatisticsObservation, ...]:
        """Load current-session OHLC via Market Quote and map to canonical observations.

        Batched into a single documented Market Quote request (the ~208-instrument universe
        fits well within the provider maximum). Instruments are deduplicated and canonically
        ordered; an instrument the provider universe cannot resolve fails closed. The result
        carries no provider identity and makes no authority claim (ADR-009 D6); a per-instrument
        missing/sentinel/invalid OHLC is withheld by normalization, not fabricated.
        """
        pairs = self._market_quote_pairs(instruments)
        if not pairs:
            return ()
        if self._live_client_id is None:
            raise ProviderAuthenticationError()
        response = await self._request_api_json(
            "POST",
            _MARKET_QUOTE_OHLC_ENDPOINT,
            _market_quote_payload(pairs),
            extra_headers={"client-id": self._live_client_id.get_secret_value()},
        )
        return normalize_session_statistics_payload(
            response, pairs, trading_date=trading_date, observed_at=observed_at
        )

    def _market_quote_pairs(
        self, instruments: Sequence[Instrument]
    ) -> tuple[tuple[Instrument, DhanInstrumentReference], ...]:
        unique = list(dict.fromkeys(instruments))  # dedup requested, preserve first occurrence
        if len(unique) > _MARKET_QUOTE_MAX_INSTRUMENTS:
            raise UnsupportedProviderRequestError()
        ordered = sorted(unique, key=lambda instrument: (instrument.exchange, instrument.symbol))
        pairs: list[tuple[Instrument, DhanInstrumentReference]] = []
        for instrument in ordered:
            reference = self._references.get(instrument)
            if reference is None or reference.exchange_segment is None:
                raise UnsupportedProviderRequestError()  # unmapped instrument — fail closed
            pairs.append((instrument, reference))
        return tuple(pairs)

    async def _request_api_json(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        client = self._require_api_client()
        token = await self._token_provider.get_access_token()
        await self._request_pacer.wait()
        headers = {"access-token": token.get_secret_value()}
        if extra_headers is not None:
            headers.update(extra_headers)
        try:
            response = await client.request(
                method,
                endpoint,
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError() from error
        except httpx.RequestError as error:
            raise ProviderNetworkError() from error

        self._raise_for_error(response, endpoint)
        try:
            response_payload: object = response.json()
        except (TypeError, ValueError) as error:
            raise NormalizationError() from error
        if not isinstance(response_payload, Mapping):
            raise NormalizationError()
        return response_payload

    def _raise_for_error(self, response: httpx.Response, endpoint: str) -> None:
        if response.is_success:
            return

        error_code, error_type, error_message = _error_details(response)
        if self._live_smoke_enabled and _undocumented_client_id_required(error_message):
            raise DhanRestContractDiscrepancyError(
                endpoint=endpoint,
                http_status=response.status_code,
                error_code=error_code,
                error_type=error_type,
                observed_requirement=_client_id_requirement(error_message),
            )
        if response.status_code == 429 or error_code in _RATE_LIMIT_ERROR_CODES:
            raise ProviderRateLimitError()
        if response.status_code in {401, 403} or error_code in _AUTHENTICATION_ERROR_CODES:
            raise ProviderAuthenticationError()
        if response.status_code == 408:
            raise ProviderTimeoutError()
        if response.status_code >= 500 or error_code in _TRANSIENT_ERROR_CODES:
            raise ProviderUnavailableError()
        if response.status_code in {400, 404} or error_code in _UNSUPPORTED_REQUEST_ERROR_CODES:
            raise UnsupportedProviderRequestError()
        raise ProviderBoundaryError("Provider request was rejected")

    def _require_api_client(self) -> httpx.AsyncClient:
        if self._api_client is None:
            raise ProviderContractViolationError()
        return self._api_client

    def _require_reference_client(self) -> httpx.AsyncClient:
        if self._reference_client is None:
            raise ProviderContractViolationError()
        return self._reference_client


def _event_matches_request(event: MarketData, request: SubscriptionRequest) -> bool:
    """Keep each canonical consumer stream within its own requested instruments and data kinds."""
    if event.instrument not in request.instruments:
        return False
    if isinstance(event, Candle):
        return MarketDataKind.CANDLE in request.data_types
    from app.schemas.market_data import DepthSnapshot, Quote, Tick

    if isinstance(event, Tick):
        return MarketDataKind.TICK in request.data_types
    if isinstance(event, Quote):
        return MarketDataKind.QUOTE in request.data_types
    if isinstance(event, DepthSnapshot):
        return MarketDataKind.DEPTH in request.data_types
    return False


def _market_quote_payload(
    pairs: Sequence[tuple[Instrument, DhanInstrumentReference]],
) -> dict[str, list[int]]:
    """Group canonical (instrument, reference) pairs into the documented per-segment batch."""
    grouped: dict[str, list[int]] = {}
    for _instrument, reference in pairs:
        segment = reference.exchange_segment
        if segment is None:
            raise UnsupportedProviderRequestError()
        try:
            security_id = int(reference.security_id)
        except ValueError as error:
            raise NormalizationError() from error
        grouped.setdefault(segment, []).append(security_id)
    return grouped


def _historical_endpoint(interval: timedelta) -> str:
    if interval == timedelta(days=1):
        return "/charts/historical"
    if (
        interval.total_seconds() % 60 == 0
        and int(interval.total_seconds() // 60) in _SUPPORTED_INTRADAY_INTERVALS
    ):
        return "/charts/intraday"
    raise UnsupportedProviderRequestError()


def _partition_historical_request(
    request: HistoricalRequest, endpoint: str
) -> tuple[HistoricalRequest, ...]:
    if endpoint == "/charts/historical":
        return (request,)

    chunks: list[HistoricalRequest] = []
    start = request.start_timestamp
    while start < request.end_timestamp:
        end = min(start + _INTRADAY_MAX_RANGE, request.end_timestamp)
        chunks.append(
            HistoricalRequest(
                instrument=request.instrument,
                start_timestamp=start,
                end_timestamp=end,
                interval=request.interval,
            )
        )
        start = end
    return tuple(chunks)


def _historical_payload(
    reference: DhanInstrumentReference,
    request: HistoricalRequest,
    endpoint: str,
    *,
    expiry_code: int | None = None,
) -> dict[str, object]:
    exchange_segment = reference.exchange_segment
    if exchange_segment is None:
        raise UnsupportedProviderRequestError()
    payload: dict[str, object] = {
        "securityId": reference.security_id,
        "exchangeSegment": exchange_segment,
        "instrument": reference.provider_instrument_type,
        "oi": False,
    }
    if endpoint == "/charts/historical":
        if expiry_code is not None:
            payload["expiryCode"] = expiry_code
        payload["fromDate"] = _market_datetime(request.start_timestamp).date().isoformat()
        payload["toDate"] = _market_datetime(request.end_timestamp).date().isoformat()
    else:
        payload["interval"] = str(int(request.interval.total_seconds() // 60))
        payload["fromDate"] = _market_datetime(request.start_timestamp).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        payload["toDate"] = _market_datetime(request.end_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return payload


def _derivative_expiry_code(
    reference: DhanInstrumentReference,
    references: Iterable[DhanInstrumentReference],
    *,
    current_date: date | None = None,
) -> int | None:
    """Return Dhan's documented near/next/far rank for one active derivative."""
    instrument = reference.instrument
    if instrument.underlying is None or instrument.expiry is None:
        return None

    as_of = current_date or date.today()
    expiries = sorted(
        {
            candidate.instrument.expiry
            for candidate in references
            if candidate.instrument.exchange == instrument.exchange
            and candidate.instrument.underlying == instrument.underlying
            and candidate.provider_instrument_type == reference.provider_instrument_type
            and candidate.instrument.expiry is not None
            and candidate.instrument.expiry >= as_of
        }
    )
    try:
        rank = expiries.index(instrument.expiry)
    except ValueError:
        return None
    return rank if rank in {0, 1, 2} else None


def _market_datetime(value: datetime) -> datetime:
    return value.astimezone(_INDIAN_MARKET_TIMEZONE)


def _error_details(response: httpx.Response) -> tuple[str | None, str | None, str]:
    try:
        payload: Any = response.json()
    except (TypeError, ValueError):
        return None, None, ""
    if not isinstance(payload, Mapping):
        return None, None, ""

    error_code = payload.get("errorCode")
    error_type = payload.get("errorType")
    error_message = payload.get("errorMessage")
    return (
        error_code.strip() if isinstance(error_code, str) else None,
        error_type.strip() if isinstance(error_type, str) else None,
        error_message.strip() if isinstance(error_message, str) else "",
    )


def _undocumented_client_id_required(error_message: str) -> bool:
    normalized = error_message.lower()
    has_client_identifier = "dhanclientid" in normalized or "client id" in normalized
    return has_client_identifier and ("required" in normalized or "mandatory" in normalized)


def _client_id_requirement(error_message: str) -> str:
    return "dhanClientId" if "dhanclientid" in error_message.lower() else "client ID"
