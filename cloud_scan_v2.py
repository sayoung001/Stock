"""
cloud_scan_v2.py — 고속 시그널 스캐너 (벡터화 + 멀티프로세싱)
═══════════════════════════════════════════════════════════════
핵심 최적화:
  1. 인디케이터 1회 계산 (코인당) — EMA,ATR,ADX,BB,RSI
  2. gap_pct 벡터 사전 계산 → GAP별 boolean 비교만
  3. Numba JIT: squeeze 런 감지 C속도화
  4. 멀티프로세싱: 코인별 병렬 (CPU 코어 수만큼)

속도: 50코인 × 19GAP ≈ 10~20분 (기존 27시간)

저장 구조:
  Results/cloud_scan_v2/{version}/signals_gap{X.X}.csv

사용법:
  # 전체 (기존 26코인, GAP 0.2~2.0, 0.1씩)
  python cloud_scan_v2.py

  # 특정 GAP 범위
  python cloud_scan_v2.py --gap-start 0.3 --gap-end 0.7 --gap-step 0.1

  # 코인 그룹
  python cloud_scan_v2.py --group core_26
  python cloud_scan_v2.py --group all
  python cloud_scan_v2.py --group extended

  # CPU 코어 수 지정
  python cloud_scan_v2.py --workers 8

  # BTC 시그널 포함
  python cloud_scan_v2.py --include-btc
"""

import pandas as pd, numpy as np, os, sys, time, argparse, json
from multiprocessing import Pool, cpu_count
from functools import partial

sys.path.insert(0, '.')

# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

DATA_DIR = "./Raw_Data/CRYPTO_BINANCE_15M"
OUTPUT_BASE = "./Results/cloud_scan_v2"

# 코인 그룹
CORE_26 = [
    "TRX", "AVAX", "BCH", "ATOM", "ADA", "NEAR", "SOL",
    "ETH", "XRP", "DOGE", "LTC", "LINK", "DOT", "FIL",
    "UNI", "COMP", "XLM", "SAND", "VET", "ALGO", "MANA",
    "1000PEPE", "OP", "APT", "ARB", "FTM",
]

# EMA 파라미터
EMA_SHORT = 12
EMA_LONG = 26

# 스캔 설정 (공격적 — 모든 시그널 포착)
SCAN_CFG = {
    'MIN_SQUEEZE_CANDLES': 8,
    'MAX_SQUEEZE_CANDLES': 200,
    'MIN_CONFIDENCE': 30,
    'MIN_ADX': 10,
    'BREAKOUT_ATR_MULT': 0.8,
    'BREAKOUT_VOLUME_MULT': 0.8,
    'BBW_SQUEEZE_RATIO': 0.7,
    'TP_FIBO_LEVEL': 2.618,
    'SL_RANGE_BUFFER': 0.3,
}


# ═══════════════════════════════════════════════════════════════
#  Numba JIT — squeeze 런 감지 (C속도)
# ═══════════════════════════════════════════════════════════════

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

def _squeeze_runs_python(gap_pct, high, low, close, threshold, min_sq, max_sq):
    """Python fallback — squeeze 런 감지"""
    n = len(gap_pct)
    sq_len = np.zeros(n, dtype=np.int32)
    sq_high = np.full(n, -np.inf)
    sq_low = np.full(n, np.inf)

    for i in range(1, n):
        if gap_pct[i] <= threshold:
            prev = sq_len[i-1]
            if prev < max_sq:
                sq_len[i] = prev + 1
                sq_high[i] = max(high[i], sq_high[i-1] if prev > 0 else high[i])
                sq_low[i] = min(low[i], sq_low[i-1] if prev > 0 else low[i])
            else:
                sq_len[i] = max_sq
                sq_high[i] = sq_high[i-1]
                sq_low[i] = sq_low[i-1]
        else:
            sq_len[i] = 0
            sq_high[i] = -np.inf
            sq_low[i] = np.inf

    return sq_len, sq_high, sq_low

if HAS_NUMBA:
    squeeze_runs = njit(cache=True)(_squeeze_runs_python)
    print("  Numba JIT: 활성화 ✅")
else:
    squeeze_runs = _squeeze_runs_python
    print("  Numba JIT: 미설치 (Python fallback)")


