"""ATR(14) — 변동성 기준 손절폭의 재료.

고정 3% 손절의 문제는 반도체처럼 일변동 5%가 예사인 종목과 유틸리티를
같은 자로 재는 것이다(analysis_2026-09-18 §3-2). ATR은 그 자를 종목마다
바꿔 준다.

**누출 주의**: 진입은 시그널 다음 거래일 시가다. 따라서 ATR은 반드시
**시그널 봉(= 진입 전일)까지**의 데이터로만 계산해야 한다. 진입일 봉이
섞여 들어가면 그날의 고저를 미리 아는 셈이 된다. 과거 candle_idx 오프바이원
사고와 같은 종류의 실수다.
"""

from __future__ import annotations

import numpy as np

DEFAULT_PERIOD = 14


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """봉별 True Range. 첫 봉은 전일 종가가 없어 고저폭으로 둔다."""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)

    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]

    hl = high - low
    hc = np.abs(high - prev_close)
    lc = np.abs(low - prev_close)
    tr = np.maximum(hl, np.maximum(hc, lc))
    tr[0] = high[0] - low[0]
    return tr


def atr_wilder(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = DEFAULT_PERIOD,
) -> np.ndarray:
    """Wilder 평활 ATR.

    ``atr[i]``는 **i번째 봉까지 포함한** 값이다. 진입 전일 기준 ATR을 쓰려면
    시그널 봉의 인덱스로 조회하면 된다(진입은 그 다음 봉이므로 누출 없음).

    워밍업이 덜 된 구간(``i < period``)은 NaN을 돌려준다. 0이나 부분평균으로
    채우면 초기 시그널의 손절폭이 조용히 터무니없어진다.
    """
    if period < 1:
        raise ValueError(f"period는 1 이상이어야 한다: {period}")

    tr = true_range(high, low, close)
    n = tr.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out

    # 시드: 첫 period개 TR의 단순평균. 이후 Wilder 재귀.
    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out
