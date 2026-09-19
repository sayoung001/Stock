"""
dual_strategy.py
================
양방향 2단계 전략 코어 엔진

전략 A: 모멘텀(Breakout) — 급등/급락이 시작될 때 탑승
전략 B: 반전(Reversal)  — 급등/급락이 소진될 때 반대매매

두 전략은 시간축이 다릅니다:
  모멘텀 = "지금 터지고 있다" → 같은 방향으로 진입
  반전   = "터진 게 끝나간다" → 반대 방향으로 진입

의존성: indicators_v2.py, coinglass_client.py, spike_reversal.py
"""

from unittest import signals

from unittest import signals

import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum
from convergence_strategy import ConvergenceBreakout, convergence_to_dual_signal

KST = timezone(timedelta(hours=9))

# BTC 필터 (선택적)
try:
    from btc_filter import BTCFilter, BTCContext
    HAS_BTC_FILTER = True
except ImportError:
    HAS_BTC_FILTER = False


# ╔══════════════════════════════════════════════════════════════╗
# ║  급등/급락을 "전부" 먹는 원리                                  ║
# ╠══════════════════════════════════════════════════════════════╣
# ║                                                              ║
# ║  타임라인:                                                    ║
# ║                                                              ║
# ║  ─────┬──────────────┬──────────────┬────────────            ║
# ║       │  🔥 브레이크아웃  │  ⚡ 스파이크 절정 │  🔄 되돌림       ║
# ║       │  (시작)         │  (고갈)         │  (반전)            ║
# ║  ─────┴──────────────┴──────────────┴────────────            ║
# ║       ↑                ↑                ↑                     ║
# ║    모멘텀 진입        모멘텀 청산       반전 진입               ║
# ║    (전략 A)          (트레일링)        (전략 B)                ║
# ║                                                              ║
# ║  핵심: 같은 코인에서 2번 수익 기회!                            ║
# ║  1차: 급등 시작 → 급등 꼭대기 (모멘텀 수익)                    ║
# ║  2차: 급등 꼭대기 → 되돌림 완료 (반전 수익)                    ║
# ╚══════════════════════════════════════════════════════════════╝


class SignalType(Enum):
    MOMENTUM_LONG  = "momentum_long"     # 급등 탑승 (롱)
    MOMENTUM_SHORT = "momentum_short"    # 급락 탑승 (숏)
    REVERSAL_LONG  = "reversal_long"     # 급락 후 반전 (롱)
    REVERSAL_SHORT = "reversal_short"    # 급등 후 반전 (숏)


@dataclass
class DualSignal:
    """통합 시그널"""
    signal_type: str              # SignalType value
    symbol: str
    direction: str                # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    trailing_atr_mult: float      # 트레일링 스탑 ATR 배수
    confidence: int               # 0~100
    risk_reward: float
    reasons: List[str]
    strategy: str                 # "momentum" or "reversal"
    atr: float
    atr_pct: float
    details: Dict = field(default_factory=dict)
    timestamp: str = ""


# ╔══════════════════════════════════════════════════════════════╗
# ║            전략 A: 모멘텀(Breakout) 감지기                     ║
# ║                                                              ║
# ║   급등/급락이 "시작"되는 순간을 포착합니다.                     ║
# ║   핵심: 거래량 폭발 + 가격 돌파 + 추세 강화                    ║
# ╚══════════════════════════════════════════════════════════════╝

