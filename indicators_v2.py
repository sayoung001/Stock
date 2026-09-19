"""
indicators_v2.py
================
개선된 기술적 지표 모듈
- 변동성 필터 (ATR%, BBW) 추가
- 스테이블코인/죽은코인 블랙리스트
- 멀티타임프레임(MTF) 합의 시스템
- 기존 indicators.py 호환 유지
"""

import pandas as pd
import numpy as np

# =====================================================
# 🚫 블랙리스트 (레버리지 매매 부적합 코인)
# =====================================================
STABLECOIN_BLACKLIST = {
    'USDC_USDT', 'TUSD_USDT', 'FDUSD_USDT', 'BUSD_USDT', 'USDP_USDT',
    'DAI_USDT', 'FRAX_USDT', 'EURI_USDT', 'XUSD_USDT', 'PYUSD_USDT',
    'USDD_USDT', 'GUSD_USDT', 'SUSD_USDT', 'UST_USDT', 'CUSD_USDT',
    'BTTC_USDT', 'WLFI_USDT',  # 극단적 저유동성 코인도 추가
    'SUN_USDT', 'KGST_USDT',
}

def is_blacklisted(code: str) -> bool:
    """블랙리스트 코인 여부 확인"""
    code_upper = code.upper().replace("/", "_")
    if code_upper in STABLECOIN_BLACKLIST:
        return True
    # 이름에 USD/EUR 등 통화명이 들어간 코인 자동 감지
    base = code_upper.split("_")[0]
    fiat_keywords = ['USD', 'EUR', 'GBP', 'JPY', 'KRW']
    for kw in fiat_keywords:
        if base.startswith(kw) or base.endswith(kw):
            return True
    return False


# =====================================================
# 📊 컬럼 표준화 (기존 호환)
# =====================================================
def check_and_fix_columns(df):
    """컬럼명 표준화 (대소문자 호환성)"""
    df = df.copy()
    col_map = {c.lower(): c for c in df.columns}
    required = {
        'close': 'Close', 'open': 'Open',
        'high': 'High', 'low': 'Low', 'volume': 'Volume'
    }
    rename_dict = {}
    for req_lower, req_std in required.items():
        if req_lower in col_map:
            rename_dict[col_map[req_lower]] = req_std
    df = df.rename(columns=rename_dict)
    missing = [c for c in required.values() if c not in df.columns]
    if missing:
        return None
    return df


