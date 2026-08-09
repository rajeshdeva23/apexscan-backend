"""Tests for the immutable, broker-neutral Timeframe value object."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.market_engine.timeframe import Timeframe, TimeframeKind


def test_minutes_builds_a_positive_intraday_timeframe() -> None:
    timeframe = Timeframe.minutes(5)
    assert timeframe.kind is TimeframeKind.INTRADAY
    assert timeframe.duration == timedelta(minutes=5)
    assert timeframe.is_session is False


def test_session_builds_a_session_timeframe_without_duration() -> None:
    timeframe = Timeframe.session()
    assert timeframe.kind is TimeframeKind.SESSION
    assert timeframe.duration is None
    assert timeframe.is_session is True


@pytest.mark.parametrize("count", [0, -1, -5])
def test_non_positive_minutes_fail_fast(count: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        Timeframe.minutes(count)


def test_intraday_without_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        Timeframe(kind=TimeframeKind.INTRADAY, duration=None)


def test_session_with_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not carry a duration"):
        Timeframe(kind=TimeframeKind.SESSION, duration=timedelta(minutes=5))


def test_timeframe_is_immutable_and_hashable() -> None:
    timeframe = Timeframe.minutes(5)
    with pytest.raises(AttributeError):
        timeframe.duration = timedelta(minutes=1)  # type: ignore[misc]
    assert {Timeframe.minutes(5), Timeframe.minutes(5)} == {Timeframe.minutes(5)}
    assert hash(Timeframe.minutes(5)) == hash(Timeframe.minutes(5))


def test_equal_timeframes_deduplicate() -> None:
    assert len({Timeframe.minutes(5), Timeframe.minutes(5), Timeframe.minutes(15)}) == 2
