"""Regression tests for distinct canonical underlying and tradable identities."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.base.errors import NormalizationError
from app.adapters.dhan.normalizer import (
    derive_equity_fno_universe,
    normalize_instrument_master,
)
from app.schemas.market_data import (
    Instrument,
    InstrumentClass,
    MarketSegment,
    OptionType,
    UnderlyingInstrument,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"


def _identity_master() -> str:
    return (_FIXTURES / "instrument_master_identity_collisions.csv").read_text(encoding="utf-8")


def test_canonical_contract_keeps_derivative_variants_distinct_without_provider_ids() -> None:
    """Dropping expiry, strike, or side would collapse distinct tradable contracts."""
    underlying = UnderlyingInstrument(exchange="NSE", symbol="APEXCO")
    future_august = Instrument(
        exchange="NSE",
        market_segment=MarketSegment.DERIVATIVES,
        symbol="APEXCOFUT",
        instrument_class=InstrumentClass.FUTURE,
        underlying=underlying,
        expiry=date(2026, 8, 27),
    )
    future_september = future_august.model_copy(update={"expiry": date(2026, 9, 24)})
    option_call = Instrument(
        exchange="NSE",
        market_segment=MarketSegment.DERIVATIVES,
        symbol="APEXCOOPT",
        instrument_class=InstrumentClass.OPTION,
        underlying=underlying,
        expiry=date(2026, 8, 27),
        strike_price=Decimal("100"),
        option_type=OptionType.CALL,
    )
    option_put = option_call.model_copy(update={"option_type": OptionType.PUT})
    index_one = Instrument(
        exchange="BSE",
        market_segment=MarketSegment.INDEX,
        symbol="CAPINS",
        instrument_class=InstrumentClass.OTHER,
        listing_type="INDEX",
        display_name="Capital Markets & Insurance",
    )
    index_two = index_one.model_copy(update={"display_name": "Capital Goods Industrial Solutions"})

    assert future_august != future_september
    assert option_call != option_put
    assert index_one != index_two
    assert "security_id" not in Instrument.model_fields
    assert "dhan" not in repr(option_call).lower()


@pytest.mark.parametrize(
    ("instrument_class", "kwargs"),
    (
        (InstrumentClass.FUTURE, {}),
        (InstrumentClass.OPTION, {"expiry": date(2026, 8, 27)}),
        (
            InstrumentClass.OPTION,
            {"expiry": date(2026, 8, 27), "strike_price": Decimal("100")},
        ),
    ),
)
def test_derivative_contract_validation_requires_its_identity_fields(
    instrument_class: InstrumentClass, kwargs: dict[str, object]
) -> None:
    """Allowing incomplete derivatives would recreate ambiguous canonical identities."""
    with pytest.raises(ValidationError):
        Instrument(
            exchange="NSE",
            market_segment=MarketSegment.DERIVATIVES,
            symbol="APEX",
            instrument_class=instrument_class,
            underlying=UnderlyingInstrument(exchange="NSE", symbol="APEXCO"),
            **kwargs,
        )


def test_normalizer_preserves_live_collision_classes_and_groups_fno_by_underlying() -> None:
    """Collapsing same-symbol expiry, strike, side, or listing variants is incorrect."""
    references = normalize_instrument_master(_identity_master())

    assert len(references) == 12
    assert len({reference.instrument for reference in references}) == 12

    futures = [
        reference.instrument
        for reference in references
        if reference.instrument.instrument_class is InstrumentClass.FUTURE
        and reference.instrument.underlying == UnderlyingInstrument(exchange="NSE", symbol="APEXCO")
    ]
    options = [
        reference.instrument
        for reference in references
        if reference.instrument.instrument_class is InstrumentClass.OPTION
        and reference.instrument.underlying == UnderlyingInstrument(exchange="NSE", symbol="APEXCO")
    ]
    listings = [
        reference.instrument
        for reference in references
        if reference.instrument.symbol in {"APEXLISTA", "APEXLISTB"}
    ]
    universe = derive_equity_fno_universe(references)

    assert {future.expiry for future in futures} == {date(2026, 8, 27), date(2026, 9, 24)}
    assert {(option.strike_price, option.option_type) for option in options} == {
        (Decimal("100"), OptionType.CALL),
        (Decimal("110"), OptionType.CALL),
        (Decimal("100"), OptionType.PUT),
    }
    assert {listing.series for listing in listings} == {"A", "B"}
    assert {listing.symbol for listing in listings} == {"APEXLISTA", "APEXLISTB"}
    assert {
        reference.instrument.display_name
        for reference in references
        if reference.instrument.symbol == "CAPINS"
    } == {"CAPITAL MARKETS & INSURANCE", "CAPITAL GOODS INDUSTRIAL SOLUTIONS"}
    assert universe.underlyings == (UnderlyingInstrument(exchange="NSE", symbol="APEXCO"),)
    assert len(universe.futures) == 2
    assert len(universe.options) == 3


def test_normalizer_rejects_a_true_duplicate_provider_row() -> None:
    """Silently accepting an identical repeated provider row would hide master corruption."""
    master = _identity_master()
    repeated_first_row = master.splitlines()[1]

    with pytest.raises(NormalizationError):
        normalize_instrument_master(master + repeated_first_row + "\n")


def test_normalizer_keeps_a_contract_when_its_provider_route_is_undocumented() -> None:
    """Rejecting a valid master row solely for a missing Dhan route loses canonical data."""
    header = _identity_master().splitlines()[0]
    commodity_option = (
        "NSE,M,701,OPTFUT,501,GOLD,GOLD,GOLD OPTION,OPTFUT,1,1,2027-01-29,168500,CE\n"
    )

    reference = normalize_instrument_master(header + "\n" + commodity_option)[0]

    assert reference.instrument.market_segment is MarketSegment.COMMODITY
    assert reference.instrument.instrument_class is InstrumentClass.OPTION
    assert reference.exchange_segment is None
