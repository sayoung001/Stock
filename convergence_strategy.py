"""
convergence_strategy.py — EMA 수렴 돌파 + 헷지 전략
═══════════════════════════════════════════════════════

전략 1: ConvergenceBreakout (EMA 수렴 → 돌파)
  - EMA20과 EMA60이 수렴(gap < 임계값)하면 "스퀴즈" 상태
  - 스퀴즈 상태에서 가격이 돌파하면 돌파 방향으로 진입
  - TP: 피보나치 확장(1.618x) 또는 ATR 배수
  - SL: 수렴 구간 반대쪽

전략 2: HedgeBreakout (헷지 돌파)
  - 스퀴즈 감지 시 롱+숏 동시 진입
  - 한쪽이 SL(타이트) → 반대쪽이 TP(넓게) 로 비대칭 관리
  - 돌파 방향이 맞으면 수익, 반대쪽 손절은 작게

사용법:
  # 단독 테스트
  python convergence_strategy.py --coins ADA NEAR --months 6

  # dual_strategy와 통합 (scan에서 추가)
  from convergence_strategy import ConvergenceBreakout, HedgeBreakout
"""

import pandas as pd, numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


# ═══════════════════════════════════════════════════════════════
#  ⚙️ CONFIG
# ═══════════════════════════════════════════════════════════════
CONVERGENCE_CONFIG = {
    # ── EMA 설정 ──
    "EMA_SHORT": 12,              # 단기 EMA (기존 20 → 12)
    "EMA_LONG": 26,               # 장기 EMA (기존 60 → 26)

    # ── 스퀴즈 감지 ──
    "EMA_GAP_THRESHOLD": 0.2,     # EMA gap이 이 %이하면 스퀴즈
    "MIN_SQUEEZE_CANDLES": 30,     # 최소 N캔들 동안 스퀴즈 유지
    "MAX_SQUEEZE_CANDLES": 40,    # 너무 오래된 스퀴즈는 무시
    "BBW_SQUEEZE_RATIO": 0.7,     # BBW가 평균의 N배 이하면 스퀴즈 보강

    # ── 돌파 감지 ──
    "BREAKOUT_ATR_MULT": 1.5,     # 현재봉이 ATR × N 이상 움직이면 돌파
    "BREAKOUT_VOLUME_MULT": 1.5,  # 거래량이 평균의 N배 이상이면 돌파 확인
    "BREAKOUT_CLOSE_OUTSIDE": True,  # 종가가 볼린저 밖에 있어야 돌파

    # ── SL / TP ──
    "SL_MODE": "range",           # "range" (수렴구간), "atr" (ATR배수), "fibo" (피보나치)
    "SL_ATR_MULT": 1.5,           # ATR 모드 시 SL 거리
    "SL_RANGE_BUFFER": 0.3,       # range 모드 시 수렴구간 + 버퍼(ATR)

    "TP_MODE": "fibo",            # "fibo" (피보나치확장), "atr" (ATR배수)
    "TP_FIBO_LEVEL": 2.618,       # 피보나치 확장 레벨
    "TP_ATR_MULT": 3.0,           # ATR 모드 시 TP 거리

    # ── 필터 ──
    "MIN_ADX": 15,                # 스퀴즈 중 ADX 하한 (너무 죽은 장 제외)
    "MAX_ATR_PCT": 2.0,           # ATR% 상한 (너무 변동성 큰 코인 제외)
    "MIN_CONFIDENCE": 60,         # 최소 확신도
}

HEDGE_CONFIG = {
    # ── 헷지 설정 ──
    "HEDGE_SL_ATR_MULT": 0.8,    # 양쪽 SL (타이트하게)
    "HEDGE_TP_ATR_MULT": 3.0,    # 양쪽 TP (넓게)
    "HEDGE_SIZE_RATIO": 0.5,     # 각 방향 사이즈 (기본의 50%)
    "EARLY_EXIT_ROE": -15,       # 한쪽이 ROE -15% 이하면 빠르게 손절
    "WINNER_TRAIL_ROE": 10,      # 이긴 쪽 ROE 10% 이상이면 T-Stop 활성화
}


