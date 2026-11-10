import random
from datetime import datetime, timezone

import pytest

from engine.catalog import CATALOG
from engine.market import Candle, CandleSeries, aggregate, synthetic_history, traded_volume

NOW = datetime(2026, 11, 10, 15, 0, 30, tzinfo=timezone.utc)


def test_candle_series_accumulates_ohlcv_per_bucket():
    series = CandleSeries(60)
    for second, price, vol in [(0, 10.0, 1), (20, 12.0, 2), (40, 9.0, 3), (61, 11.0, 4)]:
        series.add(datetime.fromtimestamp(1_800_000_000 + second, timezone.utc), price, vol)
    first, second = series.candles
    assert (first.open, first.high, first.low, first.close, first.volume) == (10.0, 12.0, 9.0, 9.0, 6)
    assert (second.open, second.close, second.volume) == (11.0, 11.0, 4)


def test_aggregate_builds_higher_timeframe():
    bars = [Candle(t, o, h, l, c, v) for t, o, h, l, c, v in [
        (0, 10, 11, 9, 10.5, 1), (60, 10.5, 13, 10, 12, 2), (120, 12, 12.5, 8, 9, 3), (300, 9, 9.5, 8.5, 9.2, 4)]]
    out = aggregate(bars, 300)
    assert [(c.time, c.open, c.high, c.low, c.close, c.volume) for c in out] == [
        (0, 10, 13, 8, 9, 6), (300, 9, 9.5, 8.5, 9.2, 4)]
    assert bars[0].high == 11  # não altera a série original


def test_synthetic_history_ends_at_current_price_before_now():
    model = CATALOG.models["PETR4"]
    hist = synthetic_history(model, NOW, 120, random.Random(1))
    assert len(hist) == 120
    assert hist[-1].close == pytest.approx(model.price)
    assert hist[-1].time + 60 <= NOW.timestamp()  # o candle ao vivo começa depois do histórico
    assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) and c.volume > 0 for c in hist)
    assert all(b.time - a.time == 60 for a, b in zip(hist, hist[1:]))


def test_traded_volume_grows_when_price_moves():
    assert traded_volume(1000, 0) < traded_volume(1000, 2) < traded_volume(1000, 50) == traded_volume(1000, 5)
