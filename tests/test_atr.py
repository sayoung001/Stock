"""ATR 계산 — 워밍업 처리와 Wilder 재귀를 검증한다."""

from __future__ import annotations

import numpy as np

from stock_auto.exit.atr import atr_wilder, true_range


def test_true_range_first_bar_uses_range():
    h = np.array([10.0, 12.0])
    l = np.array([9.0, 11.0])
    c = np.array([9.5, 11.5])
    tr = true_range(h, l, c)
    assert tr[0] == 1.0                       # 전일 종가 없음 → 고저폭
    # 2번째 봉: max(12-11, |12-9.5|, |11-9.5|) = 2.5
    assert tr[1] == 2.5


def test_atr_warmup_is_nan_not_zero():
    """워밍업 구간을 0으로 채우면 손절폭이 조용히 0이 된다 — NaN이어야 한다."""
    n = 20
    h = np.full(n, 101.0)
    l = np.full(n, 99.0)
    c = np.full(n, 100.0)
    a = atr_wilder(h, l, c, period=14)
    assert np.all(np.isnan(a[:13]))
    assert np.isfinite(a[13])


def test_atr_constant_range_equals_range():
    """고저폭이 일정하면 ATR도 그 값으로 수렴한다."""
    n = 40
    h = np.full(n, 102.0)
    l = np.full(n, 100.0)
    c = np.full(n, 101.0)
    a = atr_wilder(h, l, c, period=14)
    assert abs(a[-1] - 2.0) < 1e-9


def test_atr_shorter_than_period_all_nan():
    a = atr_wilder(np.ones(5), np.ones(5), np.ones(5), period=14)
    assert a.size == 5 and np.all(np.isnan(a))