class MomentumDetector:
    """
    브레이크아웃(돌파) 감지기

    감지 조건 (점수 기반, 각 1점):
      1. 거래량 폭발: RVOL > 2.0 (평균의 2배)
      2. 볼린저밴드 돌파: 가격이 상/하단 밖으로 이탈
      3. 캔들 크기: 현재 캔들 몸통 > 1.5 ATR
      4. ADX 급등: ADX가 직전 대비 +3 이상 상승
      5. 연속 방향: 2~3캔들 연속 같은 방향
      6. MA20 돌파: 가격이 20이평을 관통
      7. MACD 크로스: 히스토그램 방향 전환 + 가속

    진입 조건: 5점 이상 (7점 만점)

    모멘텀은 "이미 시작된 움직임에 올라타는 것"이므로
    반전 전략보다 진입이 빠르고, 손절도 타이트합니다.
    """

    def __init__(
        self,
        min_rvol: float = 2.0,
        min_body_atr: float = 1.2,
        min_score: int = 4,
        max_atr_pct: float = 3.0,    # 변동성 상한 (20배 기준)
    ):
        self.min_rvol = min_rvol
        self.min_body_atr = min_body_atr
        self.min_score = min_score
        self.max_atr_pct = max_atr_pct

    def detect(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        최신 캔들에서 모멘텀 브레이크아웃 감지.

        Returns:
            {
              'direction': 'pump' | 'dump',
              'score': int,
              'reasons': list,
              'details': dict,
            } or None
        """
        if df is None or len(df) < 30:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        atr = curr.get('atr', 0)
        if atr <= 0:
            return None

        atr_pct = curr.get('atr_pct', 99)
        if atr_pct > self.max_atr_pct:
            return None

        # 현재 캔들 방향
        body = curr['Close'] - curr['Open']
        body_abs = abs(body)
        is_bullish = body > 0
        is_bearish = body < 0

        score = 0
        reasons = []

        # ── 1. 거래량 폭발 ──────────────────────────────────
        rvol = curr.get('rvol', 0)
        if rvol >= self.min_rvol:
            score += 1
            reasons.append(f"거래량 폭발 (RVOL {rvol:.1f}x)")
        if rvol >= self.min_rvol * 1.5:   # 3배 이상이면 보너스
            score += 1
            reasons.append(f"극단적 거래량 ({rvol:.1f}x)")

        # ── 2. 볼린저밴드 돌파 ───────────────────────────────
        if 'boll_ub' in df.columns:
            if is_bullish and curr['Close'] > curr['boll_ub']:
                score += 1
                reasons.append("볼린저 상단 돌파 ↑")
            elif is_bearish and curr['Close'] < curr['boll_lb']:
                score += 1
                reasons.append("볼린저 하단 붕괴 ↓")

        # ── 3. 캔들 몸통 크기 ────────────────────────────────
        body_atr_ratio = body_abs / atr
        if body_atr_ratio >= self.min_body_atr:
            score += 1
            reasons.append(f"대형 캔들 ({body_atr_ratio:.1f} ATR)")

        # ── 4. ADX 급등 ─────────────────────────────────────
        adx_now = curr.get('adx', 0)
        adx_prev = prev.get('adx', 0)
        if adx_now - adx_prev >= 3:
            score += 1
            reasons.append(f"ADX 급등 ({adx_prev:.0f}→{adx_now:.0f})")

        # ── 5. 연속 방향 (2~3캔들) ──────────────────────────
        prev_bull = prev['Close'] > prev['Open']
        prev2_bull = prev2['Close'] > prev2['Open']
        if is_bullish and prev_bull:
            score += 1
            reasons.append("연속 양봉 ↑↑")
        elif is_bearish and not prev_bull:
            score += 1
            reasons.append("연속 음봉 ↓↓")

        # ── 6. MA20 돌파 ────────────────────────────────────
        ma20 = curr.get('close_20_ema', 0)
        if is_bullish and prev['Close'] < ma20 and curr['Close'] > ma20:
            score += 1
            reasons.append("20EMA 상향 돌파 ↑")
        elif is_bearish and prev['Close'] > ma20 and curr['Close'] < ma20:
            score += 1
            reasons.append("20EMA 하향 붕괴 ↓")

        # ── 7. MACD 가속 ────────────────────────────────────
        macdh = curr.get('macdh', 0)
        macdh_prev = prev.get('macdh', 0)
        if is_bullish and macdh > macdh_prev and macdh > 0:
            score += 1
            reasons.append("MACD 상승 가속")
        elif is_bearish and macdh < macdh_prev and macdh < 0:
            score += 1
            reasons.append("MACD 하락 가속")

        # ── 판정 ────────────────────────────────────────────
        if score < self.min_score:
            return None

        direction = "pump" if is_bullish else "dump"

        return {
            'direction': direction,
            'score': score,
            'max_score': 8,
            'reasons': reasons,
            'details': {
                'close': curr['Close'],
                'atr': atr,
                'atr_pct': atr_pct,
                'rvol': rvol,
                'rsi': curr.get('rsi_14', 50),
                'adx': adx_now,
                'body_atr': round(body_atr_ratio, 2),
                'macdh': macdh,
            }
        }


# ╔══════════════════════════════════════════════════════════════╗
# ║          전략 B: 반전(Reversal) 감지기 (간소화 버전)           ║
# ║                                                              ║
# ║   spike_reversal.py의 4단계를 하나로 통합                     ║
# ╚══════════════════════════════════════════════════════════════╝

class ReversalDetector:
    """
    급등/급락 후 반전 감지기 (spike_reversal.py 통합 버전)

    3단계 판정:
      1. 스파이크 존재 확인 (N캔들 내 M ATR 이동)
      2. 고갈 징후 (거래량↓, 몸통↓, 꼬리 길어짐, RSI 극단)
      3. 반전 캔들 (방향 전환 + 거래량 동반)
    """

    def __init__(
        self,
        lookback: int = 8,
        min_spike_atr: float = 4.0,    # 2.0→4.0 (진짜 급락만)
        min_spike_rvol: float = 1.3,
        min_exhaustion: int = 3,        # 2→3 (고갈 확실해야)
        min_reversal: int = 3,          # 2→3 (반전 확실해야)
    ):
        self.lookback = lookback
        self.min_spike_atr = min_spike_atr
        self.min_spike_rvol = min_spike_rvol
        self.min_exhaustion = min_exhaustion
        self.min_reversal = min_reversal

    def detect(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Returns:
            {
              'spike_direction': 'pump' | 'dump',
              'entry_direction': 'long' | 'short' (스파이크 반대),
              'spike_move_atr': float,
              'spike_move_pct': float,
              'peak_price': float,
              'start_price': float,
              'exhaustion_score': int,
              'reversal_score': int,
              'total_score': int,
              'reasons': list,
              'details': dict,
            } or None
        """
        if df is None or len(df) < self.lookback + 20:
            return None

        curr = df.iloc[-1]
        atr = curr.get('atr', 0)
        if atr <= 0:
            return None

        # ── 1. 스파이크 존재 확인 ────────────────────────────
        # lookback 범위 중 가장 큰 움직임 구간 찾기
        best_spike = None
        for start_offset in range(self.lookback + 4, 3, -1):
            for end_offset in range(start_offset - 3, max(0, start_offset - self.lookback - 1), -1):
                window = df.iloc[-(start_offset):-(end_offset) if end_offset > 0 else len(df)]
                if len(window) < 3:
                    continue
                w_open = window.iloc[0]['Open']
                w_high = window['High'].max()
                w_low = window['Low'].min()

                up_move = (w_high - w_open) / atr
                down_move = (w_open - w_low) / atr

                if up_move >= self.min_spike_atr and up_move > down_move:
                    spike_dir = "pump"
                    peak = w_high
                    move_atr = up_move
                elif down_move >= self.min_spike_atr and down_move > up_move:
                    spike_dir = "dump"
                    peak = w_low
                    move_atr = down_move
                else:
                    continue

                # 거래량 체크
                avg_vol = df.iloc[-(start_offset + 20):-(start_offset)]['Volume'].mean()
                spike_vol = window['Volume'].mean()
                rvol = spike_vol / (avg_vol + 1e-9)
                if rvol < self.min_spike_rvol:
                    continue

                if best_spike is None or move_atr > best_spike['move_atr']:
                    best_spike = {
                        'direction': spike_dir,
                        'peak': peak,
                        'start_price': w_open,
                        'move_atr': move_atr,
                        'move_pct': abs(peak - w_open) / w_open * 100,
                        'rvol': rvol,
                    }

        if best_spike is None:
            return None

        # ── 2. 고갈 징후 ────────────────────────────────────
        recent_3 = df.iloc[-3:]
        exh_score = 0
        exh_reasons = []

        # 거래량 연속 감소
        vols = recent_3['Volume'].values
        if vols[0] > vols[1] > vols[2]:
            exh_score += 1
            exh_reasons.append("거래량 연속↓")

        # 몸통 연속 감소
        bodies = abs(recent_3['Close'] - recent_3['Open']).values
        if bodies[0] > bodies[1] > bodies[2]:
            exh_score += 1
            exh_reasons.append("몸통 연속↓")

        # 반대 방향 꼬리
        body = abs(curr['Close'] - curr['Open'])
        if best_spike['direction'] == "dump":
            wick = min(curr['Open'], curr['Close']) - curr['Low']
        else:
            wick = curr['High'] - max(curr['Open'], curr['Close'])
        if body > 0 and wick > body * 1.5:
            exh_score += 1
            exh_reasons.append("긴 반대꼬리")

        # RSI 극단
        rsi = curr.get('rsi_14', 50)
        if (best_spike['direction'] == "pump" and rsi > 78) or \
           (best_spike['direction'] == "dump" and rsi < 22):
            exh_score += 1
            exh_reasons.append(f"RSI 극단 ({rsi:.0f})")

        # 볼린저 3σ 이탈
        if 'boll' in df.columns:
            boll_std = (curr['boll_ub'] - curr['boll']) / 2
            if best_spike['direction'] == "pump" and curr['Close'] > curr['boll'] + boll_std * 3:
                exh_score += 1
                exh_reasons.append("BB 3σ 이탈")
            elif best_spike['direction'] == "dump" and curr['Close'] < curr['boll'] - boll_std * 3:
                exh_score += 1
                exh_reasons.append("BB 3σ 이탈")

        if exh_score < self.min_exhaustion:
            return None

        # ── 3. 반전 캔들 확인 ────────────────────────────────
        rev_score = 0
        rev_reasons = []
        prev = df.iloc[-2]

        is_bull = curr['Close'] > curr['Open']
        is_bear = curr['Close'] < curr['Open']

        # 반대 방향 캔들
        if best_spike['direction'] == "dump" and is_bull:
            rev_score += 1
            rev_reasons.append("급락 후 양봉")
        elif best_spike['direction'] == "pump" and is_bear:
            rev_score += 1
            rev_reasons.append("급등 후 음봉")

        # 거래량 동반
        avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
        if curr['Volume'] > avg_vol * 1.2:
            rev_score += 1
            rev_reasons.append("거래량 동반 반전")

        # MACD 꺾임
        if 'macdh' in df.columns:
            if best_spike['direction'] == "dump" and curr['macdh'] > prev['macdh']:
                rev_score += 1
                rev_reasons.append("MACD 반등")
            elif best_spike['direction'] == "pump" and curr['macdh'] < prev['macdh']:
                rev_score += 1
                rev_reasons.append("MACD 하락전환")

        # 장악형
        if best_spike['direction'] == "dump" and is_bull and not (prev['Close'] > prev['Open']):
            if curr['Close'] > prev['Open'] and curr['Open'] < prev['Close']:
                rev_score += 1
                rev_reasons.append("상승 장악형")
        elif best_spike['direction'] == "pump" and is_bear and (prev['Close'] > prev['Open']):
            if curr['Close'] < prev['Open'] and curr['Open'] > prev['Close']:
                rev_score += 1
                rev_reasons.append("하락 장악형")

        if rev_score < self.min_reversal:
            return None

        # ── 결과 ────────────────────────────────────────────
        entry_dir = "long" if best_spike['direction'] == "dump" else "short"
        total = exh_score + rev_score

        return {
            'spike_direction': best_spike['direction'],
            'entry_direction': entry_dir,
            'spike_move_atr': round(best_spike['move_atr'], 2),
            'spike_move_pct': round(best_spike['move_pct'], 2),
            'peak_price': best_spike['peak'],
            'start_price': best_spike['start_price'],
            'exhaustion_score': exh_score,
            'reversal_score': rev_score,
            'total_score': total,
            'reasons': exh_reasons + rev_reasons,
            'details': {
                'close': curr['Close'],
                'atr': atr,
                'atr_pct': curr.get('atr_pct', 0),
                'rsi': rsi,
                'adx': curr.get('adx', 0),
                'rvol': curr.get('rvol', 0),
            }
        }


# ╔══════════════════════════════════════════════════════════════╗
# ║          온체인 보강 (CoinGlass 통합)                         ║
# ╚══════════════════════════════════════════════════════════════╝

class OnchainBooster:
    """CoinGlass 데이터로 시그널 확신도를 보강"""

    @staticmethod
    def boost_momentum(cg_client, symbol: str, direction: str) -> Tuple[List[str], int]:
        """모멘텀 시그널에 온체인 근거 추가 (0~4점)"""
        reasons = []
        score = 0
        base = symbol.split("/")[0] if "/" in symbol else symbol.replace("_USDT", "")

        try:
            # OI 증가 = 새 포지션 유입 = 모멘텀 지속 가능성
            oi = cg_client.get_oi_change(base, "1h")
            if oi and oi['change_pct'] > 2:
                reasons.append(f"OI 증가 +{oi['change_pct']:.1f}% (신규 진입 유입)")
                score += 1

            # Taker 압력이 같은 방향
            taker = cg_client.get_taker_pressure(base)
            if taker:
                if direction == "pump" and taker['signal'] in ('BUY_PRESSURE', 'STRONG_BUY_PRESSURE'):
                    reasons.append(f"Taker 매수 우위 ({taker['sell_buy_ratio']:.2f})")
                    score += 1
                elif direction == "dump" and taker['signal'] in ('SELL_PRESSURE', 'STRONG_SELL_PRESSURE'):
                    reasons.append(f"Taker 매도 우위 ({taker['sell_buy_ratio']:.2f})")
                    score += 1

            # 펀딩비가 반대 방향 = 숏스퀴즈/롱스퀴즈 연료
            funding = cg_client.get_funding_rate_trend(base)
            if funding:
                rate = funding['current']
                if direction == "pump" and rate < -0.0002:
                    reasons.append(f"음수 펀딩비 → 숏스퀴즈 연료 ({rate*100:.4f}%)")
                    score += 1
                elif direction == "dump" and rate > 0.0003:
                    reasons.append(f"양수 펀딩비 → 롱스퀴즈 연료 ({rate*100:.4f}%)")
                    score += 1

            # LS 비율 — 반대편 포지션 과다 = 청산 연료
            ls = cg_client.get_global_ls_ratio(base)
            if ls:
                if direction == "pump" and ls['short_pct'] > 55:
                    reasons.append(f"숏 과다 {ls['short_pct']:.0f}% → 숏스퀴즈 연료")
                    score += 1
                elif direction == "dump" and ls['long_pct'] > 60:
                    reasons.append(f"롱 과다 {ls['long_pct']:.0f}% → 롱스퀴즈 연료")
                    score += 1
        except:
            pass

        return reasons, score

    @staticmethod
    def boost_reversal(cg_client, symbol: str, spike_direction: str) -> Tuple[List[str], int]:
        """반전 시그널에 온체인 근거 추가 (0~4점)"""
        reasons = []
        score = 0
        base = symbol.split("/")[0] if "/" in symbol else symbol.replace("_USDT", "")

        try:
            # OI 급감 = 청산 완료 = 연료 소진 (가장 중요!)
            oi = cg_client.get_oi_change(base, "1h")
            if oi and oi['change_pct'] < -2:
                reasons.append(f"OI 급감 {oi['change_pct']:+.1f}% (청산 완료)")
                score += 1

            # Taker 반전
            taker = cg_client.get_taker_pressure(base)
            if taker:
                if spike_direction == "dump" and taker['signal'] in ('BUY_PRESSURE', 'STRONG_BUY_PRESSURE'):
                    reasons.append(f"Taker 매수 전환 ({taker['sell_buy_ratio']:.2f})")
                    score += 1
                elif spike_direction == "pump" and taker['signal'] in ('SELL_PRESSURE', 'STRONG_SELL_PRESSURE'):
                    reasons.append(f"Taker 매도 전환 ({taker['sell_buy_ratio']:.2f})")
                    score += 1

            # 펀딩비 극단 → 같은 방향 과열 → 반전 유리
            funding = cg_client.get_funding_rate_trend(base)
            if funding:
                rate = funding['current']
                if spike_direction == "pump" and rate > 0.0005:
                    reasons.append(f"펀딩비 극양수 → 롱과열 ({rate*100:.4f}%)")
                    score += 1
                elif spike_direction == "dump" and rate < -0.0003:
                    reasons.append(f"펀딩비 극음수 → 숏과열 ({rate*100:.4f}%)")
                    score += 1

            # LS 개미 쏠림
            ls = cg_client.get_global_ls_ratio(base)
            if ls:
                if spike_direction == "pump" and ls['long_pct'] > 65:
                    reasons.append(f"개미 롱쏠림 {ls['long_pct']:.0f}% → 반전 근거")
                    score += 1
                elif spike_direction == "dump" and ls['short_pct'] > 60:
                    reasons.append(f"개미 숏쏠림 {ls['short_pct']:.0f}% → 반전 근거")
                    score += 1
        except:
            pass

        return reasons, score


# ╔══════════════════════════════════════════════════════════════╗
# ║                 통합 시그널 생성기                              ║
# ╚══════════════════════════════════════════════════════════════╝

class DualStrategyEngine:
    """
    모멘텀 + 반전을 하나로 통합한 시그널 생성기.
    BTCContext 연동으로 알트 시그널의 방향 차단 및 확신도를 보정합니다.

    사용법:
        engine = DualStrategyEngine(cg_client=coinglass)
        btc_ctx = btc_filter.analyze()  # BTCContext
        signals = engine.scan(df, symbol, btc_ctx=btc_ctx)
    """

    def __init__(
        self,
        cg_client=None,
        # 모멘텀 파라미터
        momentum_min_rvol: float = 2.0,
        momentum_min_body_atr: float = 1.2,
        momentum_min_score: int = 4,
        # 반전 파라미터
        reversal_lookback: int = 8,
        reversal_min_spike_atr: float = 4.0,
        reversal_min_exhaustion: int = 3,
        reversal_min_reversal: int = 3,
        # 공통
        max_atr_pct: float = 3.0,
        min_confidence: int = 35,
    ):
        self.cg = cg_client
        self.min_confidence = min_confidence

        self.momentum = MomentumDetector(
            min_rvol=momentum_min_rvol,
            min_body_atr=momentum_min_body_atr,
            min_score=momentum_min_score,
            max_atr_pct=max_atr_pct,
        )
        self.reversal = ReversalDetector(
            lookback=reversal_lookback,
            min_spike_atr=reversal_min_spike_atr,
            min_exhaustion=reversal_min_exhaustion,
            min_reversal=reversal_min_reversal,
        )

    def scan(self, df: pd.DataFrame, symbol: str,
             btc_ctx=None) -> List[DualSignal]:
        """
        한 코인에 대해 모멘텀 + 반전 시그널 모두 스캔.
        btc_ctx: BTCContext (None이면 BTC 필터 비활성)

        BTC 필터 동작:
          1. btc_ctx.alt_long_safe=False → 롱 시그널 차단
          2. btc_ctx.alt_short_safe=False → 숏 시그널 차단
          3. btc_ctx.alt_long_boost / alt_short_boost → 확신도 보정

        Returns: List[DualSignal]
        """
        signals = []
        conv_result = self.convergence_detector.detect(df)
        if conv_result:
            sig = convergence_to_dual_signal(conv_result, symbol)
            if sig and sig['confidence'] >= self.min_confidence:
                signals.append(DualSignal(**sig))
        # ── 전략 A: 모멘텀 스캔 ──────────────────────────────
        mom_result = self.momentum.detect(df)
        if mom_result:
            sig = self._build_momentum_signal(df, symbol, mom_result, btc_ctx)
            if sig and sig.confidence >= self.min_confidence:
                signals.append(sig)

        # ── 전략 B: 반전 스캔 ────────────────────────────────
        rev_result = self.reversal.detect(df)
        if rev_result:
            sig = self._build_reversal_signal(df, symbol, rev_result, btc_ctx)
            if sig and sig.confidence >= self.min_confidence:
                signals.append(sig)

        return signals

    def _build_momentum_signal(self, df: pd.DataFrame, symbol: str,
                                result: Dict, btc_ctx=None) -> Optional[DualSignal]:
        """모멘텀 시그널 구성 (BTC 보정 포함)"""
        d = result['details']
        atr = d['atr']
        price = d['close']
        direction = result['direction']
        reasons = result['reasons'].copy()
        trade_dir = "long" if direction == "pump" else "short"

        # ── BTC 방향 차단 ──────────────────────────────────
        if btc_ctx:
            long_safe = getattr(btc_ctx, 'long_safe', getattr(btc_ctx, 'alt_long_safe', True))
            if trade_dir == "long" and not long_safe:
                return None
            short_safe = getattr(btc_ctx, 'alt_short_safe', True)
            if trade_dir == "short" and not short_safe:
                return None

        # 온체인 보강
        oc_reasons, oc_score = [], 0
        if self.cg:
            oc_reasons, oc_score = OnchainBooster.boost_momentum(
                self.cg, symbol, direction)
            reasons.extend(oc_reasons)

        # 확신도: 기술 점수(8) + 온체인(4) → 12점 만점 → 100점 스케일
        confidence = min(100, int((result['score'] + oc_score) / 12 * 100))

        # ── BTC 확신도 보정 ────────────────────────────────
        if btc_ctx:
            long_boost = getattr(btc_ctx, 'long_boost', getattr(btc_ctx, 'alt_long_boost', 0))
            short_boost = getattr(btc_ctx, 'alt_short_boost', 0)
            boost = long_boost if trade_dir == "long" else short_boost
            if boost != 0:
                confidence = max(0, min(100, confidence + boost))
                adj_kr = "유리" if boost > 0 else "불리"
                reasons.append(
                    f"₿ BTC {btc_ctx.market_state} → {adj_kr} ({boost:+d})")

        if direction == "pump":
            sl = price - atr * 1.2
            tp = price + atr * 2.5
            sig_type = SignalType.MOMENTUM_LONG.value
        else:
            sl = price + atr * 1.2
            tp = price - atr * 2.5
            sig_type = SignalType.MOMENTUM_SHORT.value

        risk = abs(price - sl)
        reward = abs(tp - price)
        rr = reward / risk if risk > 0 else 0

        return DualSignal(
            signal_type=sig_type,
            symbol=symbol,
            direction=trade_dir,
            entry_price=round(price, 6),
            stop_loss=round(sl, 6),
            take_profit=round(tp, 6),
            trailing_atr_mult=0.8,
            confidence=confidence,
            risk_reward=round(rr, 2),
            reasons=reasons,
            strategy="momentum",
            atr=atr,
            atr_pct=d['atr_pct'],
            details=d,
            timestamp=datetime.now(KST).isoformat(),
        )

    def _build_reversal_signal(self, df: pd.DataFrame, symbol: str,
                                result: Dict, btc_ctx=None) -> Optional[DualSignal]:
        """반전 시그널 구성 (BTC 보정 포함)"""
        d = result['details']
        atr = d['atr']
        price = d['close']
        spike_dir = result['spike_direction']
        entry_dir = result['entry_direction']
        reasons = result['reasons'].copy()

        # ── BTC 방향 차단 ──────────────────────────────────
        if btc_ctx:
            long_safe = getattr(btc_ctx, 'long_safe', getattr(btc_ctx, 'alt_long_safe', True))
            if entry_dir == "long" and not long_safe:
                return None
            short_safe = getattr(btc_ctx, 'alt_short_safe', True)
            if entry_dir == "short" and not short_safe:
                return None
            # 추가: CRASH 상태에서 반전 롱은 항상 차단
            if entry_dir == "long" and btc_ctx.market_state == "CRASH":
                return None

        peak = result['peak_price']
        start = result['start_price']
        spike_range = abs(peak - start)

        # 온체인 보강
        oc_reasons, oc_score = [], 0
        if self.cg:
            oc_reasons, oc_score = OnchainBooster.boost_reversal(
                self.cg, symbol, spike_dir)
            reasons.extend(oc_reasons)

        # 확신도
        tech_total = result['exhaustion_score'] + result['reversal_score']
        confidence = min(100, int((tech_total + oc_score) / 13 * 100))

        # ── BTC 확신도 보정 ────────────────────────────────
        if btc_ctx:
            long_boost = getattr(btc_ctx, 'long_boost', getattr(btc_ctx, 'alt_long_boost', 0))
            short_boost = getattr(btc_ctx, 'alt_short_boost', 0)
            boost = long_boost if entry_dir == "long" else short_boost
            if boost != 0:
                confidence = max(0, min(100, confidence + boost))
                adj_kr = "유리" if boost > 0 else "불리"
                reasons.append(
                    f"₿ BTC {btc_ctx.market_state} → {adj_kr} ({boost:+d})")

        if entry_dir == "long":
            sl = peak - atr * 0.5
            tp = peak + spike_range * 0.50
            sig_type = SignalType.REVERSAL_LONG.value
        else:
            sl = peak + atr * 0.5
            tp = peak - spike_range * 0.50
            sig_type = SignalType.REVERSAL_SHORT.value

        risk = abs(price - sl)
        reward = abs(tp - price)
        rr = reward / risk if risk > 0 else 0

        return DualSignal(
            signal_type=sig_type,
            symbol=symbol,
            direction=entry_dir,
            entry_price=round(price, 6),
            stop_loss=round(sl, 6),
            take_profit=round(tp, 6),
            trailing_atr_mult=1.0,
            confidence=confidence,
            risk_reward=round(rr, 2),
            reasons=reasons,
            strategy="reversal",
            atr=atr,
            atr_pct=d['atr_pct'],
            details={**d, 'spike_move_atr': result['spike_move_atr'],
                     'spike_move_pct': result['spike_move_pct'],
                     'peak_price': peak},
            timestamp=datetime.now(KST).isoformat(),
        )

    # ── 텔레그램 포맷 ───────────────────────────────────────
    @staticmethod
    def format_signal(sig: DualSignal) -> str:
        """시그널을 텔레그램 메시지로 포맷"""
        strategy_kr = "🔥 모멘텀" if sig.strategy == "momentum" else "🔄 반전"
        dir_kr = "롱(매수)" if sig.direction == "long" else "숏(매도)"
        dir_emoji = "🟢" if sig.direction == "long" else "🔴"

        sl_pct = (sig.stop_loss / sig.entry_price - 1) * 100
        tp_pct = (sig.take_profit / sig.entry_price - 1) * 100

        lines = [
            f"⚡ {strategy_kr} 시그널 발생!",
            f"",
            f"{dir_emoji} {sig.symbol} → {dir_kr}",
            f"💪 확신도: {sig.confidence}/100",
            f"📐 RR: {sig.risk_reward}:1",
            f"",
            f"💰 진입가: {sig.entry_price}",
            f"🛑 손절: {sig.stop_loss} ({sl_pct:+.2f}%)",
            f"🎯 익절: {sig.take_profit} ({tp_pct:+.2f}%)",
            f"📏 ATR%: {sig.atr_pct:.1f}%",
            f"",
            f"📋 근거:",
        ]
        for r in sig.reasons[:6]:
            lines.append(f"  • {r}")

        if sig.strategy == "reversal" and 'spike_move_pct' in sig.details:
            lines.append(f"")
            spike_dir = "급등" if "short" in sig.signal_type else "급락"
            lines.append(f"⚡ 선행 스파이크: {spike_dir} "
                        f"{sig.details['spike_move_pct']:.1f}% "
                        f"({sig.details['spike_move_atr']:.1f} ATR)")

        return "\n".join(lines)


# ╔══════════════════════════════════════════════════════════════╗
# ║               백테스트용 과거 스캔                              ║
# ╚══════════════════════════════════════════════════════════════╝

class DualStrategyBacktester:
    """과거 데이터에서 모멘텀 + 반전 시그널을 수집하고 성과 분석"""

    def __init__(self, future_candles: int = 12):
        self.future_candles = future_candles
        self.momentum = MomentumDetector(min_score=4)
        self.reversal = ReversalDetector()

    def scan_historical(self, df: pd.DataFrame) -> pd.DataFrame:
        """과거 데이터에서 모든 시그널 수집 + 사후 결과 라벨링"""
        if df is None or len(df) < 100:
            return pd.DataFrame()

        records = []
        n = len(df)

        for i in range(60, n - self.future_candles):
            window = df.iloc[:i + 1]
            curr = window.iloc[-1]
            atr = curr.get('adx', 0)
            if atr <= 0:
                continue

            # 모멘텀 체크
            mom = self.momentum.detect(window)
            if mom:
                future = df.iloc[i+1 : i+1+self.future_candles]
                result = self._measure_outcome(
                    mom['direction'], curr['Close'], atr, future)
                records.append({
                    'idx': i,
                    'timestamp': curr.name if hasattr(curr, 'name') else i,
                    'strategy': 'momentum',
                    'direction': mom['direction'],
                    'trade_dir': 'long' if mom['direction'] == 'pump' else 'short',
                    'score': mom['score'],
                    'entry_price': curr['Close'],
                    'rsi': curr.get('rsi_14', 50),
                    'rvol': curr.get('rvol', 0),
                    'adx': curr.get('adx', 0),
                    'atr_pct': curr.get('atr_pct', 0),
                    **result,
                })

            # 반전 체크
            rev = self.reversal.detect(window)
            if rev:
                future = df.iloc[i+1 : i+1+self.future_candles]
                result = self._measure_outcome(
                    'pump' if rev['entry_direction'] == 'long' else 'dump',
                    curr['Close'], atr, future)
                records.append({
                    'idx': i,
                    'timestamp': curr.name if hasattr(curr, 'name') else i,
                    'strategy': 'reversal',
                    'direction': rev['spike_direction'],
                    'trade_dir': rev['entry_direction'],
                    'score': rev['total_score'],
                    'entry_price': curr['Close'],
                    'rsi': curr.get('rsi_14', 50),
                    'rvol': curr.get('rvol', 0),
                    'adx': curr.get('adx', 0),
                    'atr_pct': curr.get('atr_pct', 0),
                    **result,
                })

        result_df = pd.DataFrame(records)
        if not result_df.empty:
            self._print_summary(result_df)
        return result_df

    def _measure_outcome(self, trade_direction: str, entry: float,
                         atr: float, future: pd.DataFrame, entry_adx: float = 25) -> Dict:
        """최고점 갱신형(Chandelier Exit) + 동적 ATR 배수 백테스트 측정"""
        if future.empty:
            return {'max_profit_pct': 0, 'max_loss_pct': 0, 'final_pct': 0, 'win': 0}

        highest_price = entry
        lowest_price = entry
        exit_price = 0
        exit_idx = -1

        # ADX 기반 기본 배수 설정
        base_mult = 2.5 if entry_adx > 25 else 1.5

        for i, row in future.iterrows():
            curr_high = row['High']
            curr_low = row['Low']
            curr_close = row['Close']

            if trade_direction in ("pump", "long"):
                # 1. 최고점 갱신
                if curr_high > highest_price:
                    highest_price = curr_high
                
                # 2. 방어선(T-Stop) 계산 (최고점 기준)
                t_stop = highest_price - (atr * base_mult)

                # 3. 최저가가 방어선을 깼다면 (청산 발생)
                if curr_low <= t_stop:
                    # 갭하락 등을 고려하여 방어선 가격이나 종가 중 안 좋은 가격으로 체결 가정
                    exit_price = min(t_stop, curr_close)
                    exit_idx = i
                    break
            else: # Short의 경우 (참고용)
                if curr_low < lowest_price:
                    lowest_price = curr_low
                t_stop = lowest_price + (atr * base_mult)
                if curr_high >= t_stop:
                    exit_price = max(t_stop, curr_close)
                    exit_idx = i
                    break

        # 청산되지 않고 끝까지 갔다면 마지막 종가로 계산
        if exit_price == 0:
            exit_price = future.iloc[-1]['Close']

        # 수익률 계산 (수수료 0.08% * 2회 = 0.16% 차감)
        if trade_direction in ("pump", "long"):
            final_pnl = ((exit_price - entry) / entry * 100) - 0.16
        else:
            final_pnl = ((entry - exit_price) / entry * 100) - 0.16

        win = 1 if final_pnl > 0 else 0

        return {
            'max_profit_pct': round((highest_price - entry) / entry * 100, 3) if trade_direction in ("pump", "long") else 0,
            'max_loss_pct': 0, # 단순화를 위해 제외
            'final_pct': round(final_pnl, 3),
            'win': win,
        }

    def _print_summary(self, df: pd.DataFrame):
        """백테스트 결과 요약"""
        print(f"\n{'='*60}")
        print(f"  📊 양방향 백테스트 결과 ({len(df)}건)")
        print(f"{'='*60}")

        for strategy in ['momentum', 'reversal']:
            sub = df[df['strategy'] == strategy]
            if sub.empty:
                continue
            wins = sub['win'].sum()
            total = len(sub)
            avg_profit = sub['max_profit_pct'].mean()
            avg_loss = sub['max_loss_pct'].mean()
            print(f"\n  [{strategy.upper()}]")
            print(f"    시그널: {total}건 | 승률: {wins/total*100:.1f}%")
            print(f"    평균 최대수익: {avg_profit:.2f}% | 평균 최대손실: {avg_loss:.2f}%")
            print(f"    평균 최종PnL: {sub['final_pct'].mean():.2f}%")

            for d in ['long', 'short']:
                ds = sub[sub['trade_dir'] == d]
                if not ds.empty:
                    dw = ds['win'].sum()
                    print(f"      {d}: {len(ds)}건, 승률 {dw/len(ds)*100:.1f}%")
