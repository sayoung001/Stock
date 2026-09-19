"""
semi_auto_trader.py — EMA 12/26 반자동매매 봇 v8.2
═══════════════════════════════════════════════════════════════
자동매매(auto_trader_v9.py)와 별도 계좌에서 운용.

v8.2 변경:
  [8.2] 동적 워치리스트: 바이낸스 전체 선물에서 $0.1M~$10M 코인 자동 스캔
        v8/v9 코인 제외 → 소형 급등 코인 전용
        15분마다 ticker 갱신 → 거래량 변동 반영
  [8.1-4] v2 필터: ADX≥27, VR≤3.0, LONG RSI≤75
  [8.1-2] 텔레그램: RSI 방향 + 수렴 강도 + surge + 일거래량
  [8.1-3] MAX_POSITIONS 5

핵심:
  - 시그널 발생 시 25% 자동 진입 + SL 자동 설정
  - 텔레그램 알림으로 75% 추가 진입 판단 요청
  - EE 조건 도달 시 알림만 (자동 청산 X)
  - Trail/SL/TP는 자동 관리
  - BTC 약추세 차단 항상 ON

사용법:
  python semi_auto_trader.py              # 페이퍼
  python semi_auto_trader.py --live       # 실전
"""

import ccxt, pandas as pd, numpy as np, time, json, os, sys, requests, traceback, math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from convergence_strategy import ConvergenceBreakout, CONVERGENCE_CONFIG
try:
    from coinglass_client import CoinGlassClient
except ImportError:
    CoinGlassClient = None
try:
    from agents.v9_hook import AgentHook
except ImportError:
    AgentHook = None
from dotenv import load_dotenv
load_dotenv()

KST = timezone(timedelta(hours=9))
BINANCE_FAPI = "https://fapi.binance.com"

# ═══════════════════════════════════════════════════════════════
#  설정 (반자동 전용)
# ═══════════════════════════════════════════════════════════════

CONFIG = {
    "PAPER_TRADE": True,

    # API Keys
    "BINANCE_API_KEY":  os.environ.get("BINANCE_API_KEY_SEMI",
                        os.environ.get("BINANCE_API_KEY", "")),
    "BINANCE_SECRET":   os.environ.get("BINANCE_SECRET_SEMI",
                        os.environ.get("BINANCE_SECRET", "")),
    "COINGLASS_API_KEY": os.environ.get("COINGLASS_API_KEY", ""),
    "TELEGRAM_TOKEN":   os.environ.get("TELEGRAM_TOKEN_SEMI",
                        os.environ.get("TELEGRAM_TOKEN_TRADER", "")),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID_SEMI",
                        os.environ.get("TELEGRAM_CHAT_ID", "")),
    # Watchdog 텔레그램 (에이전트 정기분석 + 급락 알림)
    "TELEGRAM_TOKEN_WATCHDOG": os.environ.get("TELEGRAM_TOKEN_WATCHDOG", ""),
    "TELEGRAM_CHAT_ID_WATCHDOG": os.environ.get("TELEGRAM_CHAT_ID_WATCHDOG", ""),

    # [8.2] 동적 워치리스트 — $0.1M~$10M 소형 코인 전체 스캔
    "WATCHLIST_MODE": "dynamic",       # "dynamic" or "fixed"
    "VOL_MIN_M": 0.1,                  # 최소 일거래량 $M
    "VOL_MAX_M": 10.0,                 # 최대 일거래량 $M
    "WATCHLIST_REFRESH_SEC": 14400,    # 워치리스트 갱신 (4시간)
    "WATCHLIST_FIXED": [               # fallback: dynamic 실패 시 사용
        "ETH/USDT", "SOL/USDT", "XRP/USDT", "BCH/USDT",
        "NEAR/USDT", "COMP/USDT", "UNI/USDT",
        "AVAX/USDT", "ADA/USDT", "DOT/USDT", "LTC/USDT",
        "FIL/USDT", "OP/USDT", "TAO/USDT",
    ],

    "LEVERAGE": 20,
    "FEE_RATE": 0.0004,

    # 시그널 필터 [8.1-4] v2 강화
    "EMA_GAP_THRESHOLD": 0.7,
    "MIN_SQUEEZE_CANDLES": 15,
    "MIN_CONFIDENCE": 70,
    "MIN_ADX": 27,                 # v2: 25→27 (ADX 25~27 PF0.64 제거)
    "MIN_VOLUME_RATIO": 1.5,
    "MAX_VOLUME_RATIO": 3.0,       # ★ v2: 과열 돌파 차단 (PF 0.99→1.56)
    "LONG_RSI_MAX": 75,            # ★ v2: LONG RSI>75 차단 (PF 0.36)
    "FIBO_LEVEL": 2.618,
    "SL_RATIO": 1.5,

    # CasTrail 전략
    "STRATEGY": "CasTrail",
    "LOCK_FIBO": 2.0,
    "TRAIL_PCT": 50,

    # EE: 알림만 (자동 청산 X)
    "ENABLE_EARLY_EXIT": False,    # EE 자동 청산 비활성
    "EE_ALERT": True,              # EE 조건 시 알림만
    "EE_START_CANDLE": 8,
    "EE_END_CANDLE": 16,
    "EE_THRESHOLD_ROE": -10,

    "TIMEOUT_H": 192,

    # BTC 필터 (항상 ON)
    "BTC_FILTER_ENABLED": True,
    "BTC_WEAK_FILTER": True,
    "BTC_WEAK_STATES": [0, 1, 3],

    # 반자동 설정
    "AUTO_ENTRY_PCT": 0.25,     # 25% 자동 진입
    "FULL_RISK_PCT": 15.0,     # 풀 포지션 리스크
    "SIZING": "compound",
    "MAX_POSITIONS": 5,
    "MARGIN_EXPOSURE_LIMIT": 0.50,

    "MAX_DAILY_TRADES": 5,
    "SCAN_SEC": 60,          # [8.1-5] 180→60 (별도 계좌, 빈번 스캔)
    "MONITOR_SEC": 15,       # [8.1-5] 30→15 (SL/Trail 빠른 감지)
    "STATUS_SEC": 7200,
    "CG_CACHE_SEC": 300,

    "LOG_DIR": "./trade_logs_semi",
    "POSITIONS_FILE": "./trade_logs_semi/positions_semi.json",
}

# [8.2] v9/v8 코인 제외 (별도 계좌에서 이미 운용 중)
EXCLUDE_COINS = {
    "SOL", "XRP", "NEAR", "UNI", "COMP", "SAND", "MANA",
    "QNT", "ANKR", "SUI", "HBAR", "KAS", "NTRN", "WAXP", "TRX",
    "ENJ", "WLD", "TRUMP", "1000PEPE", "MORPHO", "SEI", "GALA",
    "ETH", "BCH", "AVAX", "ADA", "DOT", "DOGE", "LTC", "LINK",
    "FIL", "APT", "ATOM", "XLM", "VET", "ALGO", "OP", "ARB",
    "FTM", "TAO", "BTC",
}


# ═══════════════════════════════════════════════════════════════
#  [8.1-2] Surge 프로필 (surge_scan Phase 2~3 결과)
#  3개월 1h 데이터 기반 — 수렴/급등 특성 요약
# ═══════════════════════════════════════════════════════════════

SURGE_PROFILE = {
    # coin: (총 이벤트, ↑10%, ↑25%, ↓10%, ↓25%, 수렴%, 강수렴%, catch%, 15m수렴%, 일거래량$M)
    "ETH":  (5,  2, 0, 3, 0, 40.0, 40.0, 40.0, 89.0, 11040),
    "SOL":  (7,  4, 0, 3, 0, 42.9, 28.6, 28.6, 90.6, 60),
    "XRP":  (7,  3, 1, 3, 0, 42.9, 14.3, 14.3, 85.0, 150),
    "BCH":  (5,  2, 0, 3, 0, 80.0, 60.0, 60.0, 92.5, 158),
    "NEAR": (11, 5, 1, 5, 0, 63.6, 36.4, 63.6, 87.2, 50),
    "COMP": (6,  2, 1, 3, 0, 66.7, 33.3, 50.0, 82.0, 15),
    "UNI":  (7,  3, 0, 4, 0, 71.4, 28.6, 71.4, 80.0, 30),
    "AVAX": (6,  3, 0, 3, 0, 83.3, 50.0, 66.7, 91.0, 155),
    "ADA":  (7,  3, 0, 4, 0, 85.7, 57.1, 71.4, 88.6, 215),
    "DOT":  (9,  4, 1, 4, 0, 77.8, 33.3, 77.8, 88.3, 89),
    "LTC":  (3,  2, 0, 1, 0, 100., 33.3, 66.7, 85.0, 98),
    "FIL":  (11, 5, 0, 6, 0, 81.8, 36.4, 72.7, 87.2, 113),
    "OP":   (12, 5, 0, 6, 1, 75.0, 50.0, 58.3, 85.6, 42),
    "TAO":  (13, 5, 1, 7, 0, 84.6, 30.8, 76.9, 81.3, 105),
}

# RSI 방향 판정 기준 (Phase 2 데이터: RSI<30→96.8% UP, RSI>70→93% DOWN)
RSI_DIRECTION = {
    "strong_long":  (0,  30, "🟢 강한 롱 신호 (RSI<30→96.8% 급등)"),
    "mild_long":    (30, 40, "🟡 롱 우세 (RSI 30~40→81% 급등)"),
    "neutral_long": (40, 50, "⚪ 약한 롱 (RSI 40~50→58% 급등)"),
    "neutral_short":(50, 60, "⚪ 약한 숏 (RSI 50~60→62% 급락)"),
    "mild_short":   (60, 70, "🟡 숏 우세 (RSI 60~70→75% 급락)"),
    "strong_short": (70,100, "🔴 강한 숏 신호 (RSI>70→93% 급락)"),
}


def get_rsi_direction(rsi, direction):
    """RSI 기반 방향 판정 + 일치 여부"""
    rsi_label = ""
    rsi_aligned = False
    for name, (lo, hi, desc) in RSI_DIRECTION.items():
        if lo <= rsi < hi:
            rsi_label = desc
            if direction == "long" and "롱" in name:
                rsi_aligned = True
            elif direction == "short" and "숏" in name:
                rsi_aligned = True
            break
    return rsi_label, rsi_aligned