# ═══════════════════════════════════════════════════════════════
#  인디케이터 1회 계산
# ═══════════════════════════════════════════════════════════════

def compute_indicators(df):
    """전체 DataFrame에 인디케이터 계산 (1회)"""
    c = df['Close'].values.astype(np.float64)
    h = df['High'].values.astype(np.float64)
    lo = df['Low'].values.astype(np.float64)
    o = df['Open'].values.astype(np.float64)
    v = df['Volume'].values.astype(np.float64)

    cs = pd.Series(c)
    hs = pd.Series(h)
    ls = pd.Series(lo)

    # EMA
    ema_s = cs.ewm(span=EMA_SHORT).mean().values
    ema_l = cs.ewm(span=EMA_LONG).mean().values
    gap_pct = np.abs(ema_s - ema_l) / (c + 1e-9) * 100

    # ATR
    tr = np.maximum(h - lo, np.maximum(np.abs(h - np.roll(c, 1)),
                                         np.abs(lo - np.roll(c, 1))))
    tr[0] = h[0] - lo[0]
    atr = pd.Series(tr).ewm(alpha=1/14).mean().values
    atr_pct = atr / (c + 1e-9) * 100

    # ADX
    up = np.diff(h, prepend=h[0])
    down = -np.diff(lo, prepend=lo[0])
    p_dm = np.where((up > down) & (up > 0), up, 0)
    m_dm = np.where((down > up) & (down > 0), down, 0)
    tr_sm = pd.Series(tr).ewm(alpha=1/14).mean().values + 1e-9
    pdi = 100 * pd.Series(p_dm).ewm(alpha=1/14).mean().values / tr_sm
    mdi = 100 * pd.Series(m_dm).ewm(alpha=1/14).mean().values / tr_sm
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-9)
    adx = pd.Series(dx).ewm(alpha=1/14).mean().values

    # BB
    sma20 = cs.rolling(20).mean().values
    std20 = cs.rolling(20).std().values
    bb_upper = sma20 + std20 * 2
    bb_lower = sma20 - std20 * 2
    bbw = (bb_upper - bb_lower) / (sma20 + 1e-9)
    bbw_avg = pd.Series(bbw).rolling(20).mean().values

    # RSI
    delta = np.diff(c, prepend=c[0])
    gain = pd.Series(np.where(delta > 0, delta, 0)).rolling(14).mean().values
    loss = pd.Series(np.where(delta < 0, -delta, 0)).rolling(14).mean().values + 1e-9
    rsi = 100 - (100 / (1 + gain / loss))

    # Volume ratio
    vol_avg = pd.Series(v).rolling(20).mean().values + 1e-9
    rvol = v / vol_avg

    # MACD
    macd = pd.Series(c).ewm(span=12).mean().values - pd.Series(c).ewm(span=26).mean().values
    macd_signal = pd.Series(macd).ewm(span=9).mean().values

    return {
        'ema_s': ema_s, 'ema_l': ema_l, 'gap_pct': gap_pct,
        'atr': atr, 'atr_pct': atr_pct, 'adx': adx,
        'bb_upper': bb_upper, 'bb_lower': bb_lower, 'bbw': bbw, 'bbw_avg': bbw_avg,
        'rsi': rsi, 'rvol': rvol, 'pdi': pdi, 'mdi': mdi,
        'macd': macd, 'macd_signal': macd_signal,
        'close': c, 'high': h, 'low': lo, 'open': o, 'volume': v,
    }


# ═══════════════════════════════════════════════════════════════
#  벡터화 시그널 감지
# ═══════════════════════════════════════════════════════════════