# ═══════════════════════════════════════════════════════════════
#  📊 시그널 데이터 클래스
# ═══════════════════════════════════════════════════════════════
@dataclass
class ConvergenceSignal:
    """수렴 돌파 시그널"""
    signal_type: str              # "CONVERGENCE_LONG" / "CONVERGENCE_SHORT"
    symbol: str
    direction: str                # "long" / "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    trailing_atr_mult: float
    confidence: int               # 0~100
    risk_reward: float
    reasons: List[str]
    strategy: str                 # "convergence"
    atr: float
    atr_pct: float
    details: Dict = field(default_factory=dict)
    timestamp: str = ""

    # squeeze 정보
    squeeze_candles: int = 0      # 스퀴즈 지속 캔들 수
    squeeze_range: float = 0      # 스퀴즈 구간 폭
    breakout_strength: float = 0  # 돌파 강도 (ATR 배수)


@dataclass
class HedgeSignal:
    """헷지 시그널 (롱+숏 동시)"""
    symbol: str
    long_entry: float; long_sl: float; long_tp: float
    short_entry: float; short_sl: float; short_tp: float
    confidence: int
    reasons: List[str]
    strategy: str = "hedge"
    atr: float = 0
    squeeze_candles: int = 0


# ═══════════════════════════════════════════════════════════════
#  1. EMA 수렴 돌파 전략 (ConvergenceBreakout)
# ═══════════════════════════════════════════════════════════════
class ConvergenceBreakout:
    """
    EMA 수렴 → 돌파 감지기

    작동 원리:
      1. EMA20과 EMA60의 gap이 줄어들면 "스퀴즈" 상태
      2. 볼린저밴드 폭(BBW)도 수축하면 스퀴즈 확인
      3. 스퀴즈 상태에서 강한 캔들이 나오면 "돌파"
      4. 돌파 방향으로 진입, 피보나치/ATR로 TP 설정

    진입 조건:
      - EMA gap < 0.3% (N캔들 연속)
      - 돌파 캔들: ATR × 1.5 이상 움직임
      - 거래량: 평균의 1.3배 이상
      - 종가: 볼린저밴드 밖

    SL/TP:
      - SL: 수렴 구간 반대쪽 + 버퍼
      - TP: 피보나치 1.618 확장 또는 ATR × 3
    """

    def __init__(self, config=None):
        self.cfg = {**CONVERGENCE_CONFIG, **(config or {})}

    def detect(self, df: pd.DataFrame) -> Optional[Dict]:
        """스퀴즈 + 돌파 감지"""
        if df is None or len(df) < 60:
            return None

        # 필요한 지표 확인
        required = ['Close', 'High', 'Low', 'Open', 'Volume']
        for col in required:
            if col not in df.columns:
                return None

        # 지표 계산 (없으면 직접)
        df = self._ensure_indicators(df)
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        atr = curr.get('atr', 0)
        if atr <= 0:
            return None

        atr_pct = curr.get('atr_pct', atr / curr['Close'] * 100)
        if atr_pct > self.cfg['MAX_ATR_PCT']:
            return None

        # ═══ STEP 1: 스퀴즈 감지 ═══
        squeeze_info = self._detect_squeeze(df)
        if squeeze_info is None:
            return None

        # ═══ STEP 2: 돌파 감지 ═══
        breakout = self._detect_breakout(df, squeeze_info)
        if breakout is None:
            return None

        # ═══ STEP 3: 시그널 구성 ═══
        return self._build_signal(df, squeeze_info, breakout)

    def _ensure_indicators(self, df):
        """필수 지표가 없으면 계산"""
        c = df['Close']

        if 'ema20' not in df.columns and 'close_20_ema' not in df.columns:
            ema_s = self.cfg.get('EMA_SHORT', 12)
            ema_l = self.cfg.get('EMA_LONG', 26)
            df['ema20'] = c.ewm(span=ema_s).mean()
            df['ema60'] = c.ewm(span=ema_l).mean()
        elif 'close_20_ema' in df.columns:
            df['ema20'] = df['close_20_ema']
            df['ema60'] = df['close_60_ema']

        if 'atr' not in df.columns:
            tr = pd.concat([
                df['High'] - df['Low'],
                (df['High'] - c.shift(1)).abs(),
                (df['Low'] - c.shift(1)).abs()
            ], axis=1).max(axis=1)
            df['atr'] = tr.ewm(alpha=1/14).mean()
            df['atr_pct'] = df['atr'] / c * 100

        if 'adx' not in df.columns:
            h = df['High']; lo = df['Low']
            tr = pd.concat([h-lo, (h-c.shift(1)).abs(), (lo-c.shift(1)).abs()], axis=1).max(axis=1)
            up = h.diff(); down = -lo.diff()
            p_dm = up.where((up>down)&(up>0), 0)
            m_dm = down.where((down>up)&(down>0), 0)
            tr_s = tr.ewm(alpha=1/14).mean()
            pdi = 100 * p_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
            mdi = 100 * m_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
            dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-9)
            df['adx'] = dx.ewm(alpha=1/14).mean()

        if 'boll_ub' not in df.columns:
            sma20 = c.rolling(20).mean()
            std20 = c.rolling(20).std()
            df['boll'] = sma20
            df['boll_ub'] = sma20 + std20 * 2
            df['boll_lb'] = sma20 - std20 * 2
            df['bbw'] = (df['boll_ub'] - df['boll_lb']) / (df['boll'] + 1e-9)
            df['bbw_avg'] = df['bbw'].rolling(20).mean()

        if 'rvol' not in df.columns:
            df['rvol'] = df['Volume'] / (df['Volume'].rolling(20).mean() + 1e-9)

        if 'macdh' not in df.columns:
            ema12 = c.ewm(span=12).mean()
            ema26 = c.ewm(span=26).mean()
            macd_line = ema12 - ema26
            macd_signal = macd_line.ewm(span=9).mean()
            df['macdh'] = macd_line - macd_signal

        if 'rsi_14' not in df.columns and 'rsi' not in df.columns:
            delta = c.diff()
            gain = delta.where(delta>0, 0).rolling(14).mean()
            loss = (-delta.where(delta<0, 0)).rolling(14).mean()
            df['rsi_14'] = 100 - (100 / (1 + gain / (loss + 1e-9)))

        return df

    def _detect_squeeze(self, df) -> Optional[Dict]:
        """EMA 수렴(스퀴즈) 감지"""
        cfg = self.cfg
        min_candles = cfg['MIN_SQUEEZE_CANDLES']
        max_candles = cfg['MAX_SQUEEZE_CANDLES']
        gap_threshold = cfg['EMA_GAP_THRESHOLD']

        # 최근 캔들에서 스퀴즈 구간 찾기
        squeeze_count = 0
        squeeze_high = -np.inf
        squeeze_low = np.inf

        for i in range(len(df) - 2, max(len(df) - max_candles - 2, 0), -1):
            row = df.iloc[i]
            ema20 = row.get('ema20', row.get('close_20_ema', 0))
            ema60 = row.get('ema60', row.get('close_60_ema', 0))
            price = row['Close']

            if price <= 0:
                break

            gap_pct = abs(ema20 - ema60) / price * 100

            if gap_pct <= gap_threshold:
                squeeze_count += 1
                squeeze_high = max(squeeze_high, row['High'])
                squeeze_low = min(squeeze_low, row['Low'])
            else:
                break  # 스퀴즈 끊김

        if squeeze_count < min_candles:
            return None

        # BBW 수축 확인 (보강)
        curr = df.iloc[-1]
        bbw = curr.get('bbw', 0)
        bbw_avg = curr.get('bbw_avg', bbw)
        bbw_squeeze = bbw < bbw_avg * cfg['BBW_SQUEEZE_RATIO'] if bbw_avg > 0 else False

        # ADX 체크
        adx = curr.get('adx', 0)
        if adx < cfg['MIN_ADX']:
            return None

        squeeze_range = squeeze_high - squeeze_low
        squeeze_mid = (squeeze_high + squeeze_low) / 2

        return {
            'squeeze_candles': squeeze_count,
            'squeeze_high': squeeze_high,
            'squeeze_low': squeeze_low,
            'squeeze_range': squeeze_range,
            'squeeze_mid': squeeze_mid,
            'bbw_squeeze': bbw_squeeze,
            'adx': adx,
        }

    def _detect_breakout(self, df, squeeze_info) -> Optional[Dict]:
        """스퀴즈 구간에서의 돌파 감지"""
        cfg = self.cfg
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        atr = curr.get('atr', 0)
        if atr <= 0:
            return None

        price = curr['Close']
        body = abs(curr['Close'] - curr['Open'])
        move = abs(curr['Close'] - prev['Close'])
        rvol = curr.get('rvol', 1.0)

        # 돌파 강도 체크
        breakout_atr = move / atr if atr > 0 else 0
        if breakout_atr < cfg['BREAKOUT_ATR_MULT']:
            return None

        # 거래량 체크
        if rvol < cfg['BREAKOUT_VOLUME_MULT']:
            return None

        # 방향 판정
        is_bullish = curr['Close'] > curr['Open']  # 양봉
        is_bearish = curr['Close'] < curr['Open']  # 음봉

        # 볼린저 밴드 밖 종가 체크
        if cfg['BREAKOUT_CLOSE_OUTSIDE']:
            boll_ub = curr.get('boll_ub', squeeze_info['squeeze_high'])
            boll_lb = curr.get('boll_lb', squeeze_info['squeeze_low'])

            if is_bullish and price <= boll_ub:
                return None  # 볼린저 상단 미돌파
            if is_bearish and price >= boll_lb:
                return None  # 볼린저 하단 미돌파

        # 수렴 구간 돌파 확인
        direction = None
        if is_bullish and price > squeeze_info['squeeze_high']:
            direction = "long"
        elif is_bearish and price < squeeze_info['squeeze_low']:
            direction = "short"
        else:
            return None

        # MACD 방향 확인 (보강)
        macdh = curr.get('macdh', 0)
        macdh_prev = prev.get('macdh', 0)
        macd_aligned = (direction == "long" and macdh > macdh_prev) or \
                       (direction == "short" and macdh < macdh_prev)

        return {
            'direction': direction,
            'breakout_atr': round(breakout_atr, 2),
            'volume_ratio': round(rvol, 2),
            'body_atr': round(body / atr, 2) if atr > 0 else 0,
            'macd_aligned': macd_aligned,
        }

    def _build_signal(self, df, squeeze, breakout) -> Dict:
        """시그널 구성 (SL/TP 계산)"""
        cfg = self.cfg
        curr = df.iloc[-1]
        price = curr['Close']
        atr = curr.get('atr', 0)
        direction = breakout['direction']

        # ── SL 계산 ──
        if cfg['SL_MODE'] == 'range':
            # 수렴 구간 반대쪽 + 버퍼
            buffer = atr * cfg['SL_RANGE_BUFFER']
            if direction == 'long':
                sl = squeeze['squeeze_low'] - buffer
            else:
                sl = squeeze['squeeze_high'] + buffer
        elif cfg['SL_MODE'] == 'atr':
            if direction == 'long':
                sl = price - atr * cfg['SL_ATR_MULT']
            else:
                sl = price + atr * cfg['SL_ATR_MULT']
        else:  # fibo
            if direction == 'long':
                sl = price - squeeze['squeeze_range'] * 0.5
            else:
                sl = price + squeeze['squeeze_range'] * 0.5

        # ── TP 계산 ──
        risk = abs(price - sl)
        if cfg['TP_MODE'] == 'fibo':
            # 피보나치 확장: 수렴구간 폭 × 1.618
            fibo_ext = squeeze['squeeze_range'] * cfg['TP_FIBO_LEVEL']
            if direction == 'long':
                tp = squeeze['squeeze_high'] + fibo_ext
            else:
                tp = squeeze['squeeze_low'] - fibo_ext
        else:  # atr
            if direction == 'long':
                tp = price + atr * cfg['TP_ATR_MULT']
            else:
                tp = price - atr * cfg['TP_ATR_MULT']

        rr = abs(tp - price) / risk if risk > 0 else 0

        # ── 확신도 계산 ──
        score = 0
        reasons = []

        # 스퀴즈 기간 점수 (길수록 폭발력 강함)
        if squeeze['squeeze_candles'] >= 20:
            score += 3; reasons.append(f"장기스퀴즈 {squeeze['squeeze_candles']}캔들")
        elif squeeze['squeeze_candles'] >= 10:
            score += 2; reasons.append(f"스퀴즈 {squeeze['squeeze_candles']}캔들")
        else:
            score += 1; reasons.append(f"단기스퀴즈 {squeeze['squeeze_candles']}캔들")

        # BBW 수축 (볼린저 밴드 좁아짐)
        if squeeze['bbw_squeeze']:
            score += 2; reasons.append("BB 수축 확인")

        # 돌파 강도
        if breakout['breakout_atr'] >= 2.0:
            score += 2; reasons.append(f"강한돌파 {breakout['breakout_atr']:.1f}ATR")
        else:
            score += 1; reasons.append(f"돌파 {breakout['breakout_atr']:.1f}ATR")

        # 거래량
        if breakout['volume_ratio'] >= 2.0:
            score += 2; reasons.append(f"거래량 폭발 {breakout['volume_ratio']:.1f}x")
        elif breakout['volume_ratio'] >= 1.3:
            score += 1; reasons.append(f"거래량 증가 {breakout['volume_ratio']:.1f}x")

        # MACD 방향
        if breakout['macd_aligned']:
            score += 1; reasons.append("MACD 방향일치")

        # ADX
        if squeeze['adx'] > 25:
            score += 1; reasons.append(f"ADX {squeeze['adx']:.0f}")

        confidence = min(100, int(score / 12 * 100))

        rsi = curr.get('rsi_14', curr.get('rsi', 50))

        return {
            'signal_type': f"CONVERGENCE_{direction.upper()}",
            'direction': direction,
            'entry_price': price,
            'stop_loss': sl,
            'take_profit': tp,
            'risk_reward': round(rr, 2),
            'confidence': confidence,
            'reasons': reasons,
            'strategy': 'convergence',
            'atr': atr,
            'atr_pct': curr.get('atr_pct', atr / price * 100),
            'squeeze_candles': squeeze['squeeze_candles'],
            'squeeze_range': squeeze['squeeze_range'],
            'breakout_strength': breakout['breakout_atr'],
            'details': {
                'squeeze': squeeze,
                'breakout': breakout,
                'rsi': rsi,
                'adx': squeeze['adx'],
            }
        }