def get_surge_info(symbol):
    """코인의 surge 프로필 요약 문자열"""
    coin = symbol.replace("/USDT", "").replace("USDT", "")
    p = SURGE_PROFILE.get(coin)
    if not p:
        return "surge 데이터 없음", ""
    total, u10, u25, d10, d25, sq, tsq, catch, sq15, vol = p
    summary = (f"3개월: {total}건 (↑{u10}+{u25} ↓{d10}+{d25})")
    detail = (f"수렴{sq:.0f}%(강{tsq:.0f}%) | catch {catch:.0f}% | "
              f"15m수렴{sq15:.0f}% | VOL ${vol:.0f}M")
    return summary, detail


# ═══════════════════════════════════════════════════════════════
#  텔레그램 (한글 알람)
# ═══════════════════════════════════════════════════════════════

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token; self.chat_id = chat_id
        self.ok = bool(token and chat_id)

    @staticmethod
    def _escape_html(text):
        """HTML 특수문자 이스케이프 — 에러 메시지에 <>&가 있어도 안전"""
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def send(self, msg):
        if not self.ok:
            print(f"  [TG] {msg[:200]}"); return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000], "parse_mode": "HTML"},
                timeout=10)
        except Exception as e:
            # HTML 파싱 실패 시 plain text로 재시도
            try:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                    data={"chat_id": self.chat_id, "text": msg[:4000]},
                    timeout=10)
            except:
                print(f"  TG err: {e}")

    def send_error(self, msg):
        """오류 알람 — HTML 이스케이프 적용"""
        safe_msg = self._escape_html(msg)
        self.send(f"🔴 오류 발생\n{safe_msg}")


# ═══════════════════════════════════════════════════════════════
#  온체인 데이터 (CoinGlass + Binance fallback)
# ═══════════════════════════════════════════════════════════════

class OnchainFetcher:
    def __init__(self, api_key='', cache_sec=300):
        self._cg = None
        if api_key:
            try:
                from coinglass_client import CoinGlassClient
                self._cg = CoinGlassClient(api_key, rate_limit=70)
                print("  CoinGlass: 연결됨")
            except Exception as e:
                print(f"  CoinGlass: {e}")
        self._cache = {}; self._cache_time = {}; self._ttl = cache_sec

    def get_all(self, symbol):
        now = time.time()
        clean = symbol.replace('/USDT','').replace('USDT','').replace('1000','')
        if clean in self._cache and now - self._cache_time.get(clean, 0) < self._ttl:
            return self._cache[clean]
        result = {'funding_rate': 0, 'oi_chg_1h': 0, 'taker_buy_ratio': 0.5}
        if self._cg:
            try:
                oi = self._cg.get_oi_change(clean, "1h")
                if oi: result['oi_chg_1h'] = oi.get('change_pct', 0)
                fr = self._cg.get_funding_rate_trend(clean)
                if fr: result['funding_rate'] = fr.get('current', 0)
                tk = self._cg.get_taker_pressure(clean)
                if tk: result['taker_buy_ratio'] = round(1/(1+tk.get('sell_buy_ratio',1)),4)
            except Exception as e:
                print(f"  CG {clean}: {e}")
        if result['funding_rate'] == 0:
            try:
                r = requests.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
                                 params={"symbol": symbol.replace('/','')}, timeout=5)
                result['funding_rate'] = float(r.json().get('lastFundingRate', 0))
            except: pass
        self._cache[clean] = result; self._cache_time[clean] = now
        return result


# ═══════════════════════════════════════════════════════════════
#  BTC 트렌드 필터 (백테스트 동일 로직)
#  ── mdd_improvement.py의 precompute_btc_trend()와 100% 동일 ──
#  EMA20/EMA60 + ADX > 20 → BULL(1) / BEAR(-1) / NEUTRAL(0)
# ═══════════════════════════════════════════════════════════════

class BTCTrend7Level:
    """
    [V7-2] BTC 7단계 트렌드 판정 (백테스트 동일).
     3: STRONG_BULL (EMA20>60, ADX>30, RSI>60, 1h>+1%)
     2: BULL        (EMA20>60, ADX>20)
     1: MILD_BULL   (EMA20>60, ADX≤20)
     0: NEUTRAL
    -1: MILD_BEAR   (EMA20<60, ADX≤20)
    -2: BEAR        (EMA20<60, ADX>20)
    -3: CRASH       (EMA20<60, ADX>30, RSI<40, 1h<-1%)

    7Lv_Aggressive 필터:
      long 차단: BEAR(-2), CRASH(-3)
      short 차단: STRONG_BULL(3) 만
      → BULL에서 short 허용!
    """
    NAMES = {3:'STRONG_BULL',2:'BULL',1:'MILD_BULL',0:'NEUTRAL',
             -1:'MILD_BEAR',-2:'BEAR',-3:'CRASH'}
    # 7Lv_Aggressive
    LONG_BLOCK = {-2, -3}   # BEAR, CRASH
    SHORT_BLOCK = {3}        # STRONG_BULL만

    def __init__(self, exchange, cache_sec=900):
        self.exchange = exchange
        self._cache_sec = cache_sec
        self._trend = 0
        self._last_update = 0
        self._ema_s = 0; self._ema_l = 0; self._adx = 0; self._rsi = 50

    def update(self):
        now = time.time()
        if now - self._last_update < self._cache_sec:
            return self._trend
        try:
            ohlcv = self.exchange.fetch_ohlcv('BTC/USDT', '15m', limit=200)
            df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
            c = df['Close'].values.astype(float)
            h = df['High'].values.astype(float)
            lo = df['Low'].values.astype(float)

            ema_s = pd.Series(c).ewm(span=12).mean().values
            ema_l = pd.Series(c).ewm(span=26).mean().values

            prev_c = np.roll(c, 1); prev_c[0] = c[0]
            tr = np.maximum(h-lo, np.maximum(np.abs(h-prev_c), np.abs(lo-prev_c)))
            tr[0] = h[0]-lo[0]
            atr = pd.Series(tr).rolling(14).mean().values
            plus_dm = np.diff(h, prepend=h[0])
            minus_dm = -np.diff(lo, prepend=lo[0])
            plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
            minus_dm_v = np.where((minus_dm > np.diff(h, prepend=h[0])) & (minus_dm > 0), minus_dm, 0)
            sa = pd.Series(atr).rolling(14).mean().values + 1e-9
            pdi = 100 * pd.Series(plus_dm).rolling(14).mean().values / sa
            mdi = 100 * pd.Series(minus_dm_v).rolling(14).mean().values / sa
            dx = 100 * np.abs(pdi-mdi) / (pdi+mdi+1e-9)
            adx = pd.Series(dx).rolling(14).mean().values

            # RSI
            delta = np.diff(c, prepend=c[0])
            gain = pd.Series(np.where(delta>0, delta, 0)).rolling(14).mean().values
            loss = pd.Series(np.where(delta<0, -delta, 0)).rolling(14).mean().values + 1e-9
            rsi = 100 - (100 / (1 + gain / loss))

            # 1h 변동률 (4캔들)
            idx = -2  # 마지막 완성 봉
            chg_1h = (c[idx] - c[idx-4]) / c[idx-4] * 100 if idx-4 >= 0 else 0

            self._ema_s = ema_s[idx]; self._ema_l = ema_l[idx]
            self._adx = adx[idx] if not np.isnan(adx[idx]) else 0
            self._rsi = rsi[idx] if not np.isnan(rsi[idx]) else 50

            es, el, av, rs = self._ema_s, self._ema_l, self._adx, self._rsi
            if es > el:
                if av > 30 and rs > 60 and chg_1h > 1.0: self._trend = 3
                elif av > 20: self._trend = 2
                else: self._trend = 1
            elif es < el:
                if av > 30 and rs < 40 and chg_1h < -1.0: self._trend = -3
                elif av > 20: self._trend = -2
                else: self._trend = -1
            else:
                self._trend = 0

            self._last_update = now
        except Exception as e:
            print(f"  BTC 7Lv 업데이트 실패: {e}")
            self._last_update = now - self._cache_sec + 30
        return self._trend

    @property
    def trend(self): return self._trend

    @property
    def trend_name(self): return self.NAMES.get(self._trend, 'UNKNOWN')

    @property
    def long_blocked(self): return self._trend in self.LONG_BLOCK

    @property
    def short_blocked(self): return self._trend in self.SHORT_BLOCK

    def status_str(self):
        return (f"BTC7 {self.trend_name}({self._trend:+d}) "
                f"EMA12:{self._ema_s:.0f} EMA26:{self._ema_l:.0f} "
                f"ADX:{self._adx:.1f} RSI:{self._rsi:.0f}")


# ═══════════════════════════════════════════════════════════════
#  Position 데이터클래스
# ═══════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str; direction: str; entry_price: float; quantity: float
    stop_loss: float; take_profit: float; atr: float; entry_time: str
    confidence: int; reasons: List[str]; squeeze_candles: int = 0
    max_fav_roe: float = 0; max_adv_roe: float = 0
    roe_history: Dict = field(default_factory=dict)

    # [V6-1] 캐스케이드 상태 (SL 잠금 방식)
    sl_locked: bool = False          # fibo 3.236 도달 시 SL 본전 잠금 완료?
    original_sl: float = 0           # 원본 SL (잠금 전)
    lock_trigger_roe: float = 0      # fibo 3.236의 ROE 값 (동적 계산)

    # EE 체크 상태
    ee_checked: bool = False         # 6~8h 구간에서 EE 체크 완료?

    # 거래소 주문 ID
    sl_order_id: str = ""
    tp_order_id: str = ""

    # 시그널 메타
    funding_rate: float = 0
    adx_value: float = 0; volume_ratio: float = 0; rsi: float = 50
    gap_pct: float = 0              # [8.1-5] 진입 시 EMA gap
    entry_btc_state: str = "N/A"
    profile: str = "standard"         # [V8] 프로필

    def hold_h(self):
        try: return (datetime.now(KST) - datetime.fromisoformat(self.entry_time)).total_seconds() / 3600
        except: return 0

    def hold_candles_15m(self):
        return int(self.hold_h() * 4)

    def current_roe(self, price):
        """현물 수익률% (레버리지 미포함)"""
        if self.direction == 'long': return (price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - price) / self.entry_price * 100

    def current_roe_lev(self, price, leverage=20):
        """레버리지 포함 ROE%"""
        return self.current_roe(price) * leverage

    def pnl(self, price):
        if self.direction == 'long': return (price - self.entry_price) * self.quantity
        return (self.entry_price - price) * self.quantity

    def update_extremes(self, price):
        roe = self.current_roe(price)
        self.max_fav_roe = max(self.max_fav_roe, max(0, roe))
        self.max_adv_roe = max(self.max_adv_roe, max(0, -roe))


