"""테스트 공용 픽스처 — 손으로 계산 가능한 작은 일봉을 만든다."""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.exit.engine import PriceSeries, Signal


def make_series(
    symbol: str = "T",
    opens=None,
    highs=None,
    lows=None,
    closes=None,
    start: str = "2026-01-05",
) -> PriceSeries:
    """영업일 기준 연속 일봉. 값은 호출자가 정확히 지정한다."""
    n = len(closes)
    import pandas as pd

    dates = pd.bdate_range(start=start, periods=n).to_numpy(dtype="datetime64[D]")
    return PriceSeries(
        symbol=symbol,
        dates=dates,
        open=np.asarray(opens, dtype=float),
        high=np.asarray(highs, dtype=float),
        low=np.asarray(lows, dtype=float),
        close=np.asarray(closes, dtype=float),
    )


@pytest.fixture
def flat_series() -> PriceSeries:
    """변동이 거의 없는 일봉 — 배리어가 절대 안 닿는 기준선."""
    n = 30
    return make_series(
        opens=[100.0] * n, highs=[100.5] * n, lows=[99.5] * n, closes=[100.0] * n
    )


@pytest.fixture
def sig_factory():
    def _make(px: PriceSeries, idx: int = 0, label: str = "단타", **kw) -> Signal:
        return Signal(symbol=px.symbol, signal_date=px.dates[idx], label=label, **kw)

    return _make
