"""
auto_trader_v9.py — EMA 12/26 자동매매 봇 v9.3
═══════════════════════════════════════════════════════════════
v9.3 변경 (필터 최적화 + 시장 분석):
  [V9.3-1] BTC 방향 필터: Aggressive→Minimal (CRASH만 long 차단)
           - BEAR에서 차단된 Long 18건이 실제 수익이었음 (백테스트 검증)
  [V9.3-2] BTC squeeze 필터 제거 (BTC_SQUEEZE_ENABLED=False)
           - squeeze ON/OFF 무관하게 시그널 품질 동일 (백테스트 검증)
           - 기존 Overflow 전략이 v9에 통합됨
  [V9.3-3] 4시간 정기 시장 분석 알림 (코인별 EMA/ADX/ATR/스퀴즈 상태)
  [V9.3-4] 워치리스트 갱신 (22개: 제거5 + 추가8 + POLYX 관찰)
v9.2 대비 변경 (버그 수정):
  [V9.2-1] SL 설정 실패 시 포지션 즉시 청산 (무보호 방지)
  [V9.2-2] SL 잠금 실패 시 sl_locked=False 유지 → 자동 재시도
  [V9.2-3] 청산 실패 시 1회 재시도
  [V9.2-4] status_report 텔레그램 4000자 분할 전송
  [V9.2-5] 히스토리 파일명 v8→v9
  [V9.2-6] 포지션 저장 시 백업 + 로드 실패 시 백업 복구
  [V9.2-7] 심볼 정규화 강화 (BTCUSDT→BTC/USDT)
v9.0 대비 변경:
  [V9.1-1] scan() 진단 로그 추가 (detect 실패 사유 표시)
  [V9.1-2] BTC trend 캐시 60초 (기존 900초)
  [V9.1-3] vol_surge 보조 정보 텔레그램 표시
  [V9.1-4] _scan_fails 반복 알림 (10회마다)
  [V9.1-5] bar_boundary 타이밍 안정화
  [V9.1-6] 반응속도 개선: SCAN 60s, MONITOR 15s, CG캐시 120s
v8 대비 변경:
  [V9-1] 모드 E/D 토글 (--mode E 기본)
    E: Cascade (Lock→본전→TP F8.0) + EE 4-6h_1.0 ← 수익 극대화
    D: CasTrail (Lock→본전→Trail 50%) + EE 2-4h_0.5 ← 멘탈 안정
  [V9-2] BTC squeeze 필터 (EMA12/26 gap<0.5%, 8캔들 중 6개)
  [V9-3] 22코인 (기존11 → ETH/APT/BCH 제외 + 신규 추가)
  [V9-4] R:10%, P:7, compound
  [V9-5] Walk Forward OOS 검증 완료 (2024.03~)
    E: PF 3.68, $2700→$553K, MDD 28%
    D: PF 2.62, $2700→$86K, MDD 22%

사용법:
  python auto_trader_v9.py --mode E              # Cascade 모드 (기본)
  python auto_trader_v9.py --mode D              # CasTrail 모드 (멘탈용)
  python auto_trader_v9.py --mode E --live       # 실전
  python auto_trader_v9.py --mode D --live       # 실전 CasTrail
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
from dotenv import load_dotenv
load_dotenv()

try:
    from agents.v9_hook import AgentHook
except ImportError:
    AgentHook = None

KST = timezone(timedelta(hours=9))
BINANCE_FAPI = "https://fapi.binance.com"

# ═══════════════════════════════════════════════════════════════
#  [V9-1] 모드 설정: E(Cascade) / D(CasTrail)
# ═══════════════════════════════════════════════════════════════

MODE_CONFIGS = {
    "E": {
        "desc": "🔥 Cascade (수익극대화)",
        "STRATEGY": "Cascade",
        "LOCK_FIBO": 2.0,
        "TRAIL_PCT": 0,            # Trail 없음 → TP로 청산
        "TP_FIBO": 8.0,            # F8.0 고정 TP (실제 TP)
        "EE_START_CANDLE": 16,     # 4h
        "EE_END_CANDLE": 24,       # 6h
        "EE_THRESHOLD_ROE": -20,   # -1.0% × 20 = -20 ROE
    },
    "D": {
        "desc": "🛡️ CasTrail (멘탈안정)",
        "STRATEGY": "CasTrail",
        "LOCK_FIBO": 2.0,
        "TRAIL_PCT": 50,           # 50% 트레일링
        "TP_FIBO": 0,              # 고정 TP 없음 (Trail로 청산)
        "EE_START_CANDLE": 8,      # 2h
        "EE_END_CANDLE": 16,       # 4h
        "EE_THRESHOLD_ROE": -10,   # -0.5% × 20 = -10 ROE
    },
}

PROFILES = {
    "standard": {
        "RISK_PCT": 10.0,          # [V9-4] R:10%
        "SIZING": "compound",
        "MAX_POSITIONS": 7,        # [V9-4] P:7
        "MARGIN_EXPOSURE_LIMIT": 0.50,
        "BTC_WEAK_FILTER": False,
        "desc": "⚖️ 안정",
    },
}

# ═══════════════════════════════════════════════════════════════
#  설정 (실험 확정값)
# ═══════════════════════════════════════════════════════════════

CONFIG = {
    "PAPER_TRADE": True,

    # API Keys
    "BINANCE_API_KEY":  os.environ.get("BINANCE_API_KEY", ""),
    "BINANCE_SECRET":   os.environ.get("BINANCE_SECRET", ""),
    "COINGLASS_API_KEY": os.environ.get("COINGLASS_API_KEY", ""),
    "TELEGRAM_TOKEN":   os.environ.get("TELEGRAM_TOKEN_TRADER",
                        os.environ.get("TELEGRAM_TOKEN_MONITOR", "")),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
    # Watchdog 텔레그램 (에이전트 정기분석 + 급락 알림용)
    "TELEGRAM_TOKEN_WATCHDOG": os.environ.get("TELEGRAM_TOKEN_WATCHDOG", ""),
    "TELEGRAM_CHAT_ID_WATCHDOG": os.environ.get("TELEGRAM_CHAT_ID_WATCHDOG", ""),

    # [V9-3] 22코인 (Tier1 + Tier2, ETH/APT/BCH/DOGE 제외)
    "WATCHLIST": [
        # 기존 유지 (13개)
        "SOL/USDT", "XRP/USDT", "NEAR/USDT", "UNI/USDT", "COMP/USDT",
        "SAND/USDT", "QNT/USDT", "ANKR/USDT", "SUI/USDT",
        "NTRN/USDT", "TRX/USDT", "ENJ/USDT", "WLD/USDT",
        # 신규 추가 (8개)
        "1000PEPE/USDT", "DOGE/USDT", "ETH/USDT", "BNB/USDT",
        "DOT/USDT", "AVAX/USDT", "BCH/USDT", "AXS/USDT",
        # 관찰 (거래 적지만 유망)
        "POLYX/USDT",
    ],

    # 레버리지 + 수수료
    "LEVERAGE": 20,
    "FEE_RATE": 0.0004,

    # [V8-5] 시그널 필터
    "EMA_GAP_THRESHOLD": 0.7,
    "MIN_SQUEEZE_CANDLES": 15,
    "MIN_CONFIDENCE": 70,
    "MIN_ADX": 25,
    "MIN_VOLUME_RATIO": 1.5,
    "BREAKOUT_CLOSE_OUTSIDE": True,   # BB 밖 종가 필수 여부 (False=스퀴즈 구간 돌파만 체크)
    "FIBO_LEVEL": 2.618,
    "SL_RATIO": 1.5,

    # 전략 기본값 (MODE_CONFIGS에서 오버라이드)
    "STRATEGY": "Cascade",
    "LOCK_FIBO": 2.0,
    "TRAIL_PCT": 0,
    "TP_FIBO": 8.0,

    # EE 기본값 (MODE_CONFIGS에서 오버라이드)
    "ENABLE_EARLY_EXIT": True,
    "EE_START_CANDLE": 16,
    "EE_END_CANDLE": 24,
    "EE_THRESHOLD_ROE": -20,

    # [V8-5] Timeout 192h
    "TIMEOUT_H": 192,

    # [V8-6] BTC 방향 필터
    "BTC_FILTER_ENABLED": True,
    "BTC_WEAK_STATES": [0, 1, 3],

    # [V9-2] BTC squeeze 필터
    "BTC_SQUEEZE_ENABLED": False,
    "BTC_SQUEEZE_THRESHOLD": 0.5,   # EMA gap < 0.5%
    "BTC_SQUEEZE_MIN_COUNT": 6,     # 8캔들 중 6개 이상
    "BTC_SQUEEZE_WINDOW": 8,        # 최근 8캔들

    # 일일 한도
    "MAX_DAILY_TRADES": 10,

    # 주기
    "SCAN_SEC": 60,
    "MONITOR_SEC": 15,
    "STATUS_SEC": 7200,
    "CG_CACHE_SEC": 120,

    # 경로
    "LOG_DIR": "./trade_logs",
    "POSITIONS_FILE": "./trade_logs/positions_v9.json",
}


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
        # 1차: HTML
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000], "parse_mode": "HTML"},
                timeout=10)
            if r.status_code == 200 and r.json().get('ok'):
                return  # 성공
            # HTML 파싱 실패 → plain text 재시도
            print(f"  [TG] HTML 실패({r.status_code}), plain 재시도")
        except Exception as e:
            print(f"  [TG] HTML 예외: {e}")
        # 2차: plain text
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000]},
                timeout=10)
            if r.status_code != 200:
                print(f"  [TG] plain도 실패: {r.status_code} {r.text[:200]}")
        except Exception as e:
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

    7Lv_Minimal 필터 (백테스트 검증):
      long 차단: CRASH(-3) 만 — BEAR에서 알트 반등 Long 허용
      short 차단: STRONG_BULL(3) 만
    """
    NAMES = {3:'STRONG_BULL',2:'BULL',1:'MILD_BULL',0:'NEUTRAL',
             -1:'MILD_BEAR',-2:'BEAR',-3:'CRASH'}
    # 7Lv_Minimal — CRASH만 Long 차단, BEAR에서 Long 허용 (백테스트 검증: PF +0.09)
    LONG_BLOCK = {-3}        # CRASH만 (BEAR 해제 → 알트 반등 수익 포착)
    SHORT_BLOCK = {3}        # STRONG_BULL만

    def __init__(self, exchange, cache_sec=900):
        self.exchange = exchange
        self._cache_sec = cache_sec
        self._trend = 0
        self._last_update = 0
        self._ema_s = 0; self._ema_l = 0; self._adx = 0; self._rsi = 50
        self._is_squeeze = False; self._gap_pct = 0

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

            # [V9-2] BTC squeeze 계산
            gap_series = np.abs(ema_s - ema_l) / (c + 1e-9) * 100
            self._gap_pct = gap_series[idx] if not np.isnan(gap_series[idx]) else 999
            recent = gap_series[max(0, idx-7):idx+1]
            self._is_squeeze = bool(np.sum(recent < 0.5) >= 6) if len(recent) >= 4 else False

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
        sqz = "SQZ✅" if self._is_squeeze else "SQZ❌"
        return (f"BTC7 {self.trend_name}({self._trend:+d}) "
                f"EMA12:{self._ema_s:.0f} EMA26:{self._ema_l:.0f} "
                f"ADX:{self._adx:.1f} RSI:{self._rsi:.0f} | "
                f"gap:{self._gap_pct:.3f}% {sqz}")

    # [V9-2] BTC squeeze 감지
    @property
    def is_squeeze(self):
        return self._is_squeeze

    @property
    def gap_pct(self):
        return self._gap_pct


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
    gap_pct: float = 0              # [V9.2] 진입 시 EMA gap
    vol_surge: float = 0            # [V9.2] 진입 시 vol surge
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
        # [V9.2] API 캐시
        self._price_cache = {}      # {sym: (price, timestamp)}
        self._price_ttl = 3         # 3초 캐시
        self._bal_cache = None      # (balance_dict, timestamp)
        self._bal_ttl = 30          # 30초 캐시

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

    def _fetch_balance_cached(self):
        """잔고 조회 (30초 캐시)"""
        if self.cfg["PAPER_TRADE"]:
            return {"free": self.paper_bal, "total": self.paper_bal}
        now = time.time()
        if self._bal_cache and now - self._bal_cache[1] < self._bal_ttl:
            return self._bal_cache[0]
        try:
            b = self.ex.fetch_balance({"type": "future"})
            result = {"free": float(b["USDT"]["free"]), "total": float(b["USDT"]["total"])}
            self._bal_cache = (result, now)
            return result
        except:
            if self._bal_cache:
                return self._bal_cache[0]
            return {"free": 0.0, "total": 0.0}

    def balance(self):
        return self._fetch_balance_cached()["free"]

    def total_balance(self):
        """마진 포함 총 자산"""
        return self._fetch_balance_cached()["total"]

    def invalidate_balance(self):
        """잔고 캐시 무효화 (진입/청산 직후 호출)"""
        self._bal_cache = None

    def price(self, sym):
        """가격 조회 (3초 캐시)"""
        now = time.time()
        cached = self._price_cache.get(sym)
        if cached and now - cached[1] < self._price_ttl:
            return cached[0]
        try:
            p = float(self.ex.fetch_ticker(sym)["last"])
            self._price_cache[sym] = (p, now)
            return p
        except:
            return cached[0] if cached else 0.0

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

            # [V9.2-FIX1] SL 미설정 시 포지션 즉시 청산 (무보호 방지)
            if not ids['sl']:
                self._alert(f"🚨 {sym} SL 설정 불가 → 포지션 즉시 청산!")
                try:
                    close_side = "sell" if direction == "long" else "buy"
                    self.ex.create_order(sym, "market", close_side, qty,
                        params={"positionSide": ps})
                except Exception as ce:
                    self._alert(f"🚨 {sym} 긴급 청산도 실패! 수동 확인: {ce}")
                return None

            self.invalidate_balance()  # [V9.2] 잔고 캐시 갱신
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
                # ── 방법 A: positionSide로 방향 지정 (헤지모드) ──
                # 새 SL 먼저 생성
                o_sl = self.ex.create_order(sym, "STOP_MARKET", sl_side, rounded_qty,
                    params={"stopPrice": new_sl, "positionSide": ps,
                            "priceProtect": "TRUE"})
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
            # [V9-FIX] 헤지모드: positionSide와 reduceOnly 충돌 → reduceOnly 제거
            o = self.ex.create_order(sym, "market", side, qty,
                params={"positionSide": ps})
            fill_price = float(o.get("average", price))
            fill_qty = float(o.get("filled", qty))
            print(f"  {sym} 청산 체결: price={fill_price} qty={fill_qty}")
            self._cancel_all_orders(sym)
            self.invalidate_balance()  # [V9.2] 잔고 캐시 갱신
            return {"price": fill_price, "fee": fee}

        except Exception as e:
            err = str(e).lower()
            # 포지션이 이미 없음 (거래소 SL/TP가 먼저 체결)
            if any(k in err for k in ['reduceonly','reduce_only','insufficient',
                                        'position side does not match']):
                if self._verify_position_closed(sym, direction):
                    print(f"  {sym} 거래소에서 이미 청산 확인됨")
                    self._cancel_all_orders(sym)
                    return {"price": price, "fee": 0}
                else:
                    self._alert(
                        f"청산 실패 {sym} ({reason}): {e}\n"
                        f"포지션이 아직 존재함! 수동 청산 필요!")
                    return None
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
        self.profile_name = self.cfg.get("_profile", "standard")
        self.profile = PROFILES[self.profile_name]
        self.mode = self.cfg.get("_mode", "E")
        self.mode_cfg = MODE_CONFIGS[self.mode]

        # 프로필 값을 CONFIG에 병합
        self.cfg["RISK_PCT"] = self.profile["RISK_PCT"]
        self.cfg["SIZING"] = self.profile["SIZING"]
        self.cfg["MAX_POSITIONS"] = self.profile["MAX_POSITIONS"]
        self.cfg["MARGIN_EXPOSURE_LIMIT"] = self.profile["MARGIN_EXPOSURE_LIMIT"]
        self.cfg["BTC_WEAK_FILTER"] = self.profile.get("BTC_WEAK_FILTER", False)

        # [V9-1] 모드별 전략 오버라이드
        for k in ['STRATEGY','LOCK_FIBO','TRAIL_PCT','TP_FIBO',
                   'EE_START_CANDLE','EE_END_CANDLE','EE_THRESHOLD_ROE']:
            self.cfg[k] = self.mode_cfg[k]

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
            'BREAKOUT_CLOSE_OUTSIDE': self.cfg["BREAKOUT_CLOSE_OUTSIDE"],
            'TP_FIBO_LEVEL': self.cfg["FIBO_LEVEL"]})

        # BTC 필터
        self.btc_trend = None
        if self.cfg["BTC_FILTER_ENABLED"]:
            try:
                self.btc_trend = BTCTrend7Level(self.exchange, cache_sec=60)
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

        # [V9.5] Watchdog 텔레그램 (에이전트 분석 + 급락 알림 전용)
        wd_token = self.cfg.get("TELEGRAM_TOKEN_WATCHDOG", "")
        wd_chat = self.cfg.get("TELEGRAM_CHAT_ID_WATCHDOG", "")
        if wd_token and wd_chat:
            self.tg_watchdog = Telegram(wd_token, wd_chat)
            print("  ✅ Watchdog 텔레그램 연결됨")
        else:
            self.tg_watchdog = self.tg  # 미설정 시 트레이더 텔레그램 공유
            print("  ℹ️ Watchdog 텔레그램 미설정 → 트레이더 TG 공유")

        # [V9.4] 멀티에이전트 시장 분석 (Shadow Mode)
        self.agent_hook = None
        if AgentHook is not None:
            try:
                self.agent_hook = AgentHook(
                    cg_client=self.onchain,
                    btc_filter=self.btc_trend,
                    tg=self.tg_watchdog,  # watchdog 텔레그램으로 전송
                    exchange=self.exchange,
                    shadow_mode=True,
                    analysis_interval_h=4,
                )
                print("  ✅ 에이전트 시스템 초기화 (Shadow Mode)")
            except Exception as e:
                print(f"  ⚠️ 에이전트 초기화 실패 (봇 정상 운영): {e}")
                self.agent_hook = None

        # 사이징 기준 (compound/half/quarter용)
        self._init_balance = self.executor.total_balance()

        # 시작 알람
        bal = self._init_balance
        trade_mode = "페이퍼" if self.cfg["PAPER_TRADE"] else "실전"
        btc_s = self.btc_trend.status_str() if self.btc_trend else "OFF"
        sqz_on = "ON" if self.cfg.get("BTC_SQUEEZE_ENABLED") else "OFF"
        strat = self.cfg["STRATEGY"]
        ee_h = f"{self.cfg['EE_START_CANDLE']//4}~{self.cfg['EE_END_CANDLE']//4}h"
        ee_thr = f"{self.cfg['EE_THRESHOLD_ROE']/self.cfg['LEVERAGE']:.1f}%"
        if strat == "Cascade":
            strat_s = f"Cascade: Lock F{self.cfg['LOCK_FIBO']} → SL본전 → TP F{self.cfg['TP_FIBO']}"
        else:
            strat_s = f"CasTrail: Lock F{self.cfg['LOCK_FIBO']} → SL본전 → Trail {self.cfg['TRAIL_PCT']}%"
        msg = (f"🚀 Convergence V9.3 시작 [모드{self.mode} {self.mode_cfg['desc']}]\n"
               f"매매: {trade_mode} | 잔고: ${bal:,.0f}\n"
               f"코인: {len(self.cfg['WATCHLIST'])}개 | {strat_s}\n"
               f"R:{self.cfg['RISK_PCT']}% {self.cfg['SIZING']} P:{self.cfg['MAX_POSITIONS']}\n"
               f"SL×{self.cfg['SL_RATIO']} | EE: {ee_h} {ee_thr} | TO: {self.cfg['TIMEOUT_H']}h\n"
               f"BTC방향: Minimal (CRASH만차단) | squeeze: {sqz_on}\n"
               f"BTC7: {btc_s}\n"
               f"GAP≤{self.cfg['EMA_GAP_THRESHOLD']} conf≥{self.cfg['MIN_CONFIDENCE']} ADX≥{self.cfg['MIN_ADX']}\n"
               f"시장분석: 4h주기")
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

    # ── 스캔 ──

    @staticmethod
    def _calc_vol_surge(df, lookback=20):
        """vol_surge = 현재 캔들 거래량 / 직전 N캔들 평균 (보조 정보용)"""
        if len(df) < lookback + 1:
            return 0.0
        cur = df['Volume'].iloc[-1]
        avg = df['Volume'].iloc[-(lookback+1):-1].mean()
        return round(cur / avg, 2) if avg > 0 else 0.0

    def scan(self):
        # BTC 트렌드 갱신
        if self.btc_trend:
            self.btc_trend.update()

        signals = []
        scan_summary = []  # [V9.1] 진단 로그

        for sym in self.cfg["WATCHLIST"]:
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

                # [V9.1] 진단 로그 — detect 실패 사유
                if not result:
                    close = df['Close'].astype(float)
                    ema12 = close.ewm(span=12).mean().iloc[-1]
                    ema26 = close.ewm(span=26).mean().iloc[-1]
                    gap = abs(ema12 - ema26) / close.iloc[-1] * 100
                    short_sym = sym.replace('/USDT', '')
                    if gap < 1.0:  # 수렴 근처 코인만 로그
                        scan_summary.append(f"{short_sym}:gap{gap:.2f}%")
                    continue

                conf = result.get('confidence', 0)
                if conf < self.cfg["MIN_CONFIDENCE"]:
                    short_sym = sym.replace('/USDT', '')
                    scan_summary.append(f"{short_sym}:conf{conf}")
                    continue

                result['symbol'] = sym
                adx = result.get('details',{}).get('adx',0)
                vr = result.get('details',{}).get('breakout',{}).get('volume_ratio',0)
                rsi = result.get('details',{}).get('rsi',50)

                if adx < self.cfg["MIN_ADX"] or vr < self.cfg["MIN_VOLUME_RATIO"]:
                    short_sym = sym.replace('/USDT', '')
                    d = result.get('direction', '?')
                    reason = []
                    if adx < self.cfg["MIN_ADX"]: reason.append(f"ADX:{adx:.1f}<25")
                    if vr < self.cfg["MIN_VOLUME_RATIO"]: reason.append(f"vol:{vr:.2f}<1.5")
                    print(f"  ⚠️ {short_sym} {d} 시그널 필터: {', '.join(reason)} "
                          f"(conf:{conf} RSI:{rsi:.0f})")
                    continue

                # [V9.1] vol_surge 계산 (보조 정보 — 텔레그램 표시용)
                result['vol_surge'] = self._calc_vol_surge(df)

                oc = self.onchain.get_all(sym)
                result.update({'onchain': oc, 'adx_value': adx,
                               'volume_ratio': vr, 'rsi': rsi})
                signals.append(result)
                time.sleep(0.08)
                self._scan_fails[sym] = 0
            except Exception as e:
                self._scan_fails[sym] = self._scan_fails.get(sym, 0) + 1
                # [V9.1] 10회마다 반복 알림 (기존: 5회째 1회만)
                if self._scan_fails[sym] % 10 == 5:
                    self.tg.send_error(f"{sym} 스캔 {self._scan_fails[sym]}회 연속 실패: {e}")
                continue

        # [V9.1] 요약 로그
        if scan_summary:
            print(f"  [scan] near-convergence: {' | '.join(scan_summary[:8])}"
                  + (f" +{len(scan_summary)-8}개" if len(scan_summary) > 8 else ""))

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

        # [V9-2] BTC squeeze 필터: BTC가 squeeze 상태일 때만 진입
        if self.cfg.get("BTC_SQUEEZE_ENABLED", False) and self.btc_trend:
            if not self.btc_trend.is_squeeze:
                print(f"  BTC squeeze 아님: {sym} 대기 (gap:{self.btc_trend.gap_pct:.3f}%)")
                return

        # [V8-3] CasTrail 통일 (그룹 없음)
        # SL 계산
        entry = signal['entry_price']
        if entry <= 0: return  # [V9.2] 비정상 시그널 방어
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

        # [V9-1] Lock + TP 계산 (모드별)
        fibo_base = self.cfg["FIBO_LEVEL"]  # 2.618
        lock_fibo = self.cfg["LOCK_FIBO"]   # F2.0
        tp_fibo = self.cfg["TP_FIBO"]       # E:8.0 / D:0
        tp_dist_orig = abs(signal['take_profit'] - entry)  # fibo 2.618 거리
        if tp_dist_orig <= 0: return  # [V9.2] TP 거리 0 방어
        lock_ratio = lock_fibo / fibo_base
        lock_dist = tp_dist_orig * lock_ratio
        lock_roe = lock_dist / entry * 100
        lock_price = entry + lock_dist if d == 'long' else entry - lock_dist

        # TP 계산
        if tp_fibo > 0:
            # E(Cascade): F8.0 실제 TP
            tp_ratio = tp_fibo / fibo_base
            if d == 'long':
                exchange_tp = entry + tp_dist_orig * tp_ratio
            else:
                exchange_tp = entry - tp_dist_orig * tp_ratio
        else:
            # D(CasTrail): 안전 TP F8.0 (봇 다운 대비)
            safety_ratio = 8.0 / fibo_base
            if d == 'long':
                exchange_tp = entry + tp_dist_orig * safety_ratio
            else:
                exchange_tp = entry - tp_dist_orig * safety_ratio

        # 거래소 주문: SL + TP
        result = self.executor.open_position(sym, d, qty, entry, sl, exchange_tp)
        if not result: return

        ap = result['price']
        btc_state = self.btc_trend.trend_name if self.btc_trend else "N/A"
        btc_val = self.btc_trend.trend if self.btc_trend else 0
        btc_sqz = self.btc_trend.is_squeeze if self.btc_trend else False

        pos = Position(
            symbol=sym, direction=d, entry_price=ap, quantity=result['qty'],
            stop_loss=sl, take_profit=exchange_tp,
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
            vol_surge=signal.get('vol_surge',0),
            entry_btc_state=btc_state,
            profile=self.profile_name,
        )
        self.positions.append(pos)
        self._save_positions()
        self.daily_trades += 1

        # [V9.4] 에이전트 분석 트리거 (비동기 — 봇 블로킹 없음)
        if self.agent_hook:
            try:
                self.agent_hook.on_position_open(pos, self.positions)
            except Exception:
                pass

        # 한글 진입 알람
        kr_dir = "롱 📈" if d == 'long' else "숏 📉"
        sl_pct = sl_dist / ap * 100
        lock_pct = lock_dist / ap * 100
        gap_pct = signal.get('details', {}).get('gap_pct', 0)
        adx_v = signal.get('adx_value', 0)
        vr_v = signal.get('volume_ratio', 0)
        rsi_v = signal.get('rsi', 50)
        sq_candles = signal.get('squeeze_candles', 0)
        vol_surge_v = signal.get('vol_surge', 0)
        conf_v = signal['confidence']
        strat = self.cfg["STRATEGY"]
        ee_h = f"{self.cfg['EE_START_CANDLE']//4}~{self.cfg['EE_END_CANDLE']//4}h"
        ee_thr = f"{self.cfg['EE_THRESHOLD_ROE']/self.cfg['LEVERAGE']:.1f}%"
        sqz_s = "✅" if btc_sqz else "❌"

        if strat == "Cascade":
            strat_desc = f"Lock F{lock_fibo} → SL본전 → TP F{tp_fibo}"
        else:
            strat_desc = f"Lock F{lock_fibo} → SL본전 → Trail {self.cfg['TRAIL_PCT']}%"

        msg = (f"🟢 진입 | {kr_dir} {sym} [모드{self.mode} {self.mode_cfg['desc']}]\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"📋 진입 근거:\n"
               f"  conf: {conf_v} (≥70 ✅) | GAP: {gap_pct:.2f}%\n"
               f"  adx: {adx_v:.0f} | vol: ×{vr_v:.1f} | RSI: {rsi_v:.0f}\n"
               f"  수렴: {sq_candles}캔들 | BTC squeeze: {sqz_s}\n"
               f"  vol_surge: ×{vol_surge_v:.2f}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"📊 전략: {strat}\n"
               f"  {strat_desc}\n"
               f"  EE: {ee_h} 현물{ee_thr} 미만시 탈출\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"💰 포지션:\n"
               f"  진입가: {ap:,.6f}\n"
               f"  SL(×{self.cfg['SL_RATIO']}): {sl:,.6f} (-{sl_pct:.2f}%)\n"
               f"  Lock(F{lock_fibo}): {lock_price:,.6f} (+{lock_pct:.2f}%)\n"
               f"  TP: {exchange_tp:,.6f}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"  수량: {result['qty']:.4f} | 마진: ${margin:.0f}\n"
               f"  R:{risk_pct}% {self.cfg['SIZING']} P:{self.cfg['MAX_POSITIONS']}\n"
               f"  BTC7: {btc_state}({btc_val:+d}) | 잔고: ${bal:,.0f}")
        self.tg.send(msg)

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

            # ── TP 체크 ──
            tp_hit = ((pos.direction == 'long' and price >= pos.take_profit) or
                      (pos.direction == 'short' and price <= pos.take_profit))
            if tp_hit:
                tp_label = f'TP_F{self.cfg["TP_FIBO"]}' if self.cfg["TP_FIBO"] > 0 else 'TP_안전'
                self._close(pos, price, tp_label, closed); continue

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
                        # [V9.2-FIX2] 실패 시 sl_locked=False 유지 → 다음 루프에서 재시도
                        raw_sym = pos.symbol.replace('/', '')
                        self.tg.send(
                            f"⚠️ SL 잠금 실패 (재시도 예정) | {pos.symbol}\n"
                            f"진입가: {pos.entry_price:,.6f}\n"
                            f"새 SL = {pos.entry_price:,.6f} (본전)\n"
                            f"→ 15초 후 자동 재시도")

            # ── Trail: 본전 잠금 후 최고점 대비 X% 후퇴 시 청산 (D모드만) ──
            trail_pct = self.cfg["TRAIL_PCT"]
            if trail_pct > 0 and pos.sl_locked and pos.max_fav_roe > 0.5:
                trail_threshold = pos.max_fav_roe * (1 - trail_pct / 100)
                if roe_spot <= trail_threshold:
                    self._close(pos, price, f'TRAIL_{trail_pct}%', closed)
                    continue

            # ── [V8-4] EE: 2-4h, -0.5% ──
            if (self.cfg["ENABLE_EARLY_EXIT"] and not pos.ee_checked
                    and hc >= self.cfg["EE_START_CANDLE"]):
                if hc <= self.cfg["EE_END_CANDLE"]:
                    if roe_lev <= self.cfg["EE_THRESHOLD_ROE"]:
                        self._close(pos, price, f'EE_{hc//4}h', closed); continue
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
        # [V9.2-FIX3] 청산 실패 시 1회 재시도
        if not result:
            time.sleep(1)
            price = self.executor.price(pos.symbol) or price
            result = self.executor.close_position(pos.symbol, pos.direction,
                                                   pos.quantity, price, reason)
        if not result: return
        ap = result['price']
        pnl = pos.pnl(ap) - result['fee']
        roe = pos.current_roe(ap)
        hh = pos.hold_h()
        self.daily_pnl += pnl

        if self.cfg["PAPER_TRADE"]:
            mg_return = pos.quantity * pos.entry_price / self.cfg["LEVERAGE"]
            self.executor.paper_bal += pnl + mg_return

        _, gcfg_desc = "CasTrail", "CasTrail"
        self._save_history({
            'symbol': pos.symbol, 'direction': pos.direction,
            'entry_price': pos.entry_price, 'exit_price': ap,
            'pnl': round(pnl, 4), 'roe_pct': round(roe, 2),
            'hold_h': round(hh, 1), 'reason': reason,
            'sl_locked': pos.sl_locked, 'profile': self.profile_name,
            'confidence': pos.confidence,
            'adx': pos.adx_value, 'vol_ratio': pos.volume_ratio,
            'gap_pct': pos.gap_pct, 'vol_surge': pos.vol_surge,
            'squeeze_candles': pos.squeeze_candles, 'rsi': pos.rsi,
            'time': datetime.now(KST).isoformat(),
        })

        # 한글 매도 알람
        bal = self.executor.total_balance()
        icon = "🟢 수익" if pnl > 0 else "🔴 손실"
        lock_s = " [본전잠금]" if pos.sl_locked else ""
        roe_lev = roe * self.cfg["LEVERAGE"]
        msg = (f"{icon} | {pos.direction.upper()} {pos.symbol} | {reason}{lock_s}\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"진입: {pos.entry_price:,.6f} → 청산: {ap:,.6f}\n"
               f"손익: ${pnl:+,.2f} (현물{roe:+.2f}% ROE{roe_lev:+.0f}%)\n"
               f"보유: {hh:.1f}시간 | 최대수익: +{pos.max_fav_roe:.1f}%\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"📋 진입 시그널:\n"
               f"  conf:{pos.confidence} GAP:{pos.gap_pct:.2f}% sqz:{pos.squeeze_candles}\n"
               f"  ADX:{pos.adx_value:.0f} vol:×{pos.volume_ratio:.1f} surge:×{pos.vol_surge:.1f}\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"오늘: ${self.daily_pnl:+,.2f} | 잔고: ${bal:,.0f}\n"
               f"[{self.profile['desc']}] R:{self._risk_pct()}%")
        self.tg.send(msg)
        closed_list.append(pos)
        self._alerted_orders.discard(f"{pos.symbol}_{pos.direction}")

        # [V9.4] 에이전트 Shadow Mode — 실제 결과 기록
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

        lines = [f"📊 상태 보고 [{now}] 모드{self.mode}",
                 f"잔고: ${bal:,.0f} | R:{risk}% {self.cfg['SIZING']} P:{self.cfg['MAX_POSITIONS']}"]

        # ── 봇 관리 포지션 ──
        if self.positions:
            strat = self.cfg["STRATEGY"]
            lev = self.cfg["LEVERAGE"]
            lines.append(f"\n🤖 봇 포지션: {len(self.positions)}/{self._max_positions()} [{strat}]")
            for p in self.positions:
                pr = self.executor.price(p.symbol)
                if pr <= 0: continue
                r = p.current_roe(pr); roe_lev = r * lev
                hh = p.hold_h()
                d_s = 'L' if p.direction == 'long' else 'S'
                lock_s = "🔒" if p.sl_locked else "🔓"
                pnl = p.pnl(pr)
                is_long = p.direction == 'long'

                # Fibo 레벨 계산 (TP F8.0에서 역산)
                fibo_base = self.cfg["FIBO_LEVEL"]  # 2.618
                tp_dist_full = abs(p.take_profit - p.entry_price)
                # tp = entry ± squeeze * (8.0/2.618) → squeeze = tp_dist * 2.618/8.0
                sq = tp_dist_full * fibo_base / 8.0 if tp_dist_full > 0 else p.atr * 2

                f2_dist = sq * 2.0 / fibo_base * fibo_base  # = sq * 2.0
                f3_dist = sq * 3.236 / fibo_base * fibo_base  # = sq * 3.236
                f8_dist = tp_dist_full
                if is_long:
                    f2_p = p.entry_price + sq * 2.0
                    f3_p = p.entry_price + sq * 3.236
                else:
                    f2_p = p.entry_price - sq * 2.0
                    f3_p = p.entry_price - sq * 3.236
                f2_pct = sq * 2.0 / p.entry_price * 100
                f3_pct = sq * 3.236 / p.entry_price * 100
                f8_pct = tp_dist_full / p.entry_price * 100
                sl_pct = abs(p.stop_loss - p.entry_price) / p.entry_price * 100

                # Trail 상태 (D모드)
                trail_s = ""
                trail_pct = self.cfg["TRAIL_PCT"]
                if trail_pct > 0 and p.sl_locked and p.max_fav_roe > 0.5:
                    trail_thr = p.max_fav_roe * (1 - trail_pct / 100)
                    trail_s = f"\n    Trail: 최고{p.max_fav_roe:+.2f}% → 청산기준{trail_thr:+.2f}%"

                # EE 상태
                ee_s_h = self.cfg['EE_START_CANDLE'] // 4
                ee_e_h = self.cfg['EE_END_CANDLE'] // 4
                ee_thr_spot = self.cfg['EE_THRESHOLD_ROE'] / lev
                hc = p.hold_candles_15m()
                if hc < self.cfg['EE_START_CANDLE']:
                    ee_status = f"대기 ({ee_s_h}h부터)"
                elif hc <= self.cfg['EE_END_CANDLE'] and not p.ee_checked:
                    ee_status = f"감시중 (현물{ee_thr_spot:.1f}% 미만시 탈출)"
                else:
                    ee_status = "통과"

                lines.append(f"  {d_s} {p.symbol} {lock_s}")
                lines.append(f"    현물{r:+.2f}% (ROE{roe_lev:+.0f}%) | ${pnl:+,.1f} | {hh:.0f}h")
                lines.append(f"    ─ 가격 레벨 ─")
                lines.append(f"    SL: {p.stop_loss:,.4f} (-{sl_pct:.2f}% ROE-{sl_pct*lev:.0f}%)"
                             f" {'[본전]' if p.sl_locked else '[원본]'}")
                lines.append(f"    Lock F2.0: {f2_p:,.4f} (+{f2_pct:.2f}% ROE+{f2_pct*lev:.0f}%)")
                lines.append(f"    F3.236: {f3_p:,.4f} (+{f3_pct:.2f}% ROE+{f3_pct*lev:.0f}%)")
                lines.append(f"    TP F8.0: {p.take_profit:,.4f} (+{f8_pct:.2f}% ROE+{f8_pct*lev:.0f}%)")
                lines.append(f"    ─ 상태 ─")
                lines.append(f"    최대유리: +{p.max_fav_roe:.2f}% | EE: {ee_status}")
                if trail_s:
                    lines.append(f"    {trail_s}")

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
        # [V9.2-FIX4] 텔레그램 4000자 초과 시 분할 전송
        if len(msg) <= 3900:
            self.tg.send(msg)
        else:
            chunks = msg.split('\n\n')
            buf = ""
            for chunk in chunks:
                if len(buf) + len(chunk) + 2 > 3900:
                    if buf:
                        self.tg.send(buf)
                    buf = chunk
                else:
                    buf = buf + "\n\n" + chunk if buf else chunk
            if buf:
                self.tg.send(buf)
        print(msg)

    # ── SL/TP 검증 (주기적) ──

    # ── [V9.3] 4시간 정기 시장 분석 ──

    def market_analysis(self):
        """
        4시간마다 워치리스트 전체 코인의 시장 상태 분석.
        - BTC 상태 요약
        - 코인별 EMA gap, ADX, ATR, 스퀴즈 진행도
        - 진입 임박 / 조건 부족 분류
        """
        now = datetime.now(KST).strftime('%m/%d %H:%M')
        lines = [f"🔬 시장 분석 [{now}]"]

        # ── BTC 상태 ──
        if self.btc_trend:
            self.btc_trend.update()
            bt = self.btc_trend
            sqz_s = "스퀴즈 중 ✅" if bt.is_squeeze else "방향성 ❌"
            lines.append(f"\n📊 BTC: {bt.trend_name}({bt.trend:+d}) | "
                         f"gap:{bt.gap_pct:.3f}% | ADX:{bt._adx:.1f} | RSI:{bt._rsi:.0f}")
            lines.append(f"  {sqz_s} | EMA12:{bt._ema_s:,.0f} EMA26:{bt._ema_l:,.0f}")

            # BTC 방향 영향
            if bt.trend <= -3:
                lines.append(f"  ⚠️ CRASH — Long 차단 중")
            elif bt.trend >= 3:
                lines.append(f"  ⚠️ S_BULL — Short 차단 중")

        # ── 코인별 스캔 ──
        ready = []     # 시그널 임박
        squeezing = [] # 스퀴즈 진행 중
        quiet = []     # 조용함

        for sym in self.cfg["WATCHLIST"]:
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

                c = df['Close'].astype(float)
                ema12 = c.ewm(span=12).mean()
                ema26 = c.ewm(span=26).mean()
                gap = abs(ema12.iloc[-1] - ema26.iloc[-1]) / c.iloc[-1] * 100

                # ADX
                h = df['High'].astype(float); lo = df['Low'].astype(float)
                prev_c = c.shift(1).fillna(c.iloc[0])
                tr = pd.concat([h-lo, (h-prev_c).abs(), (lo-prev_c).abs()], axis=1).max(axis=1)
                atr = tr.rolling(14).mean().iloc[-1]
                atr_pct = atr / c.iloc[-1] * 100

                up = h.diff(); down = -lo.diff()
                p_dm = up.where((up>down)&(up>0), 0)
                m_dm = down.where((down>up)&(down>0), 0)
                tr_s = tr.ewm(alpha=1/14).mean()
                pdi = 100 * p_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
                mdi = 100 * m_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
                dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-9)
                adx = dx.ewm(alpha=1/14).mean().iloc[-1]

                # 스퀴즈 캔들 수 계산
                sqz_count = 0
                threshold = self.cfg["EMA_GAP_THRESHOLD"]
                for i in range(len(df)-2, max(len(df)-80, 0), -1):
                    row = df.iloc[i]
                    e12 = ema12.iloc[i]; e26 = ema26.iloc[i]
                    g = abs(e12 - e26) / row['Close'] * 100
                    if g <= threshold:
                        sqz_count += 1
                    else:
                        break

                short = sym.replace('/USDT', '')

                # 분류
                missing = []
                if gap > threshold:
                    missing.append(f"gap:{gap:.2f}%>{threshold}")
                if adx < self.cfg["MIN_ADX"]:
                    missing.append(f"ADX:{adx:.0f}<{self.cfg['MIN_ADX']}")
                if sqz_count < self.cfg["MIN_SQUEEZE_CANDLES"]:
                    missing.append(f"sqz:{sqz_count}<{self.cfg['MIN_SQUEEZE_CANDLES']}")

                info = {'sym': short, 'gap': gap, 'adx': adx,
                        'atr_pct': atr_pct, 'sqz': sqz_count, 'missing': missing}

                if sqz_count >= self.cfg["MIN_SQUEEZE_CANDLES"] and gap <= threshold:
                    ready.append(info)
                elif sqz_count >= 8 or gap <= threshold * 1.5:
                    squeezing.append(info)
                else:
                    quiet.append(info)

                time.sleep(0.05)
            except:
                continue

        # ── 시그널 임박 ──
        if ready:
            lines.append(f"\n🎯 시그널 임박 ({len(ready)}코인):")
            for r in sorted(ready, key=lambda x: -x['sqz']):
                miss = f" ⚠️{', '.join(r['missing'])}" if r['missing'] else " ✅진입 가능"
                lines.append(f"  {r['sym']:>8} sqz:{r['sqz']:2d}봉 gap:{r['gap']:.2f}% "
                             f"ADX:{r['adx']:.0f} ATR:{r['atr_pct']:.1f}%{miss}")

        # ── 스퀴즈 진행 중 ──
        if squeezing:
            lines.append(f"\n⏳ 스퀴즈 진행 ({len(squeezing)}코인):")
            for r in sorted(squeezing, key=lambda x: -x['sqz'])[:10]:
                miss = ', '.join(r['missing'])
                lines.append(f"  {r['sym']:>8} sqz:{r['sqz']:2d}봉 gap:{r['gap']:.2f}% "
                             f"ADX:{r['adx']:.0f} | 부족: {miss}")

        # ── 조용한 코인 요약 ──
        if quiet:
            quiet_names = [r['sym'] for r in quiet]
            lines.append(f"\n💤 조용 ({len(quiet)}코인): {', '.join(quiet_names[:12])}"
                         + (f" +{len(quiet)-12}개" if len(quiet) > 12 else ""))

        # ── 종합 판단 ──
        lines.append(f"\n━━━━━━━━━━━━━━━━")
        total = len(ready) + len(squeezing) + len(quiet)
        lines.append(f"📋 워치리스트: {total}코인 | "
                     f"임박:{len(ready)} 진행:{len(squeezing)} 조용:{len(quiet)}")

        if len(ready) >= 3:
            lines.append(f"💡 시그널 임박 코인 多 — 집중 모니터링 권장")
        elif len(ready) == 0 and len(squeezing) <= 3:
            lines.append(f"💡 전반적 조용 — 인내심 구간")

        msg = '\n'.join(lines)
        self.tg.send(msg)
        print(msg)

        # ── [V9.3] Conf TOP5 분석 (별도 메시지) ──
        self._conf_top5_report()

    def _conf_top5_report(self):
        """워치리스트에서 detect()를 돌려 conf 점수 상위 5개 상세 표시"""
        conf_list = []

        for sym in self.cfg["WATCHLIST"]:
            try:
                ohlcv = self.exchange.fetch_ohlcv(sym, '15m', limit=201)
                df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
                df['Date'] = pd.to_datetime(df['Date'], unit='ms')
                df.set_index('Date', inplace=True)

                now_ts = pd.Timestamp.now(tz='UTC')
                if len(df) > 0:
                    last_start = df.index[-1]
                    if hasattr(last_start, 'tz') and last_start.tz is None:
                        last_start = last_start.tz_localize('UTC')
                    if now_ts < last_start + pd.Timedelta(minutes=15):
                        df = df.iloc[:-1]
                if len(df) < 100: continue

                # detect 시도 (내부 필터 느슨하게 — conf 점수만 보려고)
                # 원본 전략 그대로 사용하되, 결과가 None이면 수동 점수 계산
                result = self.strategy.detect(df)

                short = sym.replace('/USDT', '')

                if result:
                    conf = result.get('confidence', 0)
                    det = result.get('details', {})
                    brk = det.get('breakout', {})
                    sqz = det.get('squeeze', {})
                    reasons = result.get('reasons', [])
                    direction = result.get('direction', '?')

                    conf_list.append({
                        'sym': short, 'conf': conf, 'dir': direction,
                        'reasons': reasons,
                        'sqz_candles': result.get('squeeze_candles', 0),
                        'breakout_atr': brk.get('breakout_atr', 0),
                        'volume_ratio': brk.get('volume_ratio', 0),
                        'adx': sqz.get('adx', 0),
                        'bbw_squeeze': sqz.get('bbw_squeeze', False),
                        'macd': brk.get('macd_aligned', False),
                        'status': 'SIGNAL',
                    })
                else:
                    # detect 실패 — 수동으로 어디까지 도달했는지 분석
                    df_ind = self.strategy._ensure_indicators(df.copy())
                    curr = df_ind.iloc[-1]
                    c = df_ind['Close'].astype(float)
                    ema20 = df_ind.get('ema20', c.ewm(span=12).mean())
                    ema60 = df_ind.get('ema60', c.ewm(span=26).mean())

                    gap = abs(ema20.iloc[-1] - ema60.iloc[-1]) / curr['Close'] * 100
                    adx = curr.get('adx', 0)
                    bbw = curr.get('bbw', 0)
                    bbw_avg = curr.get('bbw_avg', bbw)
                    bbw_sqz = bbw < bbw_avg * 0.7 if bbw_avg > 0 else False
                    rvol = curr.get('rvol', 1.0)

                    # 스퀴즈 카운트
                    sqz_count = 0
                    thr = self.cfg["EMA_GAP_THRESHOLD"]
                    for i in range(len(df_ind)-2, max(len(df_ind)-80, 0), -1):
                        row = df_ind.iloc[i]
                        e12 = ema20.iloc[i] if i < len(ema20) else 0
                        e26 = ema60.iloc[i] if i < len(ema60) else 0
                        g = abs(e12 - e26) / row['Close'] * 100 if row['Close'] > 0 else 999
                        if g <= thr:
                            sqz_count += 1
                        else:
                            break

                    # 돌파 강도 (마지막 봉)
                    prev = df_ind.iloc[-2]
                    atr = curr.get('atr', 1)
                    move = abs(curr['Close'] - prev['Close'])
                    brk_atr = move / atr if atr > 0 else 0

                    # 가상 conf 점수 계산 (detect 안 됐어도)
                    score = 0; reasons = []
                    if sqz_count >= 20: score += 3; reasons.append(f"장기sqz {sqz_count}")
                    elif sqz_count >= 10: score += 2; reasons.append(f"sqz {sqz_count}")
                    elif sqz_count >= 1: score += 1; reasons.append(f"단기sqz {sqz_count}")
                    else: reasons.append("sqz 없음")
                    if bbw_sqz: score += 2; reasons.append("BB수축✅")
                    else: reasons.append("BB수축❌")
                    if brk_atr >= 2.0: score += 2; reasons.append(f"돌파{brk_atr:.1f}ATR")
                    elif brk_atr >= 1.5: score += 1; reasons.append(f"돌파{brk_atr:.1f}ATR")
                    else: reasons.append(f"돌파약{brk_atr:.1f}ATR")
                    if rvol >= 2.0: score += 2; reasons.append(f"vol×{rvol:.1f}")
                    elif rvol >= 1.3: score += 1; reasons.append(f"vol×{rvol:.1f}")
                    else: reasons.append(f"vol부족×{rvol:.1f}")

                    # MACD (직접 계산 — _ensure_indicators가 macdh를 안 만듦)
                    macd_line = c.ewm(span=12).mean() - c.ewm(span=26).mean()
                    macd_sig = macd_line.ewm(span=9).mean()
                    macdh_s = macd_line - macd_sig
                    if len(macdh_s) >= 2:
                        mh = macdh_s.iloc[-1]; mh_prev = macdh_s.iloc[-2]
                        if abs(mh - mh_prev) > 1e-9:
                            score += 1; reasons.append("MACD✅")
                        else: reasons.append("MACD❌")
                    else: reasons.append("MACD❌")

                    if adx > 25: score += 1; reasons.append(f"ADX{adx:.0f}✅")
                    else: reasons.append(f"ADX{adx:.0f}❌")

                    virtual_conf = min(100, int(score / 12 * 100))

                    # 왜 detect가 실패했는지
                    fail_reason = ""
                    if sqz_count < self.cfg["MIN_SQUEEZE_CANDLES"]:
                        fail_reason = f"sqz부족({sqz_count}<15)"
                    elif gap > thr:
                        fail_reason = f"gap초과({gap:.2f}%)"
                    elif brk_atr < 1.5:
                        fail_reason = f"돌파약({brk_atr:.1f}ATR)"
                    elif adx < 15:
                        fail_reason = f"ADX극저({adx:.0f})"
                    else:
                        fail_reason = "BB/vol/방향"

                    conf_list.append({
                        'sym': short, 'conf': virtual_conf, 'dir': '?',
                        'reasons': reasons, 'sqz_candles': sqz_count,
                        'breakout_atr': brk_atr, 'volume_ratio': rvol,
                        'adx': adx, 'bbw_squeeze': bbw_sqz,
                        'macd': False, 'status': f'FAIL({fail_reason})',
                    })

                time.sleep(0.05)
            except:
                continue

        if not conf_list: return

        # 상위 5개 정렬
        conf_list.sort(key=lambda x: -x['conf'])
        top5 = conf_list[:5]

        lines = [f"📊 Conf TOP5 분석"]
        lines.append(f"  (conf ≥70이면 진입 가능, score/12×100)")
        lines.append(f"  ┌{'─'*42}┐")

        for i, c in enumerate(top5):
            icon = "🟢" if c['conf'] >= 70 else "🟡" if c['conf'] >= 50 else "🔴"
            dir_s = c['dir'].upper() if c['dir'] != '?' else '대기'
            lines.append(f"  {icon} {i+1}. {c['sym']} conf:{c['conf']} [{dir_s}] ({c['status']})")
            lines.append(f"     sqz:{c['sqz_candles']}봉 돌파:{c['breakout_atr']:.1f}ATR "
                         f"vol:×{c['volume_ratio']:.1f} ADX:{c['adx']:.0f} "
                         f"BB:{'✅' if c['bbw_squeeze'] else '❌'}")
            # 점수 분해
            reasons_s = ' | '.join(c['reasons'][:4])
            lines.append(f"     {reasons_s}")
            if len(c['reasons']) > 4:
                lines.append(f"     {' | '.join(c['reasons'][4:])}")

        lines.append(f"  └{'─'*42}┘")

        # 전체 conf 분포
        confs = [c['conf'] for c in conf_list]
        n70 = sum(1 for c in confs if c >= 70)
        n60 = sum(1 for c in confs if 60 <= c < 70)
        n50 = sum(1 for c in confs if 50 <= c < 60)
        nlo = sum(1 for c in confs if c < 50)
        lines.append(f"\n  분포: ≥70:{n70} | 60~69:{n60} | 50~59:{n50} | <50:{nlo}")

        msg = '\n'.join(lines)
        self.tg.send(msg)
        print(msg)

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

    def _calc_smart_sl_tp(self, symbol: str, direction: str, entry: float) -> tuple:
        """
        수동 포지션 SL/TP 계산 — 기존 포지션의 평균 비율 참고.
        기존 포지션이 없으면 ATR 기반 기본값 사용.
        """
        # 기존 봇 포지션들의 SL/TP 비율 수집 (수동진입/복구 제외)
        sl_ratios = []
        tp_ratios = []
        for p in self.positions:
            if '수동진입' in (p.reasons or []) or '복구' in (p.reasons or []):
                continue
            if p.entry_price > 0 and p.stop_loss > 0:
                sl_dist = abs(p.entry_price - p.stop_loss) / p.entry_price
                sl_ratios.append(sl_dist)
            if p.entry_price > 0 and p.take_profit > 0:
                tp_dist = abs(p.take_profit - p.entry_price) / p.entry_price
                tp_ratios.append(tp_dist)

        # 평균 비율 사용, 없으면 기본값
        if sl_ratios:
            avg_sl = sum(sl_ratios) / len(sl_ratios)
        else:
            avg_sl = 0.02 * self.cfg.get("SL_RATIO", 1.5)  # ~3%

        if tp_ratios:
            avg_tp = sum(tp_ratios) / len(tp_ratios)
        else:
            avg_tp = 0.05  # 5%

        if direction == "long":
            sl = entry * (1 - avg_sl)
            tp = entry * (1 + avg_tp)
        else:
            sl = entry * (1 + avg_sl)
            tp = entry * (1 - avg_tp)

        print(f"  [Smart SL/TP] {symbol} {direction} 진입:{entry:.6f}")
        print(f"    참고 포지션: {len(sl_ratios)}개 | SL:{avg_sl*100:.2f}% TP:{avg_tp*100:.2f}%")
        print(f"    → SL:{sl:.6f} TP:{tp:.6f}")
        return round(sl, 8), round(tp, 8)

    def runtime_sync(self):
        """
        5분마다 거래소 실제 포지션과 비교.
        거래소 SL/TP가 자동 체결되면 봇이 모르는 문제를 해결.
        봇에는 있는데 거래소에 없으면 → "거래소에서 청산됨" 처리.
        """
        if self.cfg["PAPER_TRADE"]:
            return

        try:
            ex_positions = self.exchange.fetch_positions()
            # 거래소 활성 포지션 키 수집
            ex_keys = set()
            ex_active = {}
            for p in ex_positions:
                contracts = abs(float(p.get('contracts', 0)))
                if contracts <= 0: continue
                sym = self._norm_sym(p.get('symbol', ''))
                side = p.get('side', '').lower()
                key = f"{sym}_{side}"
                ex_keys.add(key)
                ex_active[key] = {
                    'sym': sym, 'side': side,
                    'entry_price': float(p.get('entryPrice', 0)),
                    'contracts': contracts,
                }

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
                        'adx': pos.adx_value, 'vol_ratio': pos.volume_ratio,
                        'gap_pct': pos.gap_pct, 'vol_surge': pos.vol_surge,
                        'squeeze_candles': pos.squeeze_candles, 'rsi': pos.rsi,
                        'time': datetime.now(KST).isoformat(),
                    })

                    icon = "🟢" if pnl > 0 else "🔴"
                    d_s = pos.direction.upper()
                    roe_lev = roe * self.cfg["LEVERAGE"]
                    bal = self.executor.total_balance()
                    self.tg.send(
                        f"{icon} 거래소 자동 청산 감지 | {d_s} {pos.symbol}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"진입: {pos.entry_price:,.6f}\n"
                        f"추정 손익: ${pnl:+,.2f} (현물{roe:+.2f}% ROE{roe_lev:+.0f}%)\n"
                        f"보유: {hh:.1f}시간\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"📋 진입 시그널:\n"
                        f"  conf:{pos.confidence} GAP:{pos.gap_pct:.2f}% sqz:{pos.squeeze_candles}\n"
                        f"  ADX:{pos.adx_value:.0f} vol:×{pos.volume_ratio:.1f} surge:×{pos.vol_surge:.1f}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"거래소 SL/TP가 자동 체결된 것으로 판단\n"
                        f"잔고: ${bal:,.0f}")

                    removed.append(pos)
                    self._alerted_orders.discard(f"{pos.symbol}_{pos.direction}")
                    # 잔여 주문 정리 (TP or SL 남아있을 수 있음)
                    try:
                        self.executor._cancel_all_orders(pos.symbol)
                    except: pass

            for p in removed:
                if p in self.positions:
                    self.positions.remove(p)
            if removed:
                self._save_positions()
                print(f"  런타임 동기화: {len(removed)}개 포지션 거래소 청산 감지")

            # ── 거래소에 있는데 봇에 없음 → 수동 포지션 감지 ──
            bot_keys_now = {f"{self._norm_sym(p.symbol)}_{p.direction}" for p in self.positions}
            for key, ex_info in ex_active.items():
                if key in bot_keys_now:
                    continue
                sym = ex_info['sym']; side = ex_info['side']
                contracts = ex_info['contracts']
                ep = ex_info['entry_price']
                if ep <= 0:
                    continue

                # 기존 포지션 SL/TP 비율 참고
                sl_p, tp_p = self._calc_smart_sl_tp(sym, side, ep)

                # SL/TP 주문 이미 있는지 확인
                has_sl, has_tp = False, False
                try:
                    orders = self.executor._get_open_orders_raw(sym)
                    for oo in (orders or []):
                        sp = float(oo.get('stopPrice', 0))
                        if sp <= 0:
                            continue
                        otype = str(oo.get('type', oo.get('origType', ''))).upper()
                        if 'STOP' in otype and 'PROFIT' not in otype:
                            has_sl = True; sl_p = sp
                        elif 'PROFIT' in otype:
                            has_tp = True; tp_p = sp
                except Exception:
                    pass

                lock_dist = abs(tp_p - ep) * (self.cfg["LOCK_FIBO"] / 8.0)
                lock_roe = lock_dist / ep * 100 if self.cfg["LOCK_FIBO"] > 0 else 0

                pos = Position(
                    symbol=sym, direction=side,
                    entry_price=ep, quantity=contracts,
                    stop_loss=sl_p, take_profit=tp_p,
                    atr=ep * 0.01, entry_time=datetime.now(KST).isoformat(),
                    confidence=0, reasons=['수동진입'],
                    original_sl=sl_p, lock_trigger_roe=lock_roe,
                    profile=self.profile_name)
                self.positions.append(pos)
                self._save_positions()

                # SL/TP 주문 없는 것만 개별 설정 (기존 주문 보호)
                ps = 'LONG' if side == 'long' else 'SHORT'
                sl_side = 'sell' if side == 'long' else 'buy'
                if not has_sl:
                    try:
                        sl_rounded = self.executor.rnd_price(sym, sl_p)
                        self.executor.ex.create_order(
                            sym, "STOP_MARKET", sl_side, contracts,
                            params={"stopPrice": sl_rounded, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        print(f"  ✅ 수동포지션 SL 설정: {sym} @ {sl_rounded}")
                    except Exception as e:
                        print(f"  ⚠️ 수동포지션 SL 설정 실패: {e}")
                if not has_tp:
                    try:
                        tp_rounded = self.executor.rnd_price(sym, tp_p)
                        self.executor.ex.create_order(
                            sym, "TAKE_PROFIT_MARKET", sl_side, contracts,
                            params={"stopPrice": tp_rounded, "positionSide": ps,
                                    "closePosition": True, "priceProtect": "TRUE"})
                        print(f"  ✅ 수동포지션 TP 설정: {sym} @ {tp_rounded}")
                    except Exception as e:
                        print(f"  ⚠️ 수동포지션 TP 설정 실패: {e}")

                sl_dist = abs(ep - sl_p) / ep * 100
                tp_dist = abs(tp_p - ep) / ep * 100
                self.tg.send(
                    f"👤 수동 포지션 감지!\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"{side.upper()} {sym}\n"
                    f"진입: {ep:,.6f} | 수량: {contracts}\n"
                    f"{'🆕 ' if not has_sl else ''}SL: {sl_p:,.6f} ({sl_dist:.1f}%)\n"
                    f"{'🆕 ' if not has_tp else ''}TP: {tp_p:,.6f} ({tp_dist:.1f}%)\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"에이전트 분석에 포함됩니다")

                if self.agent_hook:
                    try:
                        self.agent_hook.on_position_open(pos, {})
                    except Exception:
                        pass

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
        path = self.cfg["POSITIONS_FILE"]
        # [V9.2-FIX6] 저장 전 백업
        if os.path.exists(path):
            try:
                os.replace(path, path + '.bak')
            except: pass
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def _load_positions(self):
        path = self.cfg["POSITIONS_FILE"]
        # [V9.2-FIX6] 메인 파일 실패 시 백업에서 복구
        for try_path in [path, path + '.bak']:
            if not os.path.exists(try_path):
                continue
            try:
                with open(try_path) as f: data = json.load(f)
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
                    src = "백업" if try_path.endswith('.bak') else "메인"
                    print(f"  포지션 복원: {len(self.positions)}개 ({src})")
                return
            except Exception as e:
                print(f"  포지션 로드 실패 ({try_path}): {e}")
                continue
        self.positions = []
        if os.path.exists(path):
            self.tg.send_error(f"포지션 로드 완전 실패 — 수동 확인 필요")

    def _save_history(self, record):
        p = os.path.join(self.cfg["LOG_DIR"], "trade_history_v9.jsonl")
        with open(p, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    @staticmethod
    def _norm_sym(sym):
        """심볼 정규화: BTC/USDT:USDT → BTC/USDT, BTCUSDT → BTC/USDT"""
        s = str(sym).split(':')[0]  # BTC/USDT:USDT → BTC/USDT
        # BTCUSDT → BTC/USDT (슬래시 없는 경우)
        if '/' not in s and s.endswith('USDT'):
            s = s[:-4] + '/USDT'
        return s

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

            # 거래소에 있는데 봇에 없음 → 복구 (워치리스트 코인만!)
            my_watchlist = {self._norm_sym(s) for s in self.cfg.get("WATCHLIST", [])}
            for key, ex in ex_active.items():
                if key in bot_keys: continue
                sym = ex['sym']; ep = ex['entry_price']; qty = ex['contracts']
                if ep <= 0: continue
                # [V9.2-FIX] 내 워치리스트에 없는 코인은 무시 (Bot2 포지션 보호)
                if my_watchlist and self._norm_sym(sym) not in my_watchlist:
                    print(f"  동기화: {sym} — 워치리스트 외 코인 → 무시 (Bot2?)")
                    continue

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
        print(f"\n{'='*50}\n  Convergence Trader V9.2 [모드{self.mode} {self.mode_cfg['desc']}]\n{'='*50}")
        last_scan = 0; last_status = 0; last_verify = 0; last_sync = 0; last_bar_min = -1
        last_market = 0  # [V9.3] 4시간 시장 분석

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
                now_dt = datetime.now(KST)  # [V9.1] manage_positions 후 재계산
                cur_min = now_dt.minute
                bar_boundary = cur_min % 15 == 0 and now_dt.second < 45
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

                # ── [V9.3] 시장 분석 (4시간) ──
                if now - last_market >= 14400:
                    try:
                        self.market_analysis()
                    except Exception as e:
                        print(f"  시장 분석 오류: {e}")
                    last_market = now

                # ── [#9] SL/TP 검증 (30분) ──
                if now - last_verify >= 1800 and self.positions:
                    self.verify_all_orders()
                    last_verify = now

                # ── 거래소 동기화 (5분마다) — SL/TP 자동 청산 + 수동 포지션 감지 ──
                if now - last_sync >= 300:
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
    parser.add_argument('--mode', default='E', choices=['E', 'D'],
                        help='E=Cascade(수익극대화) / D=CasTrail(멘탈안정)')
    parser.add_argument('--profile', default='standard',
                        choices=['standard'])
    args = parser.parse_args()
    cfg = CONFIG.copy()
    if args.live:
        cfg["PAPER_TRADE"] = False
    cfg["_profile"] = args.profile
    cfg["_mode"] = args.mode
    ConvergenceTrader(cfg).run()