# =====================================================
# 📈 지표 계산 (기존 + 변동성 필터 추가)
# =====================================================
def calculate_indicators(df):
    """
    보조지표 계산 (기존 호환 + 변동성 필터 추가)
    추가된 컬럼:
      - atr_pct: ATR을 가격 대비 퍼센트로 표현
      - bbw: 볼린저밴드 너비 (변동성 측정)
      - bbw_avg: BBW 20기간 평균
      - volatility_rank: 변동성 순위 ('LOW', 'MEDIUM', 'HIGH')
    """
    # [1] 날짜 인덱스 처리
    found_date = False
    if isinstance(df.index, pd.DatetimeIndex):
        found_date = True

    if not found_date:
        date_candidates = ['Date', 'date', 'DATE', '일자', 'time', 'Time']
        for col in date_candidates:
            if col in df.columns:
                try:
                    df[col] = pd.to_datetime(df[col])
                    df = df.set_index(col)
                    found_date = True
                    break
                except:
                    continue

    if not found_date:
        try:
            first_col = df.columns[0]
            temp_date = pd.to_datetime(df[first_col], errors='coerce')
            if temp_date.notna().sum() > len(df) * 0.8:
                df[first_col] = temp_date
                df = df.set_index(first_col)
                found_date = True
        except:
            pass

    if not found_date and df.index.dtype == 'object':
        try:
            temp_index = pd.to_datetime(df.index, errors='coerce')
            if temp_index.notna().sum() > len(df) * 0.8:
                df.index = temp_index
                found_date = True
        except:
            pass

    if not found_date:
        return None

    df.index.name = 'Date'
    df = df.sort_index()

    # [2] 데이터 전처리
    df = check_and_fix_columns(df)
    if df is None or len(df) < 60:
        return None

    for col in ['Close', 'Open', 'High', 'Low', 'Volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # --- [기본 지표] (기존과 동일) ---
    df['close_20_ema'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['close_60_ema'] = df['Close'].ewm(span=60, adjust=False).mean()

    k = df['Close'].ewm(span=12, adjust=False).mean()
    d = df['Close'].ewm(span=26, adjust=False).mean()
    df['macd_line'] = k - d
    df['macd_signal'] = df['macd_line'].ewm(span=9, adjust=False).mean()
    df['macdh'] = df['macd_line'] - df['macd_signal']

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi_14'] = 100 - (100 / (1 + rs))

    sma20 = df['Close'].rolling(window=20).mean()
    std20 = df['Close'].rolling(window=20).std()
    df['boll'] = sma20
    df['boll_ub'] = sma20 + std20 * 2
    df['boll_lb'] = sma20 - std20 * 2

    df['roc_12'] = df['Close'].pct_change(periods=12, fill_method=None) * 100

    # ADX
    tr1 = df['High'] - df['Low']
    tr2 = abs(df['High'] - df['Close'].shift(1))
    tr3 = abs(df['Low'] - df['Close'].shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    up = df['High'] - df['High'].shift(1)
    down = df['Low'].shift(1) - df['Low']
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_s = tr.ewm(alpha=1/14, adjust=False).mean()
    p_dm_s = pd.Series(plus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean()
    m_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean()
    df['pdi'] = 100 * (p_dm_s / (tr_s + 1e-9))
    df['mdi'] = 100 * (m_dm_s / (tr_s + 1e-9))
    df['adx'] = (100 * abs(df['pdi'] - df['mdi']) / (df['pdi'] + df['mdi'] + 1e-9)).ewm(alpha=1/14, adjust=False).mean()
    df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()

    # --- [거래량 지표] (기존과 동일) ---
    df['vol_ma20'] = df['Volume'].rolling(window=20).mean()
    df['rvol'] = df['Volume'] / (df['vol_ma20'] + 1e-9)

    direction = np.where(df['Close'] > df['Close'].shift(1), 1,
                         np.where(df['Close'] < df['Close'].shift(1), -1, 0))
    df['obv'] = (direction * df['Volume']).cumsum()
    df['obv_ma20'] = df['obv'].rolling(window=20).mean()

    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    raw_money_flow = typical_price * df['Volume']
    tp_shift = typical_price.shift(1)
    pos_flow = np.where(typical_price > tp_shift, raw_money_flow, 0)
    neg_flow = np.where(typical_price < tp_shift, raw_money_flow, 0)
    pos_mf = pd.Series(pos_flow, index=df.index).rolling(window=14).sum()
    neg_mf = pd.Series(neg_flow, index=df.index).rolling(window=14).sum()
    mfi_ratio = pos_mf / (neg_mf + 1e-9)
    df['mfi_14'] = 100 - (100 / (1 + mfi_ratio))

    # === [🆕 변동성 필터 지표] ===
    
    # 1. ATR% (가격 대비 평균 변동폭 퍼센트)
    df['atr_pct'] = (df['atr'] / df['Close']) * 100
    
    # 2. 볼린저밴드 너비 (BBW)
    df['bbw'] = (df['boll_ub'] - df['boll_lb']) / (df['boll'] + 1e-9)
    df['bbw_avg'] = df['bbw'].rolling(window=20).mean()
    
    # 3. 변동성 등급
    #    - LOW: ATR% < 1.5% (20배 레버리지 안전권)
    #    - MEDIUM: 1.5% ~ 3.0%
    #    - HIGH: > 3.0% (20배에서 위험)
    df['volatility_rank'] = pd.cut(
        df['atr_pct'],
        bins=[0, 1.5, 3.0, float('inf')],
        labels=['LOW', 'MEDIUM', 'HIGH'],
        include_lowest=True
    )
    
    # 4. 추세 강도 (정배열/역배열 + ADX 조합)
    df['trend_alignment'] = np.where(
        (df['close_20_ema'] > df['close_60_ema']) & (df['adx'] > 20), 'BULL',
        np.where(
            (df['close_20_ema'] < df['close_60_ema']) & (df['adx'] > 20), 'BEAR',
            'NEUTRAL'
        )
    )
    
    # 5. 가격-MA20 괴리율 (%)
    df['dist_ma20_pct'] = (df['Close'] - df['close_20_ema']) / (df['close_20_ema'] + 1e-9) * 100

    # 6. VWAP (거래량 가중 평균가) — 최근 세션 기준
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    cum_vol = df['Volume'].cumsum()
    cum_vp = (typical_price * df['Volume']).cumsum()
    df['vwap'] = cum_vp / (cum_vol + 1e-9)
    df['vwap_dist_pct'] = (df['Close'] - df['vwap']) / (df['vwap'] + 1e-9) * 100

    df = df.dropna(subset=['rsi_14', 'macdh', 'atr'])

    return df


# =====================================================
# 🕯️ 캔들 패턴 분석 (기존과 동일 + 개선)
# =====================================================
def analyze_candlestick(df):
    """캔들 패턴 및 이평선 지지/저항 정밀 분석"""
    df['Body'] = abs(df['Close'] - df['Open'])
    df['Upper_Shadow'] = df['High'] - df[['Open', 'Close']].max(axis=1)
    df['Lower_Shadow'] = df[['Open', 'Close']].min(axis=1) - df['Low']
    df['Avg_Body'] = df['Body'].rolling(20).mean()

    prev_close = df['Close'].shift(1)
    prev_open = df['Open'].shift(1)

    df['Candle_Engulfing'] = (
        (prev_close < prev_open) &
        (df['Close'] > df['Open']) &
        (df['Close'] > prev_open) & (df['Open'] < prev_close)
    )

    df['Candle_Hammer'] = (
        (df['Lower_Shadow'] > df['Body'] * 2) &
        (df['Upper_Shadow'] < df['Body'] * 0.5)
    )

    ma20_slope = df['close_20_ema'].diff()
    df['MA20_Bounce'] = (
        (df['Low'] <= df['close_20_ema']) &
        (df['Close'] > df['close_20_ema']) &
        (ma20_slope > 0)
    )

    df['MA20_Break'] = (
        (df['Close'] < df['close_20_ema']) &
        (prev_close > df['close_20_ema'].shift(1))
    )

    # 하락 패턴
    df['Bearish_Engulfing'] = (
        (prev_close > prev_open) &
        (df['Close'] < df['Open']) &
        (df['Open'] >= prev_close) &
        (df['Close'] <= prev_open)
    )

    df['Shooting_Star'] = (
        (df['Upper_Shadow'] >= df['Body'] * 2) &
        (df['Lower_Shadow'] <= df['Body'] * 0.5)
    )

    # 하락 다이버전스
    local_high_price = df['High'].rolling(20).max()
    local_high_rsi = df['rsi_14'].rolling(20).max()
    df['Bearish_Div'] = (
        (df['High'] >= local_high_price) &
        (df['rsi_14'] < local_high_rsi) &
        (df['rsi_14'] < 70)
    )

    return df


# =====================================================
# 🎯 전략 신호 (기존 호환 + Warning 개선)
# =====================================================
def apply_strategies(df):
    """전략 신호 생성"""
    if df is None or len(df) == 0:
        return None

    df = analyze_candlestick(df)

    df['Trend_Up'] = (
        (df['close_20_ema'] > df['close_60_ema']) &
        (df['adx'] > 20) &
        (df['obv'] > df['obv_ma20'])
    )
    df['Momentum'] = (df['macdh'] > df['macdh'].shift(1)) & (df['roc_12'] > 0)
    df['Oversold'] = (df['rsi_14'] < 30) & (df['mfi_14'] < 30)

    bb_width = (df['boll_ub'] - df['boll_lb']) / (df['boll'] + 1e-9)
    df['Vol_Explode'] = (bb_width > bb_width.rolling(20).mean() * 1.5) & (df['Close'] > df['boll_ub'])

    df['Vol_Pump'] = (
        (df['rvol'] > 2.0) &
        (df['Close'] > df['Open']) &
        (df['Close'] > df['Close'].shift(1))
    )

    df['Candle_Buy'] = (
        (df['Close'] > df['close_60_ema']) &
        (df['Candle_Engulfing'] | df['Candle_Hammer'] | df['MA20_Bounce'])
    )

    strategies = ['Trend_Up', 'Momentum', 'Oversold', 'Vol_Explode', 'Vol_Pump', 'Candle_Buy']
    df['Signal_Count'] = df[strategies].sum(axis=1)
    df['Buy_Signal'] = (df['Signal_Count'] >= 2) | (df['Candle_Buy'] & df['Trend_Up'])

    # === [🆕 패턴명 생성] ===
    def _pattern_name(row):
        patterns = []
        if row.get('Candle_Engulfing', False): patterns.append("상승장악형")
        if row.get('Candle_Hammer', False): patterns.append("망치형")
        if row.get('MA20_Bounce', False): patterns.append("20선지지")
        if row.get('Bearish_Engulfing', False): patterns.append("하락장악형")
        if row.get('Shooting_Star', False): patterns.append("유성형")
        if row.get('MA20_Break', False): patterns.append("20선붕괴")
        if row.get('Bearish_Div', False): patterns.append("하락다이버전스")
        return ", ".join(patterns) if patterns else "-"

    df['Buy_Pattern'] = df.apply(_pattern_name, axis=1)

    # === [🆕 개선된 Warning] ===
    def _warning(row):
        warnings = []
        if row.get('rsi_14', 50) > 75: warnings.append("RSI과열")
        if row.get('rsi_14', 50) < 25: warnings.append("RSI과매도")
        if row.get('Bearish_Div', False): warnings.append("하락다이버전스")
        if row.get('MA20_Break', False): warnings.append("20선붕괴")
        if row.get('atr_pct', 0) > 3.0: warnings.append("고변동성주의")
        return ", ".join(warnings) if warnings else "-"

    df['Warning'] = df.apply(_warning, axis=1)

    return df


# =====================================================
# 🏗️ 매물대 분석 (기존과 동일)
# =====================================================

# =====================================================
# 📊 매물대 분석 (Volume Profile) — v1에서 이식 + 양방향 확장
# =====================================================
def analyze_volume_profile(df, period=80, bins=30, direction=None):
    """
    매물대(Volume Profile) 분석 — 양방향 지지/저항 판단

    반환값:
        POC_Price: 가장 거래가 많은 가격대 (Point of Control)
        In_POC_Zone: 현재가가 POC 구간에 있는지
        Vol_Density: 현재 가격대의 매물 밀도
        Resistance_Ratio: 위쪽 매물 / 아래쪽 매물
        Dist_to_POC: 현재가와 POC 간 거리 (%)
        direction_score: 방향별 매물대 점수 (-5 ~ +5)
    """
    if df is None or len(df) < 20:
        return None

    subset = df.iloc[-period:].copy()
    price_min = subset['Low'].min()
    price_max = subset['High'].max()
    if price_min == price_max:
        return None

    intervals = np.linspace(price_min, price_max, bins + 1)
    subset_c = subset.copy()
    subset_c['Price_Bin'] = pd.cut(subset_c['Close'], bins=intervals,
                                    labels=False, include_lowest=True)
    vol_profile = subset_c.groupby('Price_Bin')['Volume'].sum()

    current_price = df.iloc[-1]['Close']
    current_bin_idx = np.searchsorted(intervals, current_price) - 1
    current_bin_idx = max(0, min(current_bin_idx, bins - 1))

    if vol_profile.empty:
        return None

    # POC (Point of Control) — 가장 거래 많은 가격대
    max_vol_bin = vol_profile.idxmax()
    poc_price = (intervals[int(max_vol_bin)] + intervals[int(max_vol_bin) + 1]) / 2

    # 매물 밀도 (현재 구간 거래량 / 평균)
    avg_vol = vol_profile.mean()
    curr_density = vol_profile.get(current_bin_idx, 0) / (avg_vol + 1e-9)

    # 위쪽 저항 vs 아래쪽 지지
    upper_bins = range(current_bin_idx + 1, min(current_bin_idx + 6, bins))
    upper_vol = vol_profile.loc[vol_profile.index.isin(upper_bins)].sum()
    lower_bins = range(max(0, current_bin_idx - 5), current_bin_idx)
    lower_vol = vol_profile.loc[vol_profile.index.isin(lower_bins)].sum()

    resistance_ratio = upper_vol / (lower_vol + 1e-9)
    dist_to_poc = (current_price - poc_price) / (poc_price + 1e-9)

    # 방향별 점수
    dir_score = 0
    if direction == 'long':
        if resistance_ratio < 0.5: dir_score += 3    # 위쪽 진공 (상승 여지)
        elif resistance_ratio > 2.0: dir_score -= 3  # 위쪽 매물벽
        if curr_density > 1.5: dir_score += 2        # POC 근처 유동성
    elif direction == 'short':
        if resistance_ratio > 2.0: dir_score += 3    # 위쪽 매물벽 (하락 촉매)
        elif resistance_ratio < 0.5: dir_score -= 3  # 아래쪽 진공
        if curr_density > 1.5: dir_score += 2        # POC 근처 유동성

    return {
        'POC_Price': poc_price,
        'In_POC_Zone': int(max_vol_bin) == current_bin_idx,
        'Vol_Density': round(curr_density, 2),
        'Resistance_Ratio': round(resistance_ratio, 2),
        'Dist_to_POC': round(dist_to_poc, 4),
        'direction_score': dir_score,
    }


def check_immediate_hurdle(df, lookback=40):
    """초단기 매물대 분석"""
    if df is None or len(df) < lookback:
        return None

    subset = df.iloc[-lookback:].copy()
    current_close = subset.iloc[-1]['Close']
    target_upper = current_close * 1.05
    stop_lower = current_close * 0.97

    mask_resistance = (subset['Close'] > current_close) & (subset['Close'] <= target_upper)
    resistance_vol = subset.loc[mask_resistance, 'Volume'].sum()
    mask_support = (subset['Close'] >= stop_lower) & (subset['Close'] <= current_close)
    support_vol = subset.loc[mask_support, 'Volume'].sum()
    avg_vol = subset['Volume'].mean() * lookback

    resistance_intensity = resistance_vol / (avg_vol + 1e-9)
    ratio = support_vol / resistance_vol if resistance_vol > 0 else 999.0

    return {
        'Hurdle_Score': resistance_intensity,
        'SR_Ratio': ratio
    }


# =====================================================
# 🆕 레버리지 적합성 필터
# =====================================================
def filter_for_leverage(df, code: str, leverage: int = 20) -> dict:
    """
    20배 레버리지에 적합한 코인인지 판정
    
    Returns:
        dict with keys:
        - 'suitable': bool (적합 여부)
        - 'reason': str (부적합 사유)
        - 'atr_pct': float
        - 'volatility': str ('LOW'/'MEDIUM'/'HIGH')
        - 'max_safe_leverage': int (권장 최대 레버리지)
    """
    if df is None or len(df) < 20:
        return {'suitable': False, 'reason': '데이터 부족'}
    
    if is_blacklisted(code):
        return {'suitable': False, 'reason': f'블랙리스트: {code}'}
    
    last = df.iloc[-1]
    
    atr_pct = last.get('atr_pct', 999)
    adx = last.get('adx', 0)
    rvol = last.get('rvol', 0)
    vol_rank = str(last.get('volatility_rank', 'HIGH'))
    
    # 안전 레버리지 계산 (ATR% 기반)
    # 20배에서 5% 움직이면 원금 전부 → ATR%가 2.5% 이하여야 안전
    if atr_pct > 0:
        max_safe_lev = min(int(50 / atr_pct), 50)  # 최대 50배 제한
    else:
        max_safe_lev = 1
    
    reasons = []
    suitable = True
    
    # 1. 변동성 체크
    if atr_pct > 3.0:
        suitable = False
        reasons.append(f"ATR% 과다({atr_pct:.1f}%)")
    
    # 2. 추세 존재 체크 (ADX)
    if adx < 15:
        suitable = False
        reasons.append(f"추세 없음(ADX={adx:.0f})")
    
    # 3. 유동성 체크
    if rvol < 0.3:
        suitable = False
        reasons.append(f"유동성 부족(RVOL={rvol:.1f})")
    
    return {
        'suitable': suitable,
        'reason': ', '.join(reasons) if reasons else '적합',
        'atr_pct': round(atr_pct, 2),
        'volatility': vol_rank,
        'max_safe_leverage': max_safe_lev,
        'adx': round(adx, 1)
    }


# =====================================================
# 🆕 멀티타임프레임(MTF) 합의 시스템
# =====================================================
def calculate_mtf_confluence(tf_results: dict) -> dict:
    """
    여러 시간대의 분석 결과를 종합하여 합의 판정
    
    :param tf_results: {'4h': {...}, '1h': {...}, '15m': {...}, '5m': {...}}
        각 value는 다음 키를 포함:
        - 'trend': 'BULL' | 'BEAR' | 'NEUTRAL'
        - 'rsi': float
        - 'macdh_rising': bool
    
    Returns:
        dict with:
        - 'confluence': 'FULL_BEAR' | 'PARTIAL_BEAR' | 'CONFLICT' | 'PARTIAL_BULL' | 'FULL_BULL'
        - 'score': int (-4 ~ +4)
        - 'advice': str
        - 'should_alarm': bool (알람 보낼지 여부)
    """
    if not tf_results:
        return {'confluence': 'UNKNOWN', 'score': 0, 'advice': '데이터 없음', 'should_alarm': False}
    
    # 가중치: 상위 TF일수록 높음
    weights = {'1d': 4, '4h': 3, '1h': 2, '15m': 1, '5m': 0.5}
    
    score = 0
    bull_count = 0
    bear_count = 0
    
    for tf, data in tf_results.items():
        w = weights.get(tf, 1)
        trend = data.get('trend', 'NEUTRAL')
        
        if trend == 'BULL':
            score += w
            bull_count += 1
        elif trend == 'BEAR':
            score -= w
            bear_count += 1
    
    total_tf = len(tf_results)
    
    # 합의 판정
    if bear_count == total_tf:
        confluence = 'FULL_BEAR'
        advice = "✅ 전 시간대 하락 합의. 숏 홀딩/추가 안전."
        should_alarm = True
    elif bear_count >= total_tf * 0.7:
        confluence = 'PARTIAL_BEAR'
        advice = "📉 대부분 하락. 단기 노이즈 무시하고 숏 홀딩."
        should_alarm = False  # 정기 브리핑에서만
    elif bull_count == total_tf:
        confluence = 'FULL_BULL'
        advice = "🚨 전 시간대 상승 합의! 숏 포지션 즉시 점검!"
        should_alarm = True
    elif bull_count >= total_tf * 0.7:
        confluence = 'PARTIAL_BULL'
        advice = "⚠️ 대부분 상승 전환. 숏 물량 축소 고려."
        should_alarm = True
    else:
        confluence = 'CONFLICT'
        # 상위TF vs 하위TF 충돌 체크
        upper_tfs = {tf: d for tf, d in tf_results.items() if tf in ('4h', '1h', '1d')}
        lower_tfs = {tf: d for tf, d in tf_results.items() if tf in ('15m', '5m')}
        
        upper_bear = all(d.get('trend') == 'BEAR' for d in upper_tfs.values()) if upper_tfs else False
        lower_bull = any(d.get('trend') == 'BULL' for d in lower_tfs.values()) if lower_tfs else False
        
        if upper_bear and lower_bull:
            advice = "💤 상위TF 하락 + 하위TF 반등 = 가짜반등(데드캣). 알람 무시 OK."
            should_alarm = False  # ← 핵심! 이 경우 알람을 보내지 않음
        else:
            advice = "🦀 혼조세. 매매 자제 또는 포지션 축소 권장."
            should_alarm = False
    
    return {
        'confluence': confluence,
        'score': round(score, 1),
        'advice': advice,
        'should_alarm': should_alarm,
        'bull_count': bull_count,
        'bear_count': bear_count,
        'total_tf': total_tf
    }


# =====================================================
# 🆕 Short 점수 v2 (온체인 + 기술적 통합)
# =====================================================
def calculate_short_score_v2(df, coinglass_data: dict = None):
    """
    개선된 Short 점수 계산
    - 기술적 분석 (캔들, 이평선, RSI 등) : 최대 7점
    - 온체인 데이터 (CoinGlass) : 최대 8점
    - 총합 최대 15점
    
    :param df: calculate_indicators()를 거친 데이터프레임
    :param coinglass_data: CoinGlassClient.get_full_analysis() 결과 (optional)
    """
    if df is None or len(df) < 3:
        return 0, "-", {}
    
    df = analyze_candlestick(df) if 'Bearish_Engulfing' not in df.columns else df
    
    row = df.iloc[-1]
    prev = df.iloc[-2]
    
    score = 0
    patterns = []
    details = {}
    
    # === 기술적 분석 점수 (최대 7점) ===
    
    # 1. 역배열 (1점)
    if row.get('close_20_ema', 0) < row.get('close_60_ema', 0):
        score += 1
        details['reverse_align'] = True
    
    # 2. 고점/저항권 (1점)
    ma20 = row.get('close_20_ema', 1)
    dist_to_ma20 = (row['Close'] - ma20) / ma20
    if (row['rsi_14'] > 50) or (dist_to_ma20 > -0.005):
        score += 1
    
    # 3. 거래량 (1점)
    if row['rvol'] > 1.2:
        score += 1
        details['high_volume'] = True
    
    # 4. MACD 하락 (1점)
    if row['macdh'] < prev['macdh']:
        score += 1
    
    # 5. 하락 캔들패턴 (각 1점)
    if row.get('Bearish_Engulfing', False):
        score += 1
        patterns.append("하락장악형")
    if row.get('Shooting_Star', False):
        score += 1
        patterns.append("유성형")
    if row.get('MA20_Break', False):
        score += 1
        patterns.append("20선붕괴")
    
    # === 온체인 점수 (CoinGlass, 최대 8점) ===
    if coinglass_data:
        cg_score = coinglass_data.get('short_conviction', 0)
        # -8 ~ +8 범위를 0 ~ 8로 정규화
        cg_normalized = max(0, cg_score)  # 음수는 0으로
        score += cg_normalized
        
        cg_reasons = coinglass_data.get('short_reasons', [])
        if cg_reasons:
            patterns.extend(cg_reasons)
        
        details['coinglass_score'] = cg_score
        details['coinglass_verdict'] = coinglass_data.get('verdict', '-')
    
    # === VWAP 위치 점수 (1점) ===
    vwap = row.get('vwap', 0)
    if vwap > 0:
        vwap_dist = (row['Close'] - vwap) / vwap * 100
        if vwap_dist > 0.5:  # 가격이 VWAP 위 → 숏 불리
            pass
        elif vwap_dist < -0.3:  # 가격이 VWAP 아래 → 숏 유리
            score += 1
            patterns.append("VWAP하회(숏유리)")

    # === 매물대 분석 점수 (최대 2점) ===
    vp = analyze_volume_profile(df, direction='short')
    if vp:
        vp_sc = vp.get('direction_score', 0)
        if vp_sc >= 3:
            score += 2; patterns.append(f"매물대숏유리(R:{vp['Resistance_Ratio']:.1f})")
        elif vp_sc >= 1:
            score += 1; patterns.append("매물대중립+")
        details['volume_profile'] = vp

    pattern_str = ", ".join(patterns) if patterns else "-"
    details['total_score'] = score
    details['tech_patterns'] = [p for p in patterns if p in ['하락장악형', '유성형', '20선붕괴']]
    
    return score, pattern_str, details
# =====================================================
# 🚀 Long 점수 v2 (온체인 + 기술적 통합)
# =====================================================
def calculate_long_score_v2(df, coinglass_data: dict = None):
    """
    Long(매수) 포지션을 위한 점수 계산
    - 기술적 분석 (상승장악형, 망치형, 정배열 등) : 최대 7점
    - 온체인 데이터 (CoinGlass 숏 확신의 반대) : 최대 8점
    """
    if df is None or len(df) < 3:
        return 0, "-", {}
    
    df = analyze_candlestick(df) if 'Candle_Engulfing' not in df.columns else df
    
    row = df.iloc[-1]
    prev = df.iloc[-2]
    
    score = 0
    patterns = []
    details = {}
    
    # 1. 정배열 (1점) - 상승 추세
    if row.get('close_20_ema', 0) > row.get('close_60_ema', 0):
        score += 1
        details['regular_align'] = True
    
    # 2. 눌림목/지지권 (1점) - 20일선 근접 또는 RSI 안정권
    ma20 = row.get('close_20_ema', 1)
    dist_to_ma20 = (row['Close'] - ma20) / ma20
    if (row['rsi_14'] < 50) or (abs(dist_to_ma20) < 0.005):
        score += 1
    
    # 3. 거래량 (1점)
    if row['rvol'] > 1.2:
        score += 1
        details['high_volume'] = True
    
    # 4. MACD 상승 (1점)
    if row['macdh'] > prev['macdh']:
        score += 1
    
    # 5. 상승 캔들패턴 (각 1점)
    if row.get('Candle_Engulfing', False):
        score += 1
        patterns.append("상승장악형")
    if row.get('Candle_Hammer', False):
        score += 1
        patterns.append("망치형")
    if row.get('MA20_Bounce', False):
        score += 1
        patterns.append("20선지지(눌림목)")
    
    # === 온체인 점수 (CoinGlass) ===
    # 숏 확신도가 마이너스일수록 롱에 유리함
    if coinglass_data:
        cg_score = coinglass_data.get('short_conviction', 0)
        cg_long_conviction = -cg_score # 부호 반전
        cg_normalized = max(0, cg_long_conviction) 
        score += cg_normalized
        
    # === VWAP 위치 점수 (1점) ===
    vwap = row.get('vwap', 0)
    if vwap > 0:
        vwap_dist = (row['Close'] - vwap) / vwap * 100
        if vwap_dist < -0.5:  # VWAP 아래 = 롱 불리 (할인 매수)
            pass
        elif vwap_dist > 0.3:  # VWAP 위 = 롱 유리 (상승 추세 확인)
            score += 1
            patterns.append("VWAP상회(롱유리)")

    # === 매물대 분석 점수 (최대 2점) ===
    vp = analyze_volume_profile(df, direction='long')
    if vp:
        vp_sc = vp.get('direction_score', 0)
        if vp_sc >= 3:
            score += 2; patterns.append(f"매물대롱유리(R:{vp['Resistance_Ratio']:.1f})")
        elif vp_sc >= 1:
            score += 1; patterns.append("매물대중립+")
        details['volume_profile'] = vp

    pattern_str = ", ".join(patterns) if patterns else "-"
    details['total_score'] = score
    
    return score, pattern_str, details