def detect_signals_for_gap(ind, dates, gap, cfg=SCAN_CFG):
    """단일 GAP에서 모든 시그널 감지 (벡터화)"""
    n = len(ind['close'])
    min_sq = cfg['MIN_SQUEEZE_CANDLES']
    max_sq = cfg['MAX_SQUEEZE_CANDLES']
    fibo = cfg['TP_FIBO_LEVEL']

    # 1. squeeze 런 감지 (Numba JIT)
    sq_len, sq_high, sq_low = squeeze_runs(
        ind['gap_pct'], ind['high'], ind['low'], ind['close'],
        gap, min_sq, max_sq
    )

    # 2. 돌파 조건 (벡터화)
    c = ind['close']; h = ind['high']; lo = ind['low']; o = ind['open']
    atr = ind['atr']; rvol = ind['rvol']
    bb_up = ind['bb_upper']; bb_lo = ind['bb_lower']
    bbw = ind['bbw']; bbw_avg = ind['bbw_avg']
    adx = ind['adx']; rsi = ind['rsi']
    ema_s = ind['ema_s']; ema_l = ind['ema_l']
    macd = ind['macd']; macd_sig = ind['macd_signal']

    signals = []
    # squeeze가 끝나는 캔들(i-1까지 squeeze, i에서 돌파)
    for i in range(max(min_sq + 1, 60), n):
        # i-1 캔들까지 squeeze가 있어야 함
        if sq_len[i-1] < min_sq:
            continue

        # squeeze 중에는 gap이 작지만, 돌파 캔들(i)에서는 gap이 커져야 함
        # 또는 가격이 squeeze range 밖으로 나가야 함
        s_high = sq_high[i-1]
        s_low = sq_low[i-1]
        s_range = s_high - s_low
        if s_range <= 0:
            continue

        # 돌파 방향 판정
        direction = None
        if c[i] > s_high and c[i] > o[i]:   # 상방 돌파
            direction = 'long'
        elif c[i] < s_low and c[i] < o[i]:   # 하방 돌파
            direction = 'short'
        else:
            continue

        # BB 외부 확인
        if direction == 'long' and c[i] < bb_up[i]:
            continue
        if direction == 'short' and c[i] > bb_lo[i]:
            continue

        # 거래량 확인
        if rvol[i] < cfg['BREAKOUT_VOLUME_MULT']:
            continue

        # ATR 확인 (캔들 크기)
        candle_size = abs(c[i] - o[i])
        if candle_size < atr[i] * cfg['BREAKOUT_ATR_MULT'] * 0.5:
            continue

        # ── confidence 계산 ──
        conf = 50
        # BB squeeze 보강
        if bbw[i] < bbw_avg[i] * cfg['BBW_SQUEEZE_RATIO']:
            conf += 8
        # MACD 방향 일치
        if direction == 'long' and macd[i] > macd_sig[i]:
            conf += 5
        elif direction == 'short' and macd[i] < macd_sig[i]:
            conf += 5
        # ADX 강도
        if adx[i] > 25: conf += 5
        if adx[i] > 35: conf += 3
        # 돌파 캔들 body 비율
        body = abs(c[i] - o[i])
        wick_total = h[i] - lo[i]
        body_ratio = body / (wick_total + 1e-9)
        if body_ratio > 0.6: conf += 4
        # squeeze 캔들 수 보너스
        sq_candles = int(sq_len[i-1])
        if sq_candles > 20: conf += 3
        if sq_candles > 40: conf += 2
        # 볼륨 보너스
        if rvol[i] > 2.0: conf += 3
        if rvol[i] > 3.0: conf += 2

        if conf < cfg['MIN_CONFIDENCE']:
            continue

        # ── SL/TP 계산 ──
        entry_price = c[i]
        sl_buffer = atr[i] * cfg['SL_RANGE_BUFFER']
        if direction == 'long':
            sl = s_low - sl_buffer
            tp = entry_price + s_range * fibo
        else:
            sl = s_high + sl_buffer
            tp = entry_price - s_range * fibo

        # 피처
        gap_val = ind['gap_pct'][i]
        if direction == 'long':
            upper_wick = h[i] - max(c[i], o[i])
        else:
            upper_wick = min(c[i], o[i]) - lo[i]
        wick_ratio = upper_wick / (wick_total + 1e-9)

        # squeeze 중 볼륨 감소 확인
        if sq_candles > 4:
            sq_vol_start = ind['volume'][i - sq_candles: i - sq_candles + 3].mean()
            sq_vol_end = ind['volume'][i - 3: i].mean()
            vol_decrease = sq_vol_end < sq_vol_start * 0.8
        else:
            vol_decrease = False

        signals.append({
            'entry_time': dates[i] if i < len(dates) else '',
            'entry_price': round(entry_price, 8),
            'stop_loss': round(sl, 8),
            'take_profit': round(tp, 8),
            'direction': direction,
            'confidence': conf,
            'atr': round(atr[i], 8),
            'adx_value': round(adx[i], 1),
            'rsi': round(rsi[i], 1),
            'volume_ratio': round(rvol[i], 2),
            'gap_pct': round(gap_val, 4),
            'squeeze_candles': sq_candles,
            'squeeze_range': round(s_range, 8),
            'squeeze_high': round(s_high, 8),
            'squeeze_low': round(s_low, 8),
            'body_ratio': round(body_ratio, 3),
            'wick_ratio': round(wick_ratio, 3),
            'bbw': round(bbw[i], 4),
            'bbw_avg': round(bbw_avg[i], 4),
            'vol_decrease': vol_decrease,
            'candle_idx': i,
        })

    return signals