# ═══════════════════════════════════════════════════════════════
#  Executor (거래 실행)
# ═══════════════════════════════════════════════════════════════

class Executor:
    def __init__(self, exchange, cfg, tg=None):
        self.ex = exchange; self.cfg = cfg; self.paper_bal = 10000.0
        self._mkt = {}; self.tg = tg

    def _alert(self, msg):
        print(f"  {msg}")
        if self.tg: self.tg.send_error(msg)

    def setup_hedge_mode(self):
        if self.cfg["PAPER_TRADE"]: return
        try:
            self.ex.fapiPrivatePostPositionSideDual({"dualSidePosition": "true"})
            print("  헤지모드: 설정 완료")
        except Exception as e:
            es = str(e).upper()
            # 이미 헤지모드 / 포지션·주문 있어서 변경 불가 → 정상
            if any(k in es for k in ['NO_NEED', 'ALREADY', 'CANNOT BE CHANGED',
                                      'OPEN ORDERS', 'POSITION SIDE']):
                print("  헤지모드: 이미 설정됨 (정상)")
            else:
                self._alert(f"헤지모드 설정 실패: {e}")

    def _prec(self, sym):
        if sym in self._mkt: return self._mkt[sym]
        try:
            m = self.ex.load_markets().get(sym, {})
            info = {'qp': m.get('precision', {}).get('amount', 3),
                    'pp': m.get('precision', {}).get('price', 6),
                    'mq': m.get('limits', {}).get('amount', {}).get('min', 0.001),
                    'mn': m.get('limits', {}).get('cost', {}).get('min', 5)}
            self._mkt[sym] = info; return info
        except: return {'qp': 3, 'pp': 6, 'mq': 0.001, 'mn': 5}

    def rnd_qty(self, sym, qty):
        p = self._prec(sym)['qp']
        if isinstance(p, int): return max(math.floor(qty * 10**p) / 10**p, self._prec(sym)['mq'])
        return max(math.floor(qty / p) * p, self._prec(sym)['mq'])

    def rnd_price(self, sym, price):
        p = self._prec(sym)['pp']
        if isinstance(p, int): return round(price, p)
        return round(price / p) * p

    def balance(self):
        if self.cfg["PAPER_TRADE"]: return self.paper_bal
        try: return float(self.ex.fetch_balance({"type": "future"})["USDT"]["free"])
        except: return 0.0

    def total_balance(self):
        """마진 포함 총 자산"""
        if self.cfg["PAPER_TRADE"]: return self.paper_bal
        try:
            b = self.ex.fetch_balance({"type": "future"})
            return float(b["USDT"]["total"])
        except: return self.balance()

    def price(self, sym):
        try: return float(self.ex.fetch_ticker(sym)["last"])
        except: return 0.0

    def open_position(self, sym, direction, qty, entry, sl, tp):
        """진입: 마켓 주문 + SL/TP 설정"""
        qty = self.rnd_qty(sym, qty)
        sl = self.rnd_price(sym, sl); tp = self.rnd_price(sym, tp)
        if qty * entry / self.cfg["LEVERAGE"] < self._prec(sym).get('mn', 5):
            self._alert(f"{sym} 최소 주문금액 미달"); return None
        fee = qty * entry * self.cfg["FEE_RATE"]
        mg = qty * entry / self.cfg["LEVERAGE"]

        if self.cfg["PAPER_TRADE"]:
            self.paper_bal -= (mg + fee)
            return {"price": entry, "qty": qty, "mg": mg, "fee": fee,
                    "sl_order_id": "paper_sl", "tp_order_id": "paper_tp"}
        try:
            self.ex.set_leverage(self.cfg["LEVERAGE"], sym)
            ps = "LONG" if direction == "long" else "SHORT"
            side = "buy" if direction == "long" else "sell"
            o = self.ex.create_order(sym, "market", side, qty, params={"positionSide": ps})
            ap = float(o.get("average", entry))

            # 슬리피지 검증
            slippage = abs(ap - entry) / entry * 100
            if slippage > 1.0:
                self._alert(f"슬리피지 경고 {sym}: {slippage:.2f}%")
            # 체결가 기준 SL/TP 보정
            if abs(ap - entry) / entry > 0.001:
                diff = ap - entry
                sl = self.rnd_price(sym, sl + diff)
                tp = self.rnd_price(sym, tp + diff)

            ids = self._place_sl_tp(sym, direction, qty, sl, tp, ps)
            return {"price": ap, "qty": qty, "mg": mg, "fee": fee,
                    "sl_order_id": ids.get('sl',''), "tp_order_id": ids.get('tp','')}
        except Exception as e:
            self._alert(f"진입 실패 {sym}: {e}"); return None

    def _place_sl_tp(self, sym, direction, qty, sl, tp, ps, retries=3):
        """SL + TP 조건부 주문 설정 (3회 재시도)"""
        sl_side = "sell" if direction == "long" else "buy"
        ids = {'sl': '', 'tp': ''}
        self._cancel_all_orders(sym)  # 기존 잔여 주문 정리

        for i in range(retries):
            try:
                if not ids['sl']:
                    o = self.ex.create_order(sym, "STOP_MARKET", sl_side, qty,
                        params={"stopPrice": sl, "positionSide": ps,
                                "closePosition": True, "priceProtect": "TRUE"})
                    ids['sl'] = o.get('id', 'ok')
                if not ids['tp']:
                    o = self.ex.create_order(sym, "TAKE_PROFIT_MARKET", sl_side, qty,
                        params={"stopPrice": tp, "positionSide": ps,
                                "closePosition": True, "priceProtect": "TRUE"})
                    ids['tp'] = o.get('id', 'ok')
                break
            except Exception as e:
                self._alert(f"SL/TP 설정 재시도 {i+1}/3 {sym}: {e}")
                time.sleep(1)

        if not ids['sl']:
            self._alert(f"SL 설정 완전 실패! {sym} — 수동 확인 필요")
        if not ids['tp']:
            self._alert(f"TP 설정 완전 실패! {sym} — 수동 확인 필요")

        # 설정 후 검증
        if ids['sl'] or ids['tp']:
            time.sleep(0.5)
            check = self.verify_sl_tp(sym, direction)
            if check['sl'] is not None:
                if ids['sl'] and not check['sl']:
                    self._alert(f"SL 설정 후 미감지! {sym} — 재시도")
                    try:
                        o = self.ex.create_order(sym, "STOP_MARKET", sl_side, qty,
                            params={"stopPrice": sl, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        ids['sl'] = o.get('id', 'retry_ok')
                    except: pass
                if ids['tp'] and not check['tp']:
                    self._alert(f"TP 설정 후 미감지! {sym} — 재시도")
                    try:
                        o = self.ex.create_order(sym, "TAKE_PROFIT_MARKET", sl_side, qty,
                            params={"stopPrice": tp, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        ids['tp'] = o.get('id', 'retry_ok')
                    except: pass
        return ids

    def update_sl_order(self, sym, direction, qty, new_sl, pos):
        """
        [V6-1] SL 본전 잠금 — 안전 순서:
        1. 새 SL 생성 (quantity+reduceOnly, closePosition 안 씀)
        2. 기존 SL 취소
        → 1~2 사이에 항상 최소 1개 SL이 활성!
        → TP는 절대 건드리지 않음
        """
        if self.cfg["PAPER_TRADE"]:
            pos.sl_order_id = "paper_sl_locked"
            return True

        ps = "LONG" if direction == "long" else "SHORT"
        sl_side = "sell" if direction == "long" else "buy"
        new_sl = self.rnd_price(sym, new_sl)
        rounded_qty = self.rnd_qty(sym, qty)

        for attempt in range(2):
            try:
                # ── 방법 A: quantity+reduceOnly (기존 주문과 공존 가능) ──
                # 새 SL 먼저 생성
                o_sl = self.ex.create_order(sym, "STOP_MARKET", sl_side, rounded_qty,
                    params={"stopPrice": new_sl, "positionSide": ps,
                            "reduceOnly": "TRUE", "priceProtect": "TRUE"})
                new_sl_id = o_sl.get('id', '')
                print(f"  {sym}: 새 SL(본전) 생성 완료 id={new_sl_id}")

                # 기존 SL(STOP_MARKET) 주문만 찾아서 취소 (TP는 안건드림!)
                orders = self._get_open_orders_raw(sym)
                if orders:
                    for oo in orders:
                        oid = str(oo.get('orderId', oo.get('id', '')))
                        otype = str(oo.get('type', oo.get('origType', ''))).upper()
                        if ('STOP' in otype and 'PROFIT' not in otype
                                and oid != str(new_sl_id)):
                            try:
                                self.ex.cancel_order(oid, sym)
                                print(f"  {sym}: 기존 SL 취소 id={oid}")
                            except: pass

                pos.sl_order_id = new_sl_id
                return True

            except Exception as e:
                err_s = str(e)
                print(f"  SL잠금 방법A 시도{attempt+1} {sym}: {err_s}")

                # ── 방법 B: 전체 취소 후 재생성 (fallback) ──
                if attempt == 0:
                    try:
                        self._cancel_all_orders(sym)
                        time.sleep(0.8)
                        tp_price = self.rnd_price(sym, pos.take_profit)
                        # SL 재생성
                        o_sl = self.ex.create_order(sym, "STOP_MARKET", sl_side, rounded_qty,
                            params={"stopPrice": new_sl, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        pos.sl_order_id = o_sl.get('id', 'ok')
                        # TP 재생성
                        o_tp = self.ex.create_order(sym, "TAKE_PROFIT_MARKET", sl_side, rounded_qty,
                            params={"stopPrice": tp_price, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        pos.tp_order_id = o_tp.get('id', 'ok')
                        return True
                    except Exception as e2:
                        print(f"  SL잠금 방법B {sym}: {e2}")

        return False  # 2가지 방법 모두 실패

    def close_position(self, sym, direction, qty, price, reason):
        """시장가 청산 + 실제 체결 검증 + 잔여 주문 정리"""
        fee = qty * price * self.cfg["FEE_RATE"]
        if self.cfg["PAPER_TRADE"]:
            return {"price": price, "fee": fee}

        ps = "LONG" if direction == "long" else "SHORT"
        side = "sell" if direction == "long" else "buy"

        try:
            o = self.ex.create_order(sym, "market", side, qty,
                params={"positionSide": ps, "reduceOnly": True})
            fill_price = float(o.get("average", price))
            fill_qty = float(o.get("filled", qty))
            print(f"  {sym} 청산 체결: price={fill_price} qty={fill_qty}")
            self._cancel_all_orders(sym)
            return {"price": fill_price, "fee": fee}

        except Exception as e:
            err = str(e).lower()
            # reduceOnly 에러 = 포지션이 이미 없음 (거래소 SL/TP가 먼저 체결)
            if 'reduceonly' in err or 'insufficient' in err:
                # 실제로 포지션이 없는지 거래소에서 검증
                if self._verify_position_closed(sym, direction):
                    print(f"  {sym} 거래소에서 이미 청산 확인됨")
                    self._cancel_all_orders(sym)
                    return {"price": price, "fee": 0}
                else:
                    # 포지션이 아직 있는데 reduceOnly 에러?
                    # → 수량 불일치 등 다른 문제
                    self._alert(
                        f"청산 실패 {sym} ({reason}): {e}\n"
                        f"포지션이 아직 존재함! 수동 청산 필요!")
                    return None  # ← None 반환 → _close에서 제거 안 함
            else:
                self._alert(f"청산 실패 {sym} ({reason}): {e}")
                return None

    def _verify_position_closed(self, sym, direction):
        """거래소에서 해당 포지션이 실제로 없는지 확인"""
        try:
            positions = self.ex.fetch_positions([sym])
            for p in positions:
                p_side = p.get('side', '').lower()
                contracts = abs(float(p.get('contracts', 0)))
                if p_side == direction and contracts > 0:
                    return False  # 아직 포지션 있음!
            return True  # 포지션 없음 = 청산 확인
        except:
            return False  # 확인 불가 → 안전하게 "있음"으로 처리

    def _cancel_all_orders(self, sym):
        """해당 심볼의 모든 미체결 주문 취소 — 3가지 방법 순차 시도"""
        raw_sym = sym.replace('/', '')
        # 방법 1: 바이낸스 직접 API (가장 확실)
        try:
            self.ex.fapiPrivateDeleteAllOpenOrders({"symbol": raw_sym})
            print(f"  {sym}: 전체 주문 취소 (API 직접)")
            return True
        except Exception as e1:
            print(f"  {sym}: 직접 API 취소 실패: {e1}")
        # 방법 2: ccxt cancel_all_orders
        try:
            self.ex.cancel_all_orders(sym)
            print(f"  {sym}: 전체 주문 취소 (ccxt)")
            return True
        except Exception as e2:
            print(f"  {sym}: ccxt 취소 실패: {e2}")
        # 방법 3: 개별 취소
        try:
            orders = self.ex.fetch_open_orders(sym)
            for oo in orders:
                try: self.ex.cancel_order(oo['id'], sym)
                except: pass
            print(f"  {sym}: {len(orders)}개 개별 취소")
            return len(orders) > 0
        except:
            return False

    def _get_open_orders_raw(self, sym):
        """바이낸스에서 미체결 주문 목록 조회 — 여러 방법 시도"""
        raw_sym = sym.replace('/', '')
        # 방법 1: 바이낸스 직접 API
        try:
            orders = self.ex.fapiPrivateGetOpenOrders({"symbol": raw_sym})
            if isinstance(orders, list):
                return orders
        except: pass
        # 방법 2: ccxt
        try:
            orders = self.ex.fetch_open_orders(sym)
            return [oo.get('info', oo) for oo in orders]
        except: pass
        return None  # 조회 자체 실패

    def verify_sl_tp(self, sym, direction=None):
        """SL/TP 주문 존재 여부 검증 — type+origType 이중 확인"""
        if self.cfg["PAPER_TRADE"]: return {'sl': True, 'tp': True}
        orders = self._get_open_orders_raw(sym)
        if orders is None:
            return {'sl': None, 'tp': None}
        has_sl = False; has_tp = False
        ps_match = direction.upper() if direction else None
        for oo in orders:
            if ps_match:
                oo_ps = str(oo.get('positionSide', '')).upper()
                if oo_ps and oo_ps != 'BOTH' and oo_ps != ps_match:
                    continue
            otype = str(oo.get('type', '')).upper()
            orig = str(oo.get('origType', '')).upper()
            combined = otype + '|' + orig
            sp = float(oo.get('stopPrice', 0))
            if sp <= 0: continue
            if ('STOP' in combined) and ('PROFIT' not in combined):
                has_sl = True
            if 'PROFIT' in combined:
                has_tp = True
        return {'sl': has_sl, 'tp': has_tp}


# ═══════════════════════════════════════════════════════════════
#  메인 트레이더
# ═══════════════════════════════════════════════════════════════

class ConvergenceTrader:
    def __init__(self, cfg=None):
        self.cfg = cfg or CONFIG
        self.profile_name = "semi_auto"
        self.profile = {"desc": "🟡 반자동"}
        self.cfg["RISK_PCT"] = self.cfg["FULL_RISK_PCT"] * self.cfg["AUTO_ENTRY_PCT"]
        self.cfg["MAX_POSITIONS"] = self.cfg.get("MAX_POSITIONS", 3)
        self.cfg["MARGIN_EXPOSURE_LIMIT"] = self.cfg.get("MARGIN_EXPOSURE_LIMIT", 0.50)
        self.cfg["BTC_WEAK_FILTER"] = True

        os.makedirs(self.cfg["LOG_DIR"], exist_ok=True)

        self.exchange = ccxt.binance({
            'apiKey': self.cfg["BINANCE_API_KEY"], 'secret': self.cfg["BINANCE_SECRET"],
            'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.tg = Telegram(self.cfg["TELEGRAM_TOKEN"], self.cfg["TELEGRAM_CHAT_ID"])
        self.executor = Executor(self.exchange, self.cfg, self.tg)
        self.executor.setup_hedge_mode()

        self.strategy = ConvergenceBreakout({**CONVERGENCE_CONFIG,
            'EMA_GAP_THRESHOLD': self.cfg["EMA_GAP_THRESHOLD"],
            'MIN_SQUEEZE_CANDLES': self.cfg["MIN_SQUEEZE_CANDLES"],
            'MIN_CONFIDENCE': self.cfg["MIN_CONFIDENCE"],
            'MIN_ADX': self.cfg["MIN_ADX"],
            'TP_FIBO_LEVEL': self.cfg["FIBO_LEVEL"]})

        # BTC 필터
        self.btc_trend = None
        if self.cfg["BTC_FILTER_ENABLED"]:
            try:
                self.btc_trend = BTCTrend7Level(self.exchange, cache_sec=900)
                self.btc_trend.update()
                print(f"  BTC 필터: {self.btc_trend.status_str()}")
            except Exception as e:
                print(f"  BTC 필터 초기화 실패: {e}")

        self.onchain = OnchainFetcher(self.cfg.get("COINGLASS_API_KEY", ""),
                                       self.cfg.get("CG_CACHE_SEC", 300))

        # 포지션
        self.positions: List[Position] = []
        self._load_positions()
        self._sync_exchange_positions()
        self._startup_diagnosis()
        self.daily_trades = 0; self.daily_pnl = 0.0; self._daily_reset_done = False
        self._alerted_orders = set()
        self._scan_fails = {}

        # [8.2] 동적 워치리스트
        self._dynamic_watchlist = []
        self._watchlist_updated = 0
        self._squeeze_cache = {}  # coin → {'gap': float, 'time': timestamp}
        if self.cfg["WATCHLIST_MODE"] == "dynamic":
            self._refresh_watchlist()

        # 사이징 기준 (compound/half/quarter용)
        self._init_balance = self.executor.total_balance()

        # [V9.5] 멀티에이전트 시장 분석 (Shadow Mode)
        self.agent_hook = None
        if AgentHook is not None:
            try:
                # Watchdog 텔레그램 (에이전트 전용)
                wd_token = self.cfg.get("TELEGRAM_TOKEN_WATCHDOG", "")
                wd_chat = self.cfg.get("TELEGRAM_CHAT_ID_WATCHDOG", "")
                if wd_token and wd_chat:
                    tg_watchdog = Telegram(wd_token, wd_chat)
                    print("  ✅ Watchdog 텔레그램 연결됨")
                else:
                    tg_watchdog = self.tg
                    print("  ℹ️ Watchdog 텔레그램 미설정 → Semi TG 공유")
                self.agent_hook = AgentHook(
                    cg_client=self.onchain,
                    btc_filter=self.btc_trend,
                    tg=tg_watchdog,
                    exchange=self.exchange,
                    shadow_mode=True,
                    analysis_interval_h=4,
                )
                print("  ✅ 에이전트 시스템 초기화 (Shadow Mode)")
            except Exception as e:
                print(f"  ⚠️ 에이전트 초기화 실패 (봇 정상 운영): {e}")
                self.agent_hook = None

        # 시작 알람
        bal = self._init_balance
        mode = "페이퍼" if self.cfg["PAPER_TRADE"] else "실전"
        btc_s = self.btc_trend.status_str() if self.btc_trend else "OFF"
        wl_mode = self.cfg["WATCHLIST_MODE"]
        if wl_mode == "dynamic":
            wl_count = len(self._dynamic_watchlist)
            wl_desc = f"동적 ${self.cfg['VOL_MIN_M']}~${self.cfg['VOL_MAX_M']}M"
        else:
            wl_count = len(self.cfg['WATCHLIST_FIXED'])
            wl_desc = "고정"
        msg = (f"🚀 Semi-Auto V8.2 시작 [🟡 반자동]\n"
               f"모드: {mode} | 잔고: ${bal:,.0f}\n"
               f"스캔: {wl_desc} {wl_count}개 코인\n"
               f"풀R:{self.cfg['FULL_RISK_PCT']}% × 25%자동 = R:{self.cfg['RISK_PCT']:.1f}%\n"
               f"SL×{self.cfg['SL_RATIO']} Lock F{self.cfg['LOCK_FIBO']} Trail {self.cfg['TRAIL_PCT']}%\n"
               f"EE: 알림만 | TO: {self.cfg['TIMEOUT_H']}h\n"
               f"BTC7: {btc_s} | 약추세필터: ON\n"
               f"MaxPos: {self.cfg['MAX_POSITIONS']}\n"
               f"★v2: ADX≥{self.cfg['MIN_ADX']} VR≤{self.cfg['MAX_VOLUME_RATIO']} "
               f"LONG RSI≤{self.cfg['LONG_RSI_MAX']}")
        self.tg.send(msg); print(msg)

    # ── 사이징 (프로필 기반) ──

    def _max_positions(self):
        return self.cfg["MAX_POSITIONS"]

    def _sizing_base(self):
        """프로필별 사이징 기준"""
        bal = self.executor.total_balance()
        sizing = self.cfg["SIZING"]
        profit = bal - self._init_balance
        if sizing == 'compound':
            return max(bal, self._init_balance * 0.3)
        elif sizing == 'half':
            return max(self._init_balance + profit * 0.5, self._init_balance * 0.3)
        elif sizing == 'quarter':
            return max(self._init_balance + profit * 0.25, self._init_balance * 0.3)
        else:  # fixed
            return self._init_balance

    def _risk_pct(self):
        return self.cfg["RISK_PCT"]

    # ── [8.2] 동적 워치리스트 ──

    def _refresh_watchlist(self):
        """바이낸스 전체 ticker → 거래량 필터 → 동적 워치리스트 갱신"""
        try:
            tickers = self.exchange.fetch_tickers()
            vol_min = self.cfg["VOL_MIN_M"] * 1e6
            vol_max = self.cfg["VOL_MAX_M"] * 1e6

            candidates = []
            for sym, tk in tickers.items():
                if ':USDT' not in sym and '/USDT' not in sym:
                    continue
                quote_vol = float(tk.get('quoteVolume', 0) or 0)
                if quote_vol < vol_min or quote_vol > vol_max:
                    continue

                # 코인 이름 추출 + EXCLUDE 체크
                base = sym.split('/')[0].split(':')[0]
                if base in EXCLUDE_COINS:
                    continue

                # 유효한 USDT 선물만
                norm_sym = f"{base}/USDT"
                candidates.append((norm_sym, quote_vol / 1e6))

            self._dynamic_watchlist = [c[0] for c in candidates]
            self._vol_map = {c[0]: c[1] for c in candidates}
            self._watchlist_updated = time.time()

            print(f"  [워치리스트] {len(candidates)}개 코인 "
                  f"(${self.cfg['VOL_MIN_M']}~${self.cfg['VOL_MAX_M']}M)")

        except Exception as e:
            print(f"  워치리스트 갱신 실패: {e}")
            if not self._dynamic_watchlist:
                self._dynamic_watchlist = self.cfg["WATCHLIST_FIXED"]
                self._vol_map = {s: 0 for s in self._dynamic_watchlist}
                print(f"  → fallback: 고정 {len(self._dynamic_watchlist)}개")

    def _get_watchlist(self):
        """현재 스캔 대상 코인 목록"""
        if self.cfg["WATCHLIST_MODE"] != "dynamic":
            return self.cfg["WATCHLIST_FIXED"]

        # 주기적 갱신
        if time.time() - self._watchlist_updated > self.cfg["WATCHLIST_REFRESH_SEC"]:
            self._refresh_watchlist()

        return self._dynamic_watchlist

    # ── 스캔 ──

    def scan(self):
        """[8.2] 동적 워치리스트 2단계 스캔"""
        if self.btc_trend:
            self.btc_trend.update()

        watchlist = self._get_watchlist()
        signals = []

        for sym in watchlist:
            try:
                ohlcv = self.exchange.fetch_ohlcv(sym, '15m', limit=201)
                df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
                df['Date'] = pd.to_datetime(df['Date'], unit='ms')
                df.set_index('Date', inplace=True)

                # 미완성 봉 제거
                now_ts = pd.Timestamp.now(tz='UTC')
                if len(df) > 0:
                    last_start = df.index[-1]
                    if hasattr(last_start, 'tz') and last_start.tz is None:
                        last_start = last_start.tz_localize('UTC')
                    if now_ts < last_start + pd.Timedelta(minutes=15):
                        df = df.iloc[:-1]
                if len(df) < 100: continue

                result = self.strategy.detect(df)
                if not result or result['confidence'] < self.cfg["MIN_CONFIDENCE"]:
                    continue
                result['symbol'] = sym
                adx = result.get('details',{}).get('adx',0)
                vr = result.get('details',{}).get('breakout',{}).get('volume_ratio',0)
                rsi = result.get('details',{}).get('rsi',50)
                if adx < self.cfg["MIN_ADX"] or vr < self.cfg["MIN_VOLUME_RATIO"]:
                    continue
                if vr > self.cfg["MAX_VOLUME_RATIO"]:
                    continue
                if result['direction'] == 'long' and rsi > self.cfg["LONG_RSI_MAX"]:
                    continue

                oc = self.onchain.get_all(sym)
                # 일거래량 (캐시된 vol_map 사용, 없으면 ticker)
                daily_vol_m = self._vol_map.get(sym, 0)
                if daily_vol_m == 0:
                    try:
                        tk = self.exchange.fetch_ticker(sym)
                        daily_vol_m = float(tk.get('quoteVolume', 0)) / 1e6
                    except:
                        daily_vol_m = 0

                result.update({'onchain': oc, 'adx_value': adx,
                               'volume_ratio': vr, 'rsi': rsi,
                               'daily_vol_m': daily_vol_m})
                signals.append(result)
                time.sleep(0.15)
                self._scan_fails[sym] = 0
            except Exception as e:
                self._scan_fails[sym] = self._scan_fails.get(sym, 0) + 1
                if self._scan_fails[sym] == 5:
                    self.tg.send_error(f"{sym} 스캔 5회 연속 실패: {e}")
                continue
        return signals

    # ── 진입 ──

    def try_enter(self, signal):
        sym = signal['symbol']; d = signal['direction']

        # 포지션 수 제한
        max_pos = self._max_positions()
        if len(self.positions) >= max_pos: return
        if any(p.symbol == sym for p in self.positions): return
        if self.daily_trades >= self.cfg["MAX_DAILY_TRADES"]: return

        # [V8-6] BTC 필터: 약추세 차단 (기존 방향차단 대신)
        if self.btc_trend and self.cfg["BTC_FILTER_ENABLED"]:
            bt = self.btc_trend.trend
            # 기존 방향차단 유지
            if d == 'long' and self.btc_trend.long_blocked:
                print(f"  BTC7 필터: {sym} long 차단 ({self.btc_trend.trend_name})")
                return
            if d == 'short' and self.btc_trend.short_blocked:
                print(f"  BTC7 필터: {sym} short 차단 ({self.btc_trend.trend_name})")
                return
            # [V8-6] 약추세 차단 (옵션)
            if self.cfg.get("BTC_WEAK_FILTER", False):
                if bt in self.cfg.get("BTC_WEAK_STATES", [0, 1, 3]):
                    print(f"  BTC7 약추세: {sym} 대기 ({self.btc_trend.trend_name})")
                    return

        # [V8-3] CasTrail 통일 (그룹 없음)
        # SL 계산
        entry = signal['entry_price']
        sl_dist = abs(entry - signal['stop_loss']) * self.cfg["SL_RATIO"]
        if sl_dist <= 0: return
        sl = entry - sl_dist if d == 'long' else entry + sl_dist
        sl_dist_pct = sl_dist / entry * 100

        # 리스크 기반 사이징
        sizing_base = self._sizing_base()
        risk_pct = self._risk_pct()
        pos_notional = sizing_base * (risk_pct / 100) / (sl_dist_pct / 100)

        # 마진 한도
        bal = self.executor.total_balance()
        current_margin = sum(p.quantity * p.entry_price / self.cfg["LEVERAGE"]
                             for p in self.positions)
        remaining_margin = bal * self.cfg["MARGIN_EXPOSURE_LIMIT"] - current_margin
        max_notional = remaining_margin * self.cfg["LEVERAGE"]
        pos_notional = min(pos_notional, max_notional)
        if pos_notional <= 0: return

        qty = pos_notional / entry
        margin = pos_notional / self.cfg["LEVERAGE"]
        if margin < 5: return

        # [V8-3] Lock 가격 계산 (CasTrail)
        fibo_base = self.cfg["FIBO_LEVEL"]  # 2.618
        lock_fibo = self.cfg["LOCK_FIBO"]   # F2.0
        tp_dist_orig = abs(signal['take_profit'] - entry)  # fibo 2.618 거리
        lock_ratio = lock_fibo / fibo_base
        lock_dist = tp_dist_orig * lock_ratio
        lock_roe = lock_dist / entry * 100
        lock_price = entry + lock_dist if d == 'long' else entry - lock_dist

        # CasTrail: TP 없이 SL만 거래소에 설정 (Trail은 봇이 관리)
        # 안전장치: 거래소에 먼 TP도 설정 (F8.0 = 봇 다운 대비)
        safety_tp_fibo = 8.0
        safety_tp_ratio = safety_tp_fibo / fibo_base
        if d == 'long':
            safety_tp = entry + tp_dist_orig * safety_tp_ratio
        else:
            safety_tp = entry - tp_dist_orig * safety_tp_ratio

        # 거래소 주문: SL + 안전 TP(F8.0)
        result = self.executor.open_position(sym, d, qty, entry, sl, safety_tp)
        if not result: return

        ap = result['price']
        btc_state = self.btc_trend.trend_name if self.btc_trend else "N/A"
        btc_val = self.btc_trend.trend if self.btc_trend else 0

        pos = Position(
            symbol=sym, direction=d, entry_price=ap, quantity=result['qty'],
            stop_loss=sl, take_profit=safety_tp,
            atr=signal.get('atr', ap*0.01),
            entry_time=datetime.now(KST).isoformat(),
            confidence=signal['confidence'],
            reasons=signal.get('reasons',[]),
            squeeze_candles=signal.get('squeeze_candles',0),
            original_sl=sl,
            lock_trigger_roe=lock_roe,
            sl_order_id=result.get('sl_order_id',''),
            tp_order_id=result.get('tp_order_id',''),
            funding_rate=signal.get('onchain',{}).get('funding_rate',0),
            adx_value=signal.get('adx_value',0),
            volume_ratio=signal.get('volume_ratio',0),
            rsi=signal.get('rsi',50),
            gap_pct=signal.get('details',{}).get('gap_pct',0),
            entry_btc_state=btc_state,
            profile=self.profile_name,
        )
        self.positions.append(pos)
        self._save_positions()
        self.daily_trades += 1

        # [8.1-5] v9 스타일 진입 알람
        kr_dir = "롱 📈" if d == 'long' else "숏 📉"
        sl_pct = sl_dist / ap * 100
        lock_pct = lock_dist / ap * 100
        gap_pct = signal.get('details', {}).get('gap_pct', 0)
        adx_v = signal.get('adx_value', 0)
        vr_v = signal.get('volume_ratio', 0)
        rsi_v = signal.get('rsi', 50)
        sq_candles = signal.get('squeeze_candles', 0)
        conf_v = signal['confidence']
        daily_vol = signal.get('daily_vol_m', 0)

        # RSI 방향 판정
        rsi_label, rsi_aligned = get_rsi_direction(rsi_v, d)
        rsi_icon = "✅" if rsi_aligned else "⚠️"

        # 수렴 강도 분류
        if gap_pct <= 0.3:
            squeeze_grade = "🔵 극강수렴"
        elif gap_pct <= 0.5:
            squeeze_grade = "🟢 강수렴"
        elif gap_pct <= 0.7:
            squeeze_grade = "🟡 수렴"
        else:
            squeeze_grade = "⚪ 약수렴"

        # 거래량 등급
        if daily_vol >= 100:
            vol_grade = "🟢대형"
        elif daily_vol >= 30:
            vol_grade = "🟡중형"
        elif daily_vol >= 10:
            vol_grade = "🟠소형"
        else:
            vol_grade = "🔴극소"

        # Surge 히스토리
        surge_summary, surge_detail = get_surge_info(sym)

        # 안전 TP 가격 (F8.0)
        safety_tp_pct = abs(safety_tp - ap) / ap * 100

        msg = (f"🟡 반자동 진입 | {kr_dir} {sym}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"📋 진입 근거:\n"
               f"  conf: {conf_v} | GAP: {gap_pct:.2f}% | {squeeze_grade}\n"
               f"  ADX: {adx_v:.0f} | vol: ×{vr_v:.1f} | RSI: {rsi_v:.0f}\n"
               f"  수렴: {sq_candles}캔들\n"
               f"  BTC7: {btc_state}({btc_val:+d})\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"🎯 RSI 방향 {rsi_icon}\n"
               f"  {rsi_label}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"💰 포지션:\n"
               f"  진입가: {ap:,.6f}\n"
               f"  SL(×{self.cfg['SL_RATIO']}): {sl:,.6f} (-{sl_pct:.2f}%)\n"
               f"  Lock(F{lock_fibo}): {lock_price:,.6f} (+{lock_pct:.2f}%)\n"
               f"  안전TP(F8.0): {safety_tp:,.6f} (+{safety_tp_pct:.1f}%)\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"📊 전략: CasTrail\n"
               f"  Lock F{lock_fibo} → SL본전 → Trail {self.cfg['TRAIL_PCT']}%\n"
               f"  EE: 알림만 (수동 판단)\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"✅ 25% 자동 진입:\n"
               f"  수량: {result['qty']:.4f} | 마진: ${margin:.0f}\n"
               f"  풀R:{self.cfg['FULL_RISK_PCT']}% | 현재: {risk_pct:.1f}%\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"⚡ 추가 75%: ~${margin * 3:.0f}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 일거래량: ${daily_vol:.0f}M ({vol_grade})\n"
               f"  {surge_summary}\n"
               f"  {surge_detail}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"★v2: ADX≥{self.cfg['MIN_ADX']} VR≤{self.cfg['MAX_VOLUME_RATIO']} "
               f"{'RSI≤'+str(self.cfg['LONG_RSI_MAX'])+'(L)' if d=='long' else ''}\n"
               f"잔고: ${bal:,.0f}")
        self.tg.send(msg)

        # [V9.5] 에이전트 분석 트리거 (비동기)
        if self.agent_hook:
            try:
                self.agent_hook.on_position_open(pos, self.positions)
            except Exception:
                pass

    # ── 포지션 관리 ──

    def manage_positions(self):
        closed = []
        for pos in self.positions:
            price = self.executor.price(pos.symbol)
            if price <= 0: continue
            pos.update_extremes(price)
            roe_spot = pos.current_roe(price)
            roe_lev = roe_spot * self.cfg["LEVERAGE"]
            hc = pos.hold_candles_15m()
            hh = pos.hold_h()

            # ── SL 체크 ──
            sl_hit = ((pos.direction == 'long' and price <= pos.stop_loss) or
                      (pos.direction == 'short' and price >= pos.stop_loss))
            if sl_hit:
                reason = 'SL_본전' if pos.sl_locked else 'SL'
                self._close(pos, price, reason, closed); continue

            # ── 안전 TP 체크 (거래소 F8.0) ──
            tp_hit = ((pos.direction == 'long' and price >= pos.take_profit) or
                      (pos.direction == 'short' and price <= pos.take_profit))
            if tp_hit:
                self._close(pos, price, 'TP_F8.0', closed); continue

            # ── [V8-3] Lock: F2.0 도달 시 SL→본전 ──
            if not pos.sl_locked and pos.lock_trigger_roe > 0:
                if pos.max_fav_roe >= pos.lock_trigger_roe:
                    new_sl = pos.entry_price
                    if self.executor.update_sl_order(
                            pos.symbol, pos.direction, pos.quantity, new_sl, pos):
                        pos.sl_locked = True
                        pos.stop_loss = new_sl
                        self._save_positions()
                        d_s = 'Long' if pos.direction == 'long' else 'Short'
                        self.tg.send(
                            f"🔒 SL 본전 잠금 | {d_s} {pos.symbol}\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"Lock(F{self.cfg['LOCK_FIBO']}) 도달! "
                            f"(현물+{pos.max_fav_roe:.1f}%)\n"
                            f"SL: {pos.original_sl:,.6f} → {new_sl:,.6f} (본전)\n"
                            f"→ 이제 Trail {self.cfg['TRAIL_PCT']}% 모드")
                    else:
                        # [8.1-5 FIX] sl_locked=False 유지 → 다음 루프에서 재시도
                        raw_sym = pos.symbol.replace('/', '')
                        self.tg.send(
                            f"⚠️ SL 잠금 실패 (재시도 예정) | {pos.symbol}\n"
                            f"진입가: {pos.entry_price:,.6f}\n"
                            f"새 SL = {pos.entry_price:,.6f} (본전)\n"
                            f"→ {self.cfg['MONITOR_SEC']}초 후 자동 재시도")

            # ── [V8-3] Trail: 본전 잠금 후 최고점 대비 50% 후퇴 시 청산 ──
            if pos.sl_locked and pos.max_fav_roe > 0.5:
                trail_pct = self.cfg["TRAIL_PCT"]
                trail_threshold = pos.max_fav_roe * (1 - trail_pct / 100)
                if roe_spot <= trail_threshold:
                    self._close(pos, price, f'TRAIL_{trail_pct}%', closed)
                    continue

            # ── EE: 알림만 (반자동 — 수동 판단) ──
            if (self.cfg.get("EE_ALERT", True) and not pos.ee_checked
                    and hc >= self.cfg["EE_START_CANDLE"]):
                if hc <= self.cfg["EE_END_CANDLE"]:
                    if roe_lev <= self.cfg["EE_THRESHOLD_ROE"]:
                        d_s = 'Long' if pos.direction == 'long' else 'Short'
                        self.tg.send(
                            f"⚠️ EE 알림 | {d_s} {pos.symbol}\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"현물: {roe_spot:+.2f}% ({roe_lev:+.0f}%ROE)\n"
                            f"보유: {hh:.1f}h | 진입가: {pos.entry_price:,.6f}\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"EE 조건 도달 (-0.5% 현물, 2-4h)\n"
                            f"→ 수동 청산 판단 필요\n"
                            f"→ 바이낸스에서 직접 청산 또는 유지")
                        pos.ee_checked = True  # 1회만 알림
                else:
                    pos.ee_checked = True

            # ── [V8-5] Timeout 192h ──
            if hh >= self.cfg["TIMEOUT_H"]:
                self._close(pos, price, 'TO', closed); continue

        for p in closed:
            if p in self.positions: self.positions.remove(p)
        if closed: self._save_positions()

    # ── 청산 ──

    def _close(self, pos, price, reason, closed_list):
        result = self.executor.close_position(pos.symbol, pos.direction,
                                               pos.quantity, price, reason)
        # [8.1-5 FIX] 청산 실패 시 1회 재시도 (v9 동일)
        if not result:
            time.sleep(1)
            price = self.executor.price(pos.symbol) or price
            result = self.executor.close_position(pos.symbol, pos.direction,
                                                   pos.quantity, price, reason)
        if not result: return

        ap = result['price']
        pnl = pos.pnl(ap) - result['fee']
        roe = pos.current_roe(ap)
        roe_lev = roe * self.cfg["LEVERAGE"]
        hh = pos.hold_h()
        self.daily_pnl += pnl

        if self.cfg["PAPER_TRADE"]:
            mg_return = pos.quantity * pos.entry_price / self.cfg["LEVERAGE"]
            self.executor.paper_bal += pnl + mg_return

        # [E5] save_history에 시그널 정보 포함
        self._save_history({
            'symbol': pos.symbol, 'direction': pos.direction,
            'entry_price': pos.entry_price, 'exit_price': ap,
            'pnl': round(pnl, 4), 'roe_pct': round(roe, 2),
            'hold_h': round(hh, 1), 'reason': reason,
            'sl_locked': pos.sl_locked, 'profile': self.profile_name,
            'confidence': pos.confidence,
            'adx': pos.adx_value, 'vol_ratio': pos.volume_ratio,
            'gap_pct': pos.gap_pct, 'rsi': pos.rsi,
            'squeeze_candles': pos.squeeze_candles,
            'time': datetime.now(KST).isoformat(),
        })

        # [E2] v9 스타일 청산 알림 — ROE 레버리지 + 진입 시그널
        bal = self.executor.total_balance()
        icon = "🟢 수익" if pnl > 0 else "🔴 손실"
        lock_s = " [본전잠금]" if pos.sl_locked else ""
        msg = (f"{icon} | {pos.direction.upper()} {pos.symbol} | {reason}{lock_s}\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"진입: {pos.entry_price:,.6f} → 청산: {ap:,.6f}\n"
               f"손익: ${pnl:+,.2f} (현물{roe:+.2f}% ROE{roe_lev:+.0f}%)\n"
               f"보유: {hh:.1f}시간 | 최대수익: +{pos.max_fav_roe:.1f}%\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"📋 진입 시그널:\n"
               f"  conf:{pos.confidence} GAP:{pos.gap_pct:.2f}% sqz:{pos.squeeze_candles}\n"
               f"  ADX:{pos.adx_value:.0f} vol:×{pos.volume_ratio:.1f} RSI:{pos.rsi:.0f}\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"오늘: ${self.daily_pnl:+,.2f} | 잔고: ${bal:,.0f}\n"
               f"[{self.profile['desc']}] R:{self._risk_pct()}%")
        self.tg.send(msg)
        closed_list.append(pos)
        self._alerted_orders.discard(f"{pos.symbol}_{pos.direction}")

        # [V9.5] 에이전트 Shadow Mode — 실제 결과 기록
        if self.agent_hook:
            try:
                self.agent_hook.on_position_close(pos, reason, roe)
            except Exception:
                pass

    # ── 상태 보고 (2시간 간격) ──

    def status_report(self):
        bal = self.executor.total_balance()
        risk = self._risk_pct()
        now = datetime.now(KST).strftime('%m/%d %H:%M')

        # 거래소 실제 포지션 확인 (봇 관리 외 수동매매 포함)
        ex_positions = []
        if not self.cfg["PAPER_TRADE"]:
            try:
                raw = self.exchange.fetch_positions()
                for p in raw:
                    contracts = abs(float(p.get('contracts', 0)))
                    if contracts <= 0: continue
                    ex_positions.append({
                        'sym': self._norm_sym(p.get('symbol', '')),
                        'side': p.get('side', '').lower(),
                        'entry': float(p.get('entryPrice', 0)),
                        'contracts': contracts,
                        'pnl': float(p.get('unrealizedPnl', 0)),
                        'notional': float(p.get('notional', 0)),
                        'leverage': float(p.get('leverage', 20)),
                    })
            except: pass

        # 봇 포지션도 없고 거래소 포지션도 없으면 → 침묵
        if not self.positions and not ex_positions:
            return

        lines = [f"📊 상태 보고 [{now}]",
                 f"잔고: ${bal:,.0f} | [{self.profile['desc']}] R:{risk}%"]

        # ── 봇 관리 포지션 ──
        if self.positions:
            lines.append(f"\n🤖 봇 포지션: {len(self.positions)}/{self._max_positions()}")
            for p in self.positions:
                pr = self.executor.price(p.symbol)
                if pr <= 0: continue
                r = p.current_roe(pr); roe_lev = r * self.cfg["LEVERAGE"]
                hh = p.hold_h()
                d_s = 'L' if p.direction == 'long' else 'S'
                lock_s = "🔒" if p.sl_locked else "🔓"
                pnl = p.pnl(pr)
                # Trail 상태
                trail_s = ""
                if p.sl_locked and p.max_fav_roe > 0.5:
                    trail_thr = p.max_fav_roe * (1 - self.cfg["TRAIL_PCT"] / 100)
                    trail_s = f" Trail:{trail_thr:+.2f}%"
                lines.append(f"  {d_s} {p.symbol} {lock_s}{trail_s}")
                lines.append(f"    현물{r:+.2f}% (ROE{roe_lev:+.0f}%) | ${pnl:+,.1f} | {hh:.0f}h")
                lines.append(f"    최대+{p.max_fav_roe:.2f}% | SL:{p.stop_loss:,.4f}")

        # ── 거래소 수동 포지션 (봇 미관리) ──
        bot_keys = {f"{self._norm_sym(p.symbol)}_{p.direction}" for p in self.positions}
        manual_pos = [ep for ep in ex_positions
                      if f"{ep['sym']}_{ep['side']}" not in bot_keys]

        if manual_pos:
            lines.append(f"\n👤 수동 포지션: {len(manual_pos)}개")
            for ep in manual_pos:
                pr = self.executor.price(ep['sym'])
                if pr <= 0: pr = ep['entry']
                if ep['side'] == 'long':
                    roe_spot = (pr - ep['entry']) / ep['entry'] * 100
                else:
                    roe_spot = (ep['entry'] - pr) / ep['entry'] * 100
                roe_lev = roe_spot * ep['leverage']

                d_s = 'L' if ep['side'] == 'long' else 'S'
                notional = ep['contracts'] * ep['entry']
                lines.append(f"  {d_s} {ep['sym']}")
                lines.append(f"    진입: {ep['entry']:,.4f} | 현재: {pr:,.4f}")
                lines.append(f"    현물{roe_spot:+.2f}% (ROE{roe_lev:+.0f}%) | PnL:${ep['pnl']:+,.1f}")

                # ── 추천 전략 ──
                atr_est = ep['entry'] * 0.015  # ATR 추정 (1.5%)
                rec_sl = ep['entry'] - atr_est * 1.5 if ep['side'] == 'long' else ep['entry'] + atr_est * 1.5
                rec_lock = ep['entry'] * (1 + 0.02) if ep['side'] == 'long' else ep['entry'] * (1 - 0.02)
                rec_tp_f5 = ep['entry'] * (1 + 0.05) if ep['side'] == 'long' else ep['entry'] * (1 - 0.05)
                rec_tp_f8 = ep['entry'] * (1 + 0.08) if ep['side'] == 'long' else ep['entry'] * (1 - 0.08)
                sl_pct = abs(rec_sl - ep['entry']) / ep['entry'] * 100

                lines.append(f"    📌 추천 전략 (CasTrail 기준):")
                lines.append(f"    SL: {rec_sl:,.4f} (-{sl_pct:.2f}% 현물, -{sl_pct*ep['leverage']:.0f}% ROE)")
                lines.append(f"    EE: 2~4h 내 현물-0.5% (ROE-{0.5*ep['leverage']:.0f}%) 이하시 탈출")
                lines.append(f"    Lock: {rec_lock:,.4f} (+2% 현물) → 도달시 SL→본전")
                lines.append(f"    TP1(F5): {rec_tp_f5:,.4f} (+5% 현물, +{5*ep['leverage']:.0f}% ROE)")
                lines.append(f"    TP2(F8): {rec_tp_f8:,.4f} (+8% 현물, +{8*ep['leverage']:.0f}% ROE)")
                lines.append(f"    TO: 192h | Trail 50%: Lock후 최고점 50% 후퇴시 청산")

        if self.daily_pnl != 0:
            lines.append(f"\n오늘: ${self.daily_pnl:+,.2f} ({self.daily_trades}건)")

        btc_s = ""
        if self.btc_trend:
            btc_s = f"\n{self.btc_trend.status_str()}"
        msg = '\n'.join(lines) + btc_s
        self.tg.send(msg); print(msg)

    # ── SL/TP 검증 (주기적) ──

    def verify_all_orders(self):
        """[#9] 모든 포지션의 SL/TP 주문 존재 확인 — 알람은 포지션당 1회만"""
        for pos in self.positions:
            # 이미 알림한 포지션은 스킵
            key = f"{pos.symbol}_{pos.direction}"
            if key in self._alerted_orders:
                continue
            check = self.executor.verify_sl_tp(pos.symbol, pos.direction)
            # 조회 자체 실패(None) → 무시 (네트워크 문제)
            if check['sl'] is None or check['tp'] is None:
                continue
            missing = []
            if not check['sl']: missing.append('SL')
            if not check['tp']: missing.append('TP')
            if missing:
                self.tg.send_error(f"{pos.symbol} {'+'.join(missing)} 주문 없음! 확인 필요")
                self._alerted_orders.add(key)

    # ── 런타임 거래소 동기화 (핵심!) ──

    def runtime_sync(self):
        """
        5분마다 거래소 실제 포지션과 비교.
        거래소 SL/TP가 자동 체결되면 봇이 모르는 문제를 해결.
        봇에는 있는데 거래소에 없으면 → "거래소에서 청산됨" 처리.
        """
        if self.cfg["PAPER_TRADE"] or not self.positions:
            return

        try:
            ex_positions = self.exchange.fetch_positions()
            # 거래소 활성 포지션 키 수집
            ex_keys = set()
            for p in ex_positions:
                contracts = abs(float(p.get('contracts', 0)))
                if contracts <= 0: continue
                sym = self._norm_sym(p.get('symbol', ''))
                side = p.get('side', '').lower()
                ex_keys.add(f"{sym}_{side}")

            # 봇 포지션 중 거래소에 없는 것 찾기
            removed = []
            for pos in self.positions:
                key = f"{self._norm_sym(pos.symbol)}_{pos.direction}"
                if key not in ex_keys:
                    # 거래소에서 이미 청산됨 (SL/TP 자동 체결)
                    price = self.executor.price(pos.symbol)
                    if price <= 0: price = pos.entry_price
                    pnl = pos.pnl(price)
                    roe = pos.current_roe(price)
                    hh = pos.hold_h()
                    self.daily_pnl += pnl

                    self._save_history({
                        'symbol': pos.symbol, 'direction': pos.direction,
                        'entry_price': pos.entry_price, 'exit_price': price,
                        'pnl': round(pnl, 4), 'roe_pct': round(roe, 2),
                        'hold_h': round(hh, 1), 'reason': 'EXCHANGE_AUTO',
                        'sl_locked': pos.sl_locked, 'profile': pos.profile,
                        'confidence': pos.confidence,
                        'time': datetime.now(KST).isoformat(),
                    })

                    icon = "🟢" if pnl > 0 else "🔴"
                    d_s = pos.direction.upper()
                    bal = self.executor.total_balance()
                    self.tg.send(
                        f"{icon} 거래소 자동 청산 감지 | {d_s} {pos.symbol}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"진입: {pos.entry_price:,.6f}\n"
                        f"추정 손익: ${pnl:+,.2f} (현물 {roe:+.2f}%)\n"
                        f"보유: {hh:.1f}시간\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"거래소 SL/TP가 자동 체결된 것으로 판단\n"
                        f"잔고: ${bal:,.0f}")

                    removed.append(pos)
                    self._alerted_orders.discard(f"{pos.symbol}_{pos.direction}")

            for p in removed:
                if p in self.positions:
                    self.positions.remove(p)
            if removed:
                self._save_positions()
                print(f"  런타임 동기화: {len(removed)}개 포지션 거래소 청산 감지")

        except Exception as e:
            print(f"  런타임 동기화 실패: {e}")

    # ── 저장/로드 ──

    def _save_positions(self):
        data = []
        for p in self.positions:
            d = {}
            for k, v in p.__dict__.items():
                if isinstance(v, dict):
                    d[k] = {str(kk): vv for kk, vv in v.items()}
                else:
                    d[k] = v
            data.append(d)
        with open(self.cfg["POSITIONS_FILE"], 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def _load_positions(self):
        path = self.cfg["POSITIONS_FILE"]
        if not os.path.exists(path): return
        try:
            with open(path) as f: data = json.load(f)
            for d in data:
                for dict_field in ['roe_history']:
                    raw = d.get(dict_field, {})
                    converted = {}
                    for k, v in raw.items():
                        try: converted[int(k)] = v
                        except: converted[k] = v
                    d[dict_field] = converted
                reasons = d.pop('reasons', [])
                valid = {f.name for f in Position.__dataclass_fields__.values()}
                clean = {k: v for k, v in d.items() if k in valid}
                clean['reasons'] = reasons
                self.positions.append(Position(**clean))
            if self.positions:
                print(f"  포지션 복원: {len(self.positions)}개")
        except Exception as e:
            print(f"  포지션 로드 실패: {e}"); self.positions = []
            self.tg.send_error(f"포지션 로드 실패: {e}")

    def _save_history(self, record):
        p = os.path.join(self.cfg["LOG_DIR"], "trade_history_semi.jsonl")
        with open(p, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    @staticmethod
    def _norm_sym(sym):
        return sym.split(':')[0] if ':' in str(sym) else str(sym)

    def _sync_exchange_positions(self):
        """거래소 실제 포지션과 동기화"""
        if self.cfg["PAPER_TRADE"]: return
        try:
            ex_positions = self.exchange.fetch_positions()
            ex_active = {}
            for p in ex_positions:
                contracts = abs(float(p.get('contracts', 0)))
                if contracts <= 0: continue
                sym = self._norm_sym(p.get('symbol', ''))
                side = p.get('side', '').lower()
                key = f"{sym}_{side}"
                ex_active[key] = {
                    'sym': sym, 'side': side,
                    'entry_price': float(p.get('entryPrice', 0)),
                    'contracts': contracts,
                    'pnl': float(p.get('unrealizedPnl', 0)),
                }

            bot_keys = {f"{self._norm_sym(p.symbol)}_{p.direction}": p for p in self.positions}

            # 봇에 있는데 거래소에 없음 → 이미 청산
            for key, pos in list(bot_keys.items()):
                if key not in ex_active:
                    self.tg.send(f"동기화: {pos.symbol} {pos.direction} 이미 청산됨 → 제거")
                    self.executor._cancel_all_orders(pos.symbol)
                    if pos in self.positions: self.positions.remove(pos)

            # 거래소에 있는데 봇에 없음 → 복구
            for key, ex in ex_active.items():
                if key in bot_keys: continue
                sym = ex['sym']; ep = ex['entry_price']; qty = ex['contracts']
                if ep <= 0: continue

                # SL/TP 주문 찾기 — 바이낸스 직접 API 사용
                sl_p = 0; tp_p = 0
                try:
                    orders = self.executor._get_open_orders_raw(sym)
                    if orders:
                        for oo in orders:
                            sp = float(oo.get('stopPrice', 0))
                            if sp <= 0: continue
                            otype = str(oo.get('type', oo.get('origType', ''))).upper()
                            if 'STOP' in otype and 'PROFIT' not in otype:
                                sl_p = sp
                            elif 'PROFIT' in otype:
                                tp_p = sp
                except: pass

                if sl_p == 0:
                    self.tg.send_error(f"동기화: {sym} SL 없음! 수동 확인")
                    sl_p = ep * (0.96 if ex['side']=='long' else 1.04)
                if tp_p == 0:
                    self.tg.send_error(f"동기화: {sym} TP 없음! 수동 확인")
                    tp_p = ep * (1.10 if ex['side']=='long' else 0.90)

                gid = 'CasTrail'
                lock_dist = abs(tp_p - ep) * (self.cfg["LOCK_FIBO"] / 8.0)
                lock_roe = lock_dist / ep * 100 if self.cfg["LOCK_FIBO"] > 0 else 0

                pos = Position(
                    symbol=sym, direction=ex['side'],
                    entry_price=ep, quantity=qty,
                    stop_loss=sl_p, take_profit=tp_p,
                    atr=ep*0.01, entry_time=datetime.now(KST).isoformat(),
                    confidence=0, reasons=['복구'],
                    original_sl=sl_p, lock_trigger_roe=lock_roe,
                    profile=self.profile_name)
                self.positions.append(pos)
                self.tg.send(f"🔄 복구: {ex['side']} {sym} [{'CasTrail'}]\n"
                             f"진입:{ep:,.6f} SL:{sl_p:,.6f} TP:{tp_p:,.6f}")

            self._save_positions()
        except Exception as e:
            self.tg.send_error(f"거래소 동기화 실패: {e}")

    def _startup_diagnosis(self):
        """시작 시 모든 포지션 상태 점검 → 텔레그램 보고"""
        if not self.positions:
            return

        lines = ["🔍 포지션 자동 점검 결과"]
        issues = 0

        for pos in self.positions:
            price = self.executor.price(pos.symbol)
            if price <= 0:
                lines.append(f"  ❌ {pos.symbol}: 가격 조회 실패")
                issues += 1; continue

            roe = pos.current_roe(price)
            hh = pos.hold_h()
            d_s = 'L' if pos.direction == 'long' else 'S'

            # ── lock_trigger_roe 검증 ──
            lock_roe = pos.lock_trigger_roe
            pass  # v8: 단일 전략
            if lock_roe <= 0 and self.cfg["LOCK_FIBO"] > 0:
                # lock_trigger_roe가 0이면 복구 시 계산 실패 → 자동 수정
                if pos.take_profit > 0 and pos.entry_price > 0:
                    tp_dist = abs(pos.take_profit - pos.entry_price)
                    fibo_tp = 8.0
                    fibo_lock = self.cfg["LOCK_FIBO"]
                    lock_dist = tp_dist * (fibo_lock / fibo_tp)
                    lock_roe = lock_dist / pos.entry_price * 100
                    pos.lock_trigger_roe = lock_roe
                    lines.append(f"  🔧 {pos.symbol}: lock_roe 자동수정 → {lock_roe:.2f}%")
                else:
                    lines.append(f"  ❌ {pos.symbol}: lock_roe 계산 불가")
                    issues += 1

            # ── SL 잠금 상태 확인 ──
            lock_status = "🔒잠금" if pos.sl_locked else "🔓미잠금"
            should_lock = roe >= lock_roe and lock_roe > 0 and not pos.sl_locked

            status_line = (f"  {d_s} {pos.symbol} {lock_status}\n"
                           f"    현물{roe:+.2f}% | ${pos.pnl(price):+,.1f} | {hh:.0f}h\n"
                           f"    진입:{pos.entry_price:,.6f} SL:{pos.stop_loss:,.6f} TP:{pos.take_profit:,.6f}\n"
                           f"    잠금기준: 현물+{lock_roe:.2f}% {'← 도달!' if should_lock else ''}")

            if should_lock:
                status_line += (f"\n    ⚡ 현재 +{roe:.2f}% ≥ 기준 +{lock_roe:.2f}%"
                                f"\n    → SL을 본전({pos.entry_price:,.6f})으로 변경 시도 예정")
                # max_fav_roe도 갱신 (복구 시 0일 수 있음)
                pos.max_fav_roe = max(pos.max_fav_roe, roe)

            lines.append(status_line)

            # ── SL/TP 거래소 주문 검증 ──
            if not self.cfg["PAPER_TRADE"]:
                check = self.executor.verify_sl_tp(pos.symbol, pos.direction)
                if check['sl'] is None:
                    lines.append(f"    ℹ️ 주문 조회 실패 (네트워크) — 바이낸스에서 직접 확인")
                elif check['sl'] and check['tp']:
                    lines.append(f"    ✅ SL/TP 주문 확인됨")
                else:
                    missing = []
                    if not check['sl']: missing.append('SL')
                    if not check['tp']: missing.append('TP')
                    lines.append(f"    ⚠️ {'+'.join(missing)} 미감지 — 바이낸스에서 직접 확인 권장")
                    lines.append(f"       (이전 봇 주문은 API로 감지 안 될 수 있음)")

        if issues > 0:
            lines.append(f"\n⚠️ {issues}개 문제 발견 — 확인 필요")
        else:
            lines.append(f"\n✅ 모든 포지션 정상")

        self._save_positions()  # 자동수정 반영
        msg = '\n'.join(lines)
        self.tg.send(msg)
        print(msg)

    # ── 메인 루프 ──

    def run(self):
        print(f"\n{'='*50}\n  Semi-Auto Trader V8.0 [반자동]\n{'='*50}")
        last_scan = 0; last_status = 0; last_verify = 0; last_sync = 0; last_bar_min = -1

        while True:
            try:
                now = time.time()
                now_dt = datetime.now(KST)

                # ── [V9.5] 실시간 급락 감지 + 방향 전환 감지 + 정기분석 (매 루프) ──
                if self.agent_hook:
                    try:
                        self.agent_hook.crash_check(self.positions)
                    except Exception:
                        pass
                    try:
                        self.agent_hook.trend_check(self.positions)
                    except Exception:
                        pass
                    try:
                        self.agent_hook.periodic_check(self.positions)
                    except Exception:
                        pass

                # ── 포지션 모니터 (30초마다) ──
                if self.positions:
                    self.manage_positions()

                # ── 시그널 스캔: 15분봉 완성 직후 ──
                cur_min = now_dt.minute
                bar_boundary = cur_min % 15 == 0 and now_dt.second < 30
                if bar_boundary and cur_min != last_bar_min:
                    for sig in self.scan():
                        self.try_enter(sig)
                    last_scan = now; last_bar_min = cur_min

                # ── 보조 스캔 (3분) ──
                elif now - last_scan >= self.cfg["SCAN_SEC"]:
                    for sig in self.scan():
                        self.try_enter(sig)
                    last_scan = now

                # ── [#7] 상태 보고 (2시간) ──
                if now - last_status >= self.cfg["STATUS_SEC"]:
                    self.status_report()
                    last_status = now

                # ── [#9] SL/TP 검증 (30분) ──
                if now - last_verify >= 1800 and self.positions:
                    self.verify_all_orders()
                    last_verify = now

                # ── 거래소 동기화 (5분마다) — SL/TP 자동 청산 감지 ──
                if now - last_sync >= 300 and self.positions:
                    self.runtime_sync()
                    last_sync = now

                # ── 일일 리셋 ──
                if now_dt.hour == 0 and not self._daily_reset_done:
                    if self.daily_trades > 0:
                        self.tg.send(f"📅 일일 결산\n"
                                     f"거래: {self.daily_trades}건\n"
                                     f"손익: ${self.daily_pnl:+,.2f}\n"
                                     f"잔고: ${self.executor.total_balance():,.0f}")
                    self.daily_trades = 0; self.daily_pnl = 0
                    self._daily_reset_done = True
                elif now_dt.hour != 0:
                    self._daily_reset_done = False

                time.sleep(self.cfg["MONITOR_SEC"])

            except KeyboardInterrupt:
                self.tg.send("🛑 봇 수동 종료"); break
            except Exception as e:
                traceback.print_exc()
                self.tg.send_error(f"메인 루프 오류: {e}")
                time.sleep(60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    cfg = CONFIG.copy()
    if args.live:
        cfg["PAPER_TRADE"] = False
    cfg["_profile"] = "semi_auto"
    ConvergenceTrader(cfg).run()