# ═══════════════════════════════════════════════════════════════
#  2. 헷지 돌파 전략 (HedgeBreakout)
# ═══════════════════════════════════════════════════════════════
class HedgeBreakout:
    """
    스퀴즈 감지 시 롱+숏 동시 진입

    작동 원리:
      1. ConvergenceBreakout과 같은 스퀴즈 감지
      2. 돌파를 기다리지 않고 스퀴즈 상태에서 양방향 진입
      3. 한쪽이 SL(타이트) → 반대쪽이 TP(넓게)
      4. 비대칭 R:R로 한쪽 수익이 양쪽 손실을 커버

    포지션 구조:
      롱: entry=현재가, SL=squeeze_low - buffer, TP=squeeze_high + fibo
      숏: entry=현재가, SL=squeeze_high + buffer, TP=squeeze_low - fibo
      → 돌파 방향의 TP가 반대쪽 SL보다 훨씬 멀어서 비대칭 수익
    """

    def __init__(self, convergence_config=None, hedge_config=None):
        self.convergence = ConvergenceBreakout(convergence_config)
        self.cfg = {**HEDGE_CONFIG, **(hedge_config or {})}

    def detect(self, df: pd.DataFrame) -> Optional[Dict]:
        """스퀴즈 감지 → 양방향 시그널"""
        if df is None or len(df) < 60:
            return None

        df = self.convergence._ensure_indicators(df)
        squeeze = self.convergence._detect_squeeze(df)
        if squeeze is None:
            return None

        curr = df.iloc[-1]
        price = curr['Close']
        atr = curr.get('atr', 0)
        if atr <= 0:
            return None

        cfg = self.cfg
        buffer = atr * 0.3  # SL 버퍼

        # ── 롱 포지션 ──
        long_sl = squeeze['squeeze_low'] - buffer
        long_tp_fibo = squeeze['squeeze_high'] + squeeze['squeeze_range'] * 1.618
        long_tp_atr = price + atr * cfg['HEDGE_TP_ATR_MULT']
        long_tp = max(long_tp_fibo, long_tp_atr)  # 더 넓은 TP

        # ── 숏 포지션 ──
        short_sl = squeeze['squeeze_high'] + buffer
        short_tp_fibo = squeeze['squeeze_low'] - squeeze['squeeze_range'] * 1.618
        short_tp_atr = price - atr * cfg['HEDGE_TP_ATR_MULT']
        short_tp = min(short_tp_fibo, short_tp_atr)  # 더 넓은 TP

        # RR 계산
        long_risk = abs(price - long_sl)
        long_reward = abs(long_tp - price)
        short_risk = abs(short_sl - price)
        short_reward = abs(price - short_tp)

        long_rr = long_reward / long_risk if long_risk > 0 else 0
        short_rr = short_reward / short_risk if short_risk > 0 else 0

        # 최소 RR 체크 (한쪽이라도 1.5 이상)
        if max(long_rr, short_rr) < 1.5:
            return None

        reasons = [
            f"스퀴즈 {squeeze['squeeze_candles']}캔들",
            f"구간폭 {squeeze['squeeze_range']/atr:.1f}ATR",
        ]
        if squeeze['bbw_squeeze']:
            reasons.append("BB 수축")

        confidence = min(100, int(squeeze['squeeze_candles'] / 20 * 60))
        if squeeze['bbw_squeeze']:
            confidence += 15
        confidence = min(100, confidence)

        return {
            'strategy': 'hedge',
            'symbol': '',  # 호출자가 설정
            'long_entry': price, 'long_sl': long_sl, 'long_tp': long_tp,
            'short_entry': price, 'short_sl': short_sl, 'short_tp': short_tp,
            'long_rr': round(long_rr, 2), 'short_rr': round(short_rr, 2),
            'confidence': confidence,
            'reasons': reasons,
            'atr': atr,
            'atr_pct': curr.get('atr_pct', atr / price * 100),
            'squeeze_candles': squeeze['squeeze_candles'],
            'squeeze_range': squeeze['squeeze_range'],
            'size_ratio': cfg['HEDGE_SIZE_RATIO'],
        }