# ═══════════════════════════════════════════════════════════════
#  코인별 스캔 (멀티프로세싱 단위)
# ═══════════════════════════════════════════════════════════════

def scan_coin(args):
    """단일 코인 × 전체 GAP 스캔"""
    code, gaps, output_dir, min_days = args
    path = os.path.join(DATA_DIR, f"{code}_USDT.csv")
    if not os.path.exists(path):
        return code, {}, 'no_file'

    df = pd.read_csv(path)
    if len(df) < 100:
        return code, {}, 'too_short'
    df['Date'] = pd.to_datetime(df['Date'])

    # 최소 데이터 기간 필터
    data_days = (df['Date'].iloc[-1] - df['Date'].iloc[0]).days
    if data_days < min_days:
        print(f"  {code}: ⏭ 스킵 ({data_days}일 < {min_days}일 최소)")
        return code, {}, f'skip_{data_days}d'

    dates = df['Date'].values

    t0 = time.time()

    # 1. 인디케이터 1회 계산
    ind = compute_indicators(df)

    # 2. 각 GAP에서 시그널 감지
    results = {}
    for gap in gaps:
        sigs = detect_signals_for_gap(ind, dates, gap)
        for s in sigs:
            s['symbol'] = f"{code}_USDT"
        results[gap] = sigs

    elapsed = time.time() - t0
    total_sigs = sum(len(v) for v in results.values())
    print(f"  {code}: {total_sigs}건 ({len(gaps)}GAP, {data_days}일) ⏱{elapsed:.1f}s")
    return code, results, 'ok'


# ═══════════════════════════════════════════════════════════════
#  BTC 시그널 (Phase 2용)
# ═══════════════════════════════════════════════════════════════

def scan_btc(gaps, output_dir):
    """BTC 시그널 스캔 (별도 저장)"""
    path = os.path.join(DATA_DIR, "BTC_USDT.csv")
    if not os.path.exists(path):
        print("  ⚠️ BTC 데이터 없음")
        return

    df = pd.read_csv(path); df['Date'] = pd.to_datetime(df['Date'])
    ind = compute_indicators(df)
    dates = df['Date'].values

    all_sigs = []
    for gap in gaps:
        sigs = detect_signals_for_gap(ind, dates, gap)
        for s in sigs:
            s['symbol'] = 'BTC_USDT'
            s['scan_gap'] = gap
        all_sigs.extend(sigs)

    if all_sigs:
        btc_df = pd.DataFrame(all_sigs)
        btc_path = os.path.join(output_dir, 'signals_btc.csv')
        btc_df.to_csv(btc_path, index=False)
        print(f"  BTC: {len(btc_df)}건 → {btc_path}")


# ═══════════════════════════════════════════════════════════════
#  BTC7 계산 (기존과 동일)
# ═══════════════════════════════════════════════════════════════

