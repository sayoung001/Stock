"""지표 계산 — `현재_적용_로직_전수.md` §2.3에 열거된 소비 지표들.

**전부 인과적(causal)으로 계산한다.** i번째 값은 0..i 봉만 쓴다. 미래 봉이
한 칸이라도 섞이면 백테스트가 통째로 거짓이 되고, 그게 과거 candle_idx
오프바이원 사고였다.

일목균형표의 선행스팬(senkou)만 예외적으로 주의가 필요하다. 원래 정의는
26봉 **앞으로** 밀어 그리는 것인데, 백테스트에서 "지금 구름"을 보려면
26봉 **전에** 계산된 값을 현재 봉에 놓아야 한다. 즉 shift(+26)이고,
이건 과거 참조이므로 누출이 아니다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stock_auto.exit.atr import atr_wilder

#: 지표가 다 익는 데 필요한 최소 봉 수. MA60 + 일목 52 + 선행 26을 고려.
WARMUP_BARS = 140

#: 유동성 sigmoid 중심값 (일평균 거래대금). 시장별로 통화가 다르다 —
#: KR에 US 중심값을 쓰면 전 종목이 포화된다(US_KR 문서 §3.1의 그 버그).
LIQUIDITY_CENTER = {"US": 50_000_000.0, "KR": 30_000_000_000.0}


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder 평활 (= alpha 1/period EMA). ADX/RSI가 쓴다."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0 * (avg_gain > 0))


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    atr_s = _wilder(tr, period)
    plus_di = 100.0 * _wilder(pd.Series(plus_dm, index=high.index), period) / atr_s
    minus_di = 100.0 * _wilder(pd.Series(minus_dm, index=high.index), period) / atr_s
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return _wilder(dx, period)


def _mfi(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    tp = (high + low + close) / 3.0
    flow = tp * volume
    up = flow.where(tp > tp.shift(1), 0.0)
    down = flow.where(tp < tp.shift(1), 0.0)
    pos = up.rolling(period, min_periods=period).sum()
    neg = down.rolling(period, min_periods=period).sum()
    ratio = pos / neg.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + ratio)).fillna(100.0 * (pos > 0))


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def _liquidity_score(trading_value_ma5: pd.Series, market: str = "US") -> pd.Series:
    """거래대금 → 0.3~1.4 연속 점수.

    계단식 컷오프(Cliff Effect)를 피하려고 sigmoid를 쓴다. 반감폭은
    반 decade(약 3.2배)로 잡았다 — 문서에 폭이 없어 세운 가정이다
    (`docs/MODEL_REPRODUCTION.md` A3).
    """
    center = LIQUIDITY_CENTER.get(market, LIQUIDITY_CENTER["US"])
    tv = trading_value_ma5.clip(lower=1.0)
    z = (np.log10(tv) - np.log10(center)) / 0.5
    return 0.3 + 1.1 / (1.0 + np.exp(-z))


def calculate_indicators(df: pd.DataFrame, market: str = "US") -> pd.DataFrame:
    """OHLCV DataFrame에 지표 컬럼을 붙여 돌려준다.

    Args:
        df: 컬럼 ``open/high/low/close/volume``, 인덱스는 날짜 오름차순.
        market: 유동성 통화 정규화용. **반드시 전달해야 한다** — 기본값으로
            두면 KR 종목이 US 기준으로 채점되는 그 버그가 재발한다.
    """
    out = df.copy()
    o, h, l, c, v = (out[k] for k in ("open", "high", "low", "close", "volume"))

    # --- 이동평균 ---------------------------------------------------------
    for p in (5, 10, 20, 60):
        out[f"MA{p}"] = c.rolling(p, min_periods=p).mean()
    out["EMA20"] = c.ewm(span=20, adjust=False, min_periods=20).mean()
    out["EMA60"] = c.ewm(span=60, adjust=False, min_periods=60).mean()

    # --- 추세·모멘텀 ------------------------------------------------------
    out["adx"] = _adx(h, l, c, 14)
    out["rsi_14"] = _rsi(c, 14)
    out["atr"] = atr_wilder(h.to_numpy(), l.to_numpy(), c.to_numpy(), 14)

    macd_fast = c.ewm(span=12, adjust=False, min_periods=12).mean()
    macd_slow = c.ewm(span=26, adjust=False, min_periods=26).mean()
    out["macd_line"] = macd_fast - macd_slow
    out["macd_signal"] = out["macd_line"].ewm(span=9, adjust=False, min_periods=9).mean()
    out["macdh"] = out["macd_line"] - out["macd_signal"]

    # --- 거래량 ----------------------------------------------------------
    out["vol_ma20"] = v.rolling(20, min_periods=20).mean()
    out["rvol"] = v / out["vol_ma20"]
    out["trading_value"] = c * v
    tv_ma5 = out["trading_value"].rolling(5, min_periods=5).mean()
    out["liquidity_score"] = _liquidity_score(tv_ma5, market)

    # VAI 2단 — 거래량 가속(단기)과 지속(중기)을 분리해 본다.
    # 분모를 shift(1)으로 잡아 "어제까지의 기준선"과 비교한다(누출 차단).
    vol_ma5 = v.rolling(5, min_periods=5).mean()
    vol_ma60 = v.rolling(60, min_periods=60).mean()
    out["vai_stage1"] = vol_ma5 / out["vol_ma20"].shift(1)
    out["vai_stage2"] = out["vol_ma20"] / vol_ma60.shift(1)
    # 컷 1.5/1.2 (지속) · 2.0/1.1 (단발) — 문서 §7.1이 언급한 네 숫자.
    out["vai_sustained"] = (out["vai_stage1"] >= 1.5) & (out["vai_stage2"] >= 1.2)
    out["vai_spike_only"] = (out["vai_stage1"] >= 2.0) & (out["vai_stage2"] < 1.1)

    out["obv"] = _obv(c, v)
    out["obv_ma20"] = out["obv"].rolling(20, min_periods=20).mean()
    out["mfi_14"] = _mfi(h, l, c, v, 14)

    # --- 볼린저 / 스퀴즈 ---------------------------------------------------
    bb_mid = c.rolling(20, min_periods=20).mean()
    bb_sd = c.rolling(20, min_periods=20).std(ddof=0)
    out["boll_ub"] = bb_mid + 2.0 * bb_sd
    out["boll_lb"] = bb_mid - 2.0 * bb_sd
    width = (out["boll_ub"] - out["boll_lb"]).replace(0.0, np.nan)
    out["bb_pctb"] = (c - out["boll_lb"]) / width
    out["bb_width"] = width / bb_mid

    # 켈트너 채널 — 볼린저가 켈트너 안으로 들어가면 스퀴즈.
    kc_mid = out["EMA20"]
    kc_range = 1.5 * out["atr"]
    out["squeeze_on"] = (out["boll_ub"] < kc_mid + kc_range) & (
        out["boll_lb"] > kc_mid - kc_range
    )

    # --- 일목균형표 -------------------------------------------------------
    out["ichimoku_tenkan"] = (
        h.rolling(9, min_periods=9).max() + l.rolling(9, min_periods=9).min()
    ) / 2.0
    out["ichimoku_kijun"] = (
        h.rolling(26, min_periods=26).max() + l.rolling(26, min_periods=26).min()
    ) / 2.0
    # 선행스팬은 26봉 **전에** 산출된 값이 지금의 구름이다 → shift(+26).
    # 과거를 당겨오는 것이므로 누출이 아니다.
    out["senkou_a"] = (
        (out["ichimoku_tenkan"] + out["ichimoku_kijun"]) / 2.0
    ).shift(26)
    out["senkou_b"] = (
        (h.rolling(52, min_periods=52).max() + l.rolling(52, min_periods=52).min()) / 2.0
    ).shift(26)
    out["cloud_top"] = out[["senkou_a", "senkou_b"]].max(axis=1)

    # --- 캔들 / 페널티 재료 -------------------------------------------------
    rng = (h - l).replace(0.0, np.nan)
    body_top = pd.concat([o, c], axis=1).max(axis=1)
    out["upper_wick_ratio"] = (h - body_top) / rng
    out["high_close_drop"] = (h - c) / c * 100.0        # 고가→종가 낙폭 %
    out["is_bull"] = c > o                              # 양봉
    out["gap_pct"] = (o / c.shift(1) - 1.0) * 100.0
    out["high20"] = h.rolling(20, min_periods=20).max()
    # 20일 신고가 돌파 — **전일까지의** 20일 고가와 비교해야 한다.
    # 당일 고가가 포함된 high20과 비교하면 항상 참이 되는 자기참조가 된다.
    out["break_high20"] = c > out["high20"].shift(1)

    # 하락 경고 3종 (Warning_Count의 재료)
    out["MA20_Break"] = (c < out["MA20"]) & (c.shift(1) >= out["MA20"].shift(1))
    prev_bear = (o.shift(1) > c.shift(1))
    out["Candle_BearEngulfing"] = (
        (o > c) & (o >= c.shift(1)) & (c <= o.shift(1)) & ~prev_bear
    )
    # 약세 다이버전스: 가격은 20일 신고가인데 RSI는 그 고점을 못 넘김.
    price_hi = c >= c.rolling(20, min_periods=20).max()
    rsi_lower = out["rsi_14"] < out["rsi_14"].rolling(20, min_periods=20).max()
    out["Bearish_Div"] = price_hi & rsi_lower

    out["Warning_Count"] = (
        out["MA20_Break"].astype(int)
        + out["Bearish_Div"].astype(int)
        + out["Candle_BearEngulfing"].astype(int)
    )

    # --- 페널티 (문서 §2.6) -------------------------------------------------
    out["Penalty"] = (
        0.5 * (out["upper_wick_ratio"] > 0.6).astype(float)
        + 0.5 * (out["high_close_drop"] > 1.5).astype(float)
        + 1.0 * out["vai_spike_only"].astype(float)
        + 0.5 * (out["liquidity_score"] < 0.5).astype(float)
    ).clip(upper=3.0)

    return out