# ═══════════════════════════════════════════════════════════════
#  🔌 DualStrategyEngine 연동 어댑터
# ═══════════════════════════════════════════════════════════════
def convergence_to_dual_signal(result, symbol):
    """ConvergenceBreakout 결과 → DualSignal 호환 dict"""
    if result is None:
        return None
    return {
        'signal_type': result['signal_type'],
        'symbol': symbol,
        'direction': result['direction'],
        'entry_price': result['entry_price'],
        'stop_loss': result['stop_loss'],
        'take_profit': result['take_profit'],
        'trailing_atr_mult': 1.0,
        'confidence': result['confidence'],
        'risk_reward': result['risk_reward'],
        'reasons': result['reasons'],
        'strategy': 'convergence',
        'atr': result['atr'],
        'atr_pct': result['atr_pct'],
    }


# ═══════════════════════════════════════════════════════════════
#  🧪 단독 백테스트
# ═══════════════════════════════════════════════════════════════
def backtest_convergence(coins=None, months=6, config=None, hedge=False):
    """수렴 돌파 전략 간이 백테스트"""
    import os, glob

    data_dir = "./Raw_Data/CRYPTO_BINANCE_15M"
    if coins:
        files = [f"{data_dir}/{c}_USDT.csv" for c in coins if os.path.exists(f"{data_dir}/{c}_USDT.csv")]
    else:
        files = sorted(glob.glob(f"{data_dir}/*.csv"))

    if not files:
        print("❌ 데이터 없음. download_data.py를 먼저 실행하세요.")
        return

    detector = ConvergenceBreakout(config)
    hedge_detector = HedgeBreakout(config) if hedge else None

    balance = 1000.0
    initial = balance
    leverage = 20
    risk_per_trade = 0.015
    fee_rate = 0.0004
    trades = []

    print(f"\n{'='*60}")
    print(f"  {'🔀 헷지' if hedge else '📐 수렴 돌파'} 백테스트")
    print(f"  코인: {len(files)}개 | 레버리지: {leverage}x")
    print(f"{'='*60}\n")

    for fpath in files:
        code = os.path.basename(fpath).replace('.csv', '')
        df = pd.read_csv(fpath)
        if len(df) < 100:
            continue
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)

        for i in range(80, len(df)):
            window = df.iloc[max(0, i-100):i+1].copy()
            if len(window) < 60:
                continue

            if hedge and hedge_detector:
                result = hedge_detector.detect(window)
                if result is None:
                    continue

                # 헷지: 양방향 동시 진입 시뮬레이션
                price = result['long_entry']
                for direction in ['long', 'short']:
                    sl = result[f'{direction}_sl']
                    tp = result[f'{direction}_tp']
                    risk = abs(price - sl)
                    if risk <= 0:
                        continue

                    margin = balance * risk_per_trade * result['size_ratio']
                    if margin < 5 or margin > balance * 0.3:
                        continue
                    qty = margin * leverage / price
                    fee = qty * price * fee_rate * 2

                    # 간이 시뮬: 이후 48캔들 내에서 SL/TP 도달 여부
                    future = df.iloc[i+1:min(i+49, len(df))]
                    exit_price = price
                    exit_reason = "시간초과"

                    for _, frow in future.iterrows():
                        if direction == 'long':
                            if frow['Low'] <= sl:
                                exit_price = sl; exit_reason = "SL"; break
                            if frow['High'] >= tp:
                                exit_price = tp; exit_reason = "TP"; break
                        else:
                            if frow['High'] >= sl:
                                exit_price = sl; exit_reason = "SL"; break
                            if frow['Low'] <= tp:
                                exit_price = tp; exit_reason = "TP"; break

                    if exit_reason == "시간초과":
                        exit_price = future.iloc[-1]['Close'] if len(future) > 0 else price

                    if direction == 'long':
                        pnl = (exit_price - price) * qty - fee
                    else:
                        pnl = (price - exit_price) * qty - fee

                    balance += pnl
                    trades.append({
                        'code': code, 'direction': direction,
                        'entry': price, 'exit': exit_price,
                        'pnl': round(pnl, 2), 'reason': exit_reason,
                        'time': str(window.index[-1]),
                    })

            else:
                result = detector.detect(window)
                if result is None:
                    continue
                if result['confidence'] < detector.cfg['MIN_CONFIDENCE']:
                    continue

                direction = result['direction']
                price = result['entry_price']
                sl = result['stop_loss']
                tp = result['take_profit']
                risk = abs(price - sl)
                if risk <= 0:
                    continue

                margin = balance * risk_per_trade
                if margin < 5:
                    continue
                qty = margin * leverage / price
                fee = qty * price * fee_rate * 2

                # 간이 시뮬: 이후 48캔들 내에서 SL/TP 도달 여부
                future = df.iloc[i+1:min(i+49, len(df))]
                exit_price = price
                exit_reason = "시간초과"

                for _, frow in future.iterrows():
                    if direction == 'long':
                        if frow['Low'] <= sl:
                            exit_price = sl; exit_reason = "SL"; break
                        if frow['High'] >= tp:
                            exit_price = tp; exit_reason = "TP"; break
                    else:
                        if frow['High'] >= sl:
                            exit_price = sl; exit_reason = "SL"; break
                        if frow['Low'] <= tp:
                            exit_price = tp; exit_reason = "TP"; break

                if exit_reason == "시간초과":
                    exit_price = future.iloc[-1]['Close'] if len(future) > 0 else price

                if direction == 'long':
                    pnl = (exit_price - price) * qty - fee
                else:
                    pnl = (price - exit_price) * qty - fee

                balance += pnl
                trades.append({
                    'code': code, 'direction': direction,
                    'entry': price, 'exit': exit_price,
                    'pnl': round(pnl, 2), 'reason': exit_reason,
                    'squeeze': result['squeeze_candles'],
                    'rr': result['risk_reward'],
                    'time': str(window.index[-1]),
                })

    # 결과 출력
    if not trades:
        print("❌ 거래 없음 (스퀴즈 조건을 완화해보세요)")
        return

    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades['pnl'] > 0]
    losses = df_trades[df_trades['pnl'] <= 0]

    roi = (balance - initial) / initial * 100
    mdd = min(0, df_trades['pnl'].cumsum().min()) / initial * 100

    print(f"\n{'='*60}")
    print(f"  📊 결과")
    print(f"{'='*60}")
    print(f"  ${initial:.0f} → ${balance:.0f} (ROI {roi:+.1f}%)")
    print(f"  거래: {len(trades)}건 | 승: {len(wins)} 패: {len(losses)}")
    if len(trades) > 0:
        wr = len(wins) / len(trades) * 100
        print(f"  승률: {wr:.1f}%")
    if len(wins) > 0 and len(losses) > 0:
        pf = wins['pnl'].mean() / abs(losses['pnl'].mean())
        print(f"  손익비: {pf:.2f}")
        print(f"  평균 수익: ${wins['pnl'].mean():.2f} | 평균 손실: ${losses['pnl'].mean():.2f}")

    # 청산 사유
    print(f"\n  청산 사유:")
    for reason, group in df_trades.groupby('reason'):
        print(f"    {reason:10s}: {len(group):3d}건 ${group['pnl'].sum():+.2f}")

    # 코인별
    if len(df_trades['code'].unique()) > 1:
        print(f"\n  코인별:")
        for code, group in df_trades.groupby('code'):
            wr = (group['pnl'] > 0).mean() * 100
            print(f"    {code:15s}: {len(group):3d}건 승률{wr:.0f}% ${group['pnl'].sum():+.2f}")

    print(f"{'='*60}")
    return df_trades


# ═══════════════════════════════════════════════════════════════
#  🚀 CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    coins = None
    months = 6
    hedge = '--hedge' in args
    config = {}

    if '--coins' in args:
        i = args.index('--coins')
        coins = []
        for a in args[i+1:]:
            if a.startswith('--'): break
            coins.append(a.upper())

    if '--months' in args:
        i = args.index('--months')
        months = int(args[i+1])

    if '--gap' in args:
        i = args.index('--gap')
        config['EMA_GAP_THRESHOLD'] = float(args[i+1])

    if '--squeeze-min' in args:
        i = args.index('--squeeze-min')
        config['MIN_SQUEEZE_CANDLES'] = int(args[i+1])

    if '--tp-mode' in args:
        i = args.index('--tp-mode')
        config['TP_MODE'] = args[i+1]

    if hedge:
        print("🔀 헷지 모드")
    else:
        print("📐 수렴 돌파 모드")

    backtest_convergence(coins=coins, months=months, config=config, hedge=hedge)