def compute_btc7(output_dir):
    """BTC 7레벨 트렌드 계산 → 시그널에 병합"""
    path = os.path.join(DATA_DIR, "BTC_USDT.csv")
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path); df['Date'] = pd.to_datetime(df['Date'])
    c = df['Close'].values.astype(float)

    ema_s = pd.Series(c).ewm(span=EMA_SHORT).mean().values
    ema_l = pd.Series(c).ewm(span=EMA_LONG).mean().values

    # ADX
    h = df['High'].values.astype(float); lo = df['Low'].values.astype(float)
    tr = np.maximum(h-lo, np.maximum(np.abs(h-np.roll(c,1)), np.abs(lo-np.roll(c,1))))
    tr[0] = h[0]-lo[0]
    up = np.diff(h, prepend=h[0]); down = -np.diff(lo, prepend=lo[0])
    p_dm = np.where((up>down)&(up>0), up, 0)
    m_dm = np.where((down>up)&(down>0), down, 0)
    tr_sm = pd.Series(tr).ewm(alpha=1/14).mean().values+1e-9
    pdi = 100*pd.Series(p_dm).ewm(alpha=1/14).mean().values/tr_sm
    mdi = 100*pd.Series(m_dm).ewm(alpha=1/14).mean().values/tr_sm
    dx = 100*np.abs(pdi-mdi)/(pdi+mdi+1e-9)
    adx = pd.Series(dx).ewm(alpha=1/14).mean().values

    # RSI
    delta = np.diff(c, prepend=c[0])
    gain = pd.Series(np.where(delta>0,delta,0)).rolling(14).mean().values
    loss_v = pd.Series(np.where(delta<0,-delta,0)).rolling(14).mean().values+1e-9
    rsi = 100-(100/(1+gain/loss_v))

    # 1h 변동
    chg_1h = np.zeros(len(c))
    for i in range(4, len(c)):
        chg_1h[i] = (c[i]-c[i-4])/(c[i-4]+1e-9)*100

    # 7레벨
    btc7 = np.zeros(len(c), dtype=int)
    for i in range(1, len(c)):
        if ema_s[i] > ema_l[i]:
            if adx[i]>30 and rsi[i]>60 and chg_1h[i]>1.0: btc7[i]=3
            elif adx[i]>20: btc7[i]=2
            else: btc7[i]=1
        elif ema_s[i] < ema_l[i]:
            if adx[i]>30 and rsi[i]<40 and chg_1h[i]<-1.0: btc7[i]=-3
            elif adx[i]>20: btc7[i]=-2
            else: btc7[i]=-1

    dates = df['Date'].values
    return pd.DataFrame({'Date': dates, 'btc7': btc7}).set_index('Date')


# ═══════════════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════════════

def get_all_coins():
    """데이터 디렉토리에서 사용 가능한 전체 코인"""
    if not os.path.exists(DATA_DIR):
        return []
    files = [f.replace('_USDT.csv', '') for f in os.listdir(DATA_DIR)
             if f.endswith('_USDT.csv') and not f.startswith('BTC')]
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description='Cloud Scan V2 — 고속 시그널 스캐너')
    parser.add_argument('--gap-start', type=float, default=0.2)
    parser.add_argument('--gap-end', type=float, default=2.0)
    parser.add_argument('--gap-step', type=float, default=0.1)
    parser.add_argument('--group', default='core_26', choices=['core_26', 'all', 'extended', 'custom'])
    parser.add_argument('--coins', nargs='+', help='custom 그룹용 코인 목록')
    parser.add_argument('--workers', type=int, default=0, help='CPU 코어 수 (0=자동)')
    parser.add_argument('--include-btc', action='store_true', help='BTC 시그널도 스캔')
    parser.add_argument('--version', default='default', help='저장 버전명')
    parser.add_argument('--min-days', type=int, default=180, help='최소 데이터 일수 (기본 180=6개월)')
    args = parser.parse_args()

    t0 = time.time()

    # GAP 목록
    gaps = []
    g = args.gap_start
    while g <= args.gap_end + 0.001:
        gaps.append(round(g, 1))
        g += args.gap_step
    print(f"  GAP: {gaps[0]}~{gaps[-1]} ({len(gaps)}개, step {args.gap_step})")

    # 코인 목록
    all_available = get_all_coins()
    if args.group == 'core_26':
        coins = [c for c in CORE_26 if c in all_available]
    elif args.group == 'all':
        coins = all_available
    elif args.group == 'extended':
        coins = [c for c in all_available if c not in CORE_26]
    elif args.group == 'custom' and args.coins:
        coins = [c for c in args.coins if c in all_available]
    else:
        coins = [c for c in CORE_26 if c in all_available]

    # ── 사전 분류: 데이터 기간별 티어 ──
    # T1(≥2년): 충분한 백테스트 가능
    # T2(1~2년): 참고용 (과적합 주의)
    # T3(<1년): 스킵 또는 극소 참고
    TIER_LONG = 730    # 2년
    TIER_MID = 365     # 1년

    coin_tiers = {'T1': [], 'T2': [], 'T3': [], 'skip': []}
    coin_days = {}

    print(f"\n  코인 데이터 기간 분류 중...")
    for code in coins:
        path = os.path.join(DATA_DIR, f"{code}_USDT.csv")
        if not os.path.exists(path):
            coin_tiers['skip'].append(code)
            continue
        try:
            df = pd.read_csv(path, usecols=['Date'], nrows=1)
            df_last = pd.read_csv(path, usecols=['Date'])
            first = pd.to_datetime(df_last['Date'].iloc[0])
            last = pd.to_datetime(df_last['Date'].iloc[-1])
            days = (last - first).days
            coin_days[code] = days

            if days >= TIER_LONG:
                coin_tiers['T1'].append(code)
            elif days >= TIER_MID:
                coin_tiers['T2'].append(code)
            elif days >= args.min_days:
                coin_tiers['T3'].append(code)
            else:
                coin_tiers['skip'].append(code)
        except:
            coin_tiers['skip'].append(code)

    print(f"  ┌─────────────────────────────────────────────")
    print(f"  │ T1 (≥2년, 신뢰): {len(coin_tiers['T1'])}개")
    for c in sorted(coin_tiers['T1']):
        print(f"  │   {c:>12} ({coin_days.get(c,0)}일)")
    print(f"  │ T2 (1~2년, 참고): {len(coin_tiers['T2'])}개")
    for c in sorted(coin_tiers['T2']):
        print(f"  │   {c:>12} ({coin_days.get(c,0)}일)")
    print(f"  │ T3 (<1년, 과적합주의): {len(coin_tiers['T3'])}개")
    for c in sorted(coin_tiers['T3']):
        print(f"  │   {c:>12} ({coin_days.get(c,0)}일)")
    print(f"  │ 스킵 (<{args.min_days}일): {len(coin_tiers['skip'])}개")
    if coin_tiers['skip']:
        print(f"  │   {', '.join(sorted(coin_tiers['skip']))}")
    print(f"  └─────────────────────────────────────────────")

    # 스캔 대상 = T1 + T2 + T3 (skip 제외)
    scan_coins = coin_tiers['T1'] + coin_tiers['T2'] + coin_tiers['T3']
    print(f"\n  스캔 대상: {len(scan_coins)}개 (스킵 {len(coin_tiers['skip'])}개)")
    print(f"  총 스캔: {len(scan_coins)} × {len(gaps)} = {len(scan_coins)*len(gaps)}회")

    # 출력 디렉토리
    version = args.version if args.version != 'default' else args.group
    output_dir = os.path.join(OUTPUT_BASE, version)
    os.makedirs(output_dir, exist_ok=True)

    # 티어 정보 저장
    tier_info = {code: ('T1' if code in coin_tiers['T1']
                        else 'T2' if code in coin_tiers['T2']
                        else 'T3') for code in scan_coins}
    tier_path = os.path.join(output_dir, 'coin_tiers.json')
    with open(tier_path, 'w') as f:
        json.dump({
            'T1': sorted(coin_tiers['T1']),
            'T2': sorted(coin_tiers['T2']),
            'T3': sorted(coin_tiers['T3']),
            'skip': sorted(coin_tiers['skip']),
            'days': {k: v for k, v in sorted(coin_days.items())},
        }, f, indent=2)
    print(f"  티어 정보 → {tier_path}")

    # BTC7 사전 계산
    print(f"\n  BTC7 계산 중...")
    btc7_df = compute_btc7(output_dir)
    if btc7_df is not None:
        print(f"  BTC7: {len(btc7_df)}봉")

    # BTC 시그널
    if args.include_btc:
        print(f"\n  BTC 시그널 스캔...")
        scan_btc(gaps, output_dir)

    # 멀티프로세싱
    workers = args.workers if args.workers > 0 else min(cpu_count(), len(scan_coins))
    print(f"\n  워커: {workers}개")
    print(f"\n{'='*70}")
    print(f"  스캔 시작")
    print(f"{'='*70}")

    scan_args = [(code, gaps, output_dir, args.min_days) for code in scan_coins]

    if workers <= 1:
        results = [scan_coin(a) for a in scan_args]
    else:
        # Windows + notebook(%run) 환경에서 multiprocessing spawn 오류 방지
        import __main__
        if not hasattr(__main__, '__spec__'):
            __main__.__spec__ = None
        with Pool(workers) as pool:
            results = pool.map(scan_coin, scan_args)

    # ── 결과 병합 — 티어 태깅 + GAP별 저장 ──
    print(f"\n{'='*70}")
    print(f"  저장")
    print(f"{'='*70}")

    def add_btc7(sdf):
        if btc7_df is not None and len(sdf) > 0:
            sdf['entry_time'] = pd.to_datetime(sdf['entry_time'])
            sdf['btc7'] = sdf['entry_time'].apply(
                lambda t: btc7_df.iloc[btc7_df.index.searchsorted(t)-1]['btc7']
                if btc7_df.index.searchsorted(t) > 0 else 0)
        else:
            sdf['btc7'] = 0
        return sdf

    def add_tier(sdf):
        sdf['data_tier'] = sdf['symbol'].apply(
            lambda s: tier_info.get(s.replace('_USDT', ''), 'T3'))
        sdf['data_days'] = sdf['symbol'].apply(
            lambda s: coin_days.get(s.replace('_USDT', ''), 0))
        return sdf

    # GAP별 저장 (전체)
    for gap in gaps:
        gap_sigs = []
        for code, coin_results, status in results:
            if status != 'ok': continue
            if gap in coin_results:
                gap_sigs.extend(coin_results[gap])
        if not gap_sigs: continue

        sdf = pd.DataFrame(gap_sigs).sort_values('entry_time').reset_index(drop=True)
        sdf = add_btc7(sdf)
        sdf = add_tier(sdf)

        fname = f"signals_gap{gap:.1f}.csv"
        sdf.to_csv(os.path.join(output_dir, fname), index=False)

        # 티어별 건수
        t1n = len(sdf[sdf['data_tier']=='T1'])
        t2n = len(sdf[sdf['data_tier']=='T2'])
        t3n = len(sdf[sdf['data_tier']=='T3'])
        print(f"  GAP {gap:.1f}: {len(sdf):>5}건 (T1:{t1n} T2:{t2n} T3:{t3n})")

    # 전체 통합
    all_sigs = []
    for code, coin_results, status in results:
        if status != 'ok': continue
        for gap, sigs in coin_results.items():
            for s in sigs:
                s['scan_gap'] = gap
            all_sigs.extend(sigs)

    if all_sigs:
        adf = pd.DataFrame(all_sigs).sort_values('entry_time').reset_index(drop=True)
        adf = add_btc7(adf)
        adf = add_tier(adf)

        # 전체 통합
        adf.to_csv(os.path.join(output_dir, 'signals_all.csv'), index=False)
        print(f"\n  통합: {len(adf):>6}건 → signals_all.csv")

        # 티어별 분리 저장
        for tier in ['T1', 'T2', 'T3']:
            tdf = adf[adf['data_tier'] == tier]
            if len(tdf) == 0: continue
            tier_dir = os.path.join(output_dir, f'tier_{tier}')
            os.makedirs(tier_dir, exist_ok=True)
            tdf.to_csv(os.path.join(tier_dir, 'signals_all.csv'), index=False)
            # 티어별 GAP 분리도
            for gap in gaps:
                gdf = tdf[tdf['scan_gap'] == gap]
                if len(gdf) == 0: continue
                gdf.to_csv(os.path.join(tier_dir, f'signals_gap{gap:.1f}.csv'), index=False)
            coins_in = tdf['symbol'].nunique()
            print(f"  {tier}: {len(tdf):>6}건 ({coins_in}코인) → tier_{tier}/")

    # ── 요약 ──
    elapsed = time.time() - t0
    scanned = sum(1 for _, _, s in results if s == 'ok')
    skipped = sum(1 for _, _, s in results if s != 'ok')
    print(f"\n{'='*70}")
    print(f"  완료")
    print(f"  스캔: {scanned}코인 × {len(gaps)}GAP | 스킵: {skipped}코인")
    print(f"  시그널: {len(all_sigs):,}건")
    print(f"  T1(≥2년): {len(coin_tiers['T1'])}코인 — 백테스트 신뢰")
    print(f"  T2(1~2년): {len(coin_tiers['T2'])}코인 — 참고용")
    print(f"  T3(<1년): {len(coin_tiers['T3'])}코인 — 과적합 주의")
    print(f"  저장: {output_dir}/")
    print(f"  ⏱ {elapsed/60:.1f}분")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
