"""
crash_strategy.py — 급락 감지 + 반대매매 + 수익 관리 + 양방향 근거 분석

═══════════════════════════════════════════════════════════════
5가지 핵심 기능:

  1. CrashDetector         — 급락/폭락 실시간 감지
  2. CounterTradeEngine    — 급락 후 반대매매 전략
  3. ProfitManager         — 반익절 + 추가진입 + 재진입
  4. EvidenceAnalyzer      — 롱/숏 양방향 근거 리포트
  5. CrashMonitorPlugin    — smart_monitor_cloud.py 연동 플러그인

사용법 (단독 실행):
  python crash_strategy.py                         # 전체 분석
  python crash_strategy.py --watch SOL ETH         # 급락 실시간 감시
  python crash_strategy.py --evidence BTC SOL      # 양방향 근거 리포트
  python crash_strategy.py --short-report BTC      # 숏 전용 리포트
  python crash_strategy.py --long-report SOL       # 롱 전용 리포트

모니터봇 연동:
  from crash_strategy import CrashMonitorPlugin
  plugin = CrashMonitorPlugin(exchange, cg_client, btc_filter)
  plugin.run_cycle(symbols)   # 메인 루프 안에서 호출

텔레그램 분리:
  - 단독 실행: TELEGRAM_TOKEN_CRASH + TELEGRAM_CHAT_ID_CRASH
  - 모니터봇 연동: 플러그인이 자체 텔레그램으로 전송 (모니터봇 채널과 분리)
═══════════════════════════════════════════════════════════════
"""

import ccxt, pandas as pd, numpy as np, time, os, sys, requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Callable

from indicators_v2 import calculate_indicators, apply_strategies, calculate_mtf_confluence
from coinglass_client import CoinGlassClient
from btc_filter import BTCFilter, BTCContext
from dotenv import load_dotenv
load_dotenv()

KST = timezone(timedelta(hours=9))

# ╔══════════════════════════════════════════════════════════════╗
# ║                    ⚙️  CONFIG (여기서 수정)                    ║
# ╚══════════════════════════════════════════════════════════════╝

CONFIG = {
    # ── API 키 (.env 파일에서 읽음) ──
    "COINGLASS_API_KEY": os.environ.get("COINGLASS_API_KEY", ""),

    # ── 텔레그램 (CRASH 전용 — 모니터봇과 분리) ──
    #    .env에 TELEGRAM_TOKEN_CRASH / TELEGRAM_CHAT_ID_CRASH 설정
    #    미설정 시 MONITOR 토큰으로 fallback
    "TELEGRAM_TOKEN_CRASH":   os.environ.get("TELEGRAM_TOKEN_CRASH",
                              os.environ.get("TELEGRAM_TOKEN_MONITOR", "")),
    "TELEGRAM_CHAT_ID_CRASH": os.environ.get("TELEGRAM_CHAT_ID_CRASH",
                              os.environ.get("TELEGRAM_CHAT_ID", "")),

    # ── 감시 대상 ──
    "WATCH_SYMBOLS": [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    ],

    # ── 급락 감지 (CrashDetector) ──
    "CRASH_TIMEFRAMES":      ["5m", "15m", "1h", "4h"],
    "FLASH_DIP_THRESHOLD":   -3.0,    # FLASH_DIP 최소 하락률(%)
    "CRASH_THRESHOLD":       -5.0,    # CRASH 최소 하락률(%)
    "BLACK_SWAN_THRESHOLD":  -15.0,   # BLACK_SWAN 최소 하락률(%)
    "CRASH_VOL_SPIKE_MIN":   2.0,     # 거래량 최소 배수
    "CRASH_CHECK_INTERVAL":  120,     # 급락 체크 주기 (초)

    # ── 반대매매 (CounterTradeEngine) ──
    "V_BOUNCE_MIN_SCORE":    5,       # V반등 최소 점수
    "DEAD_CAT_MIN_SCORE":    4,       # 데드캣숏 최소 점수
    "LIQ_CASCADE_MIN_SCORE": 5,       # 연쇄청산롱 최소 점수
    "FUNDING_SQ_MIN_SCORE":  4,       # 펀딩스퀴즈 최소 점수

    # ── 수익 관리 (ProfitManager) ──
    "LEVERAGE":              20,
    "TP1_ROE":               15,      # 1차 반익절 ROE(%)
    "TP1_CLOSE_PCT":         50,      # 1차 청산 비율(%)
    "TP2_ROE":               30,      # 2차 반익절 ROE(%)
    "TP2_CLOSE_PCT":         25,      # 2차 청산 비율(%)
    "TP3_ROE":               50,      # 트레일링 전환 ROE(%)
    "ADD_MIN_ROE":           10,      # 추가 진입 최소 ROE(%)
    "ADD_SIZE_PCT":          30,      # 추가 진입 비율(%) (기존 대비)
    "REENTRY_ENABLED":       True,    # 재진입 사용 여부

    # ── 근거 분석 (EvidenceAnalyzer) ──
    "EVIDENCE_TIMEFRAMES":   {"4h": 3, "1h": 2, "15m": 1},  # TF: 가중치
    "STRONG_VERDICT_SCORE":  70,      # STRONG 판정 기준
    "VERDICT_SCORE":         50,      # 일반 판정 기준
    "WEAK_VERDICT_SCORE":    35,      # WEAK 판정 기준
    "EVIDENCE_INTERVAL":     3600,    # 근거 분석 주기 (초) — 모니터봇용

    # ── 모니터봇 연동 ──
    "PLUGIN_CRASH_ENABLED":  True,    # 급락 감시 활성화
    "PLUGIN_EVIDENCE_ENABLED": True,  # 근거 분석 활성화
    "PLUGIN_EVIDENCE_SYMBOLS": [],    # 비우면 WATCH_SYMBOLS 사용
}


# ╔══════════════════════════════════════════════════════════════╗
# ║          📨 텔레그램 — CRASH 전용 채널                          ║
# ╚══════════════════════════════════════════════════════════════╝

class TelegramCrash:
    """
    CRASH 전용 텔레그램 전송
    - 단독 실행: CONFIG에서 CRASH 토큰/채팅ID 사용
    - 모니터봇 연동: 플러그인이 자체 인스턴스 생성 → 모니터봇 채널과 분리
    """
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or CONFIG["TELEGRAM_TOKEN_CRASH"]
        self.chat_id = chat_id or CONFIG["TELEGRAM_CHAT_ID_CRASH"]
        self.ok = bool(self.token and self.chat_id and self.token != "YOUR_TELEGRAM_TOKEN")

    def send(self, msg: str):
        if not self.ok:
            print(f"  [CRASH TG 미연결] {msg[:100]}...")
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000]}, timeout=10)
        except Exception as e:
            print(f"  ⚠️ CRASH TG전송실패: {e}")


# ╔══════════════════════════════════════════════════════════════╗
# ║        🔧 CoinGlass 데이터 안전 추출 헬퍼                       ║
# ╚══════════════════════════════════════════════════════════════╝

def _cg_extract(cg_data: dict) -> dict:
    """
    CoinGlass get_full_analysis() 리턴값에서 필요한 값을 안전하게 추출

    get_full_analysis() 리턴 구조 (중첩 딕셔너리):
      ['oi']           → {'change_pct':..., 'trend':...} 또는 None
      ['funding']      → {'current':..., 'signal':...} 또는 None
      ['global_ls']    → {'long_pct':..., 'short_pct':..., 'ratio':...} 또는 None
      ['top_ls']       → {'long_pct':..., 'short_pct':...} 또는 None
      ['liquidation']  → {'dominant':..., 'long_liquidation_usd':...} 또는 None
      ['taker']        → {'sell_buy_ratio':..., 'signal':...} 또는 None
      ['short_conviction'] → int (최상위 키)

    ★ 이전 버그: cg.get('funding_rate') 등 플랫 키로 접근 → 항상 기본값 리턴
    ★ 수정: 중첩 딕셔너리에서 안전하게 꺼냄
    """
    if not cg_data:
        return {
            'funding_rate': 0, 'funding_signal': 'NEUTRAL',
            'oi_change_pct': 0, 'oi_trend': 'FLAT',
            'ls_long_pct': 50, 'ls_short_pct': 50, 'ls_ratio': 1.0,
            'top_ls_long': 50, 'top_ls_short': 50,
            'taker_ratio': 1.0, 'taker_signal': 'NEUTRAL',
            'liq_dominant': '', 'liq_long_usd': 0, 'liq_short_usd': 0,
            'short_conviction': 0, 'short_reasons': [],
        }

    oi = cg_data.get('oi') or {}
    fund = cg_data.get('funding') or {}
    gls = cg_data.get('global_ls') or {}
    tls = cg_data.get('top_ls') or {}
    liq = cg_data.get('liquidation') or {}
    tak = cg_data.get('taker') or {}

    return {
        'funding_rate':      fund.get('current', 0),
        'funding_signal':    fund.get('signal', 'NEUTRAL'),
        'oi_change_pct':     oi.get('change_pct', 0),
        'oi_trend':          oi.get('trend', 'FLAT'),
        'ls_long_pct':       gls.get('long_pct', 50),
        'ls_short_pct':      gls.get('short_pct', 50),
        'ls_ratio':          gls.get('ratio', 1.0),
        'top_ls_long':       tls.get('long_pct', 50),
        'top_ls_short':      tls.get('short_pct', 50),
        'taker_ratio':       tak.get('sell_buy_ratio', 1.0),
        'taker_signal':      tak.get('signal', 'NEUTRAL'),
        'liq_dominant':      liq.get('dominant', ''),
        'liq_long_usd':      liq.get('long_liquidation_usd', 0),
        'liq_short_usd':     liq.get('short_liquidation_usd', 0),
        'short_conviction':  cg_data.get('short_conviction', 0),
        'short_reasons':     cg_data.get('short_reasons', []),
    }


# ╔══════════════════════════════════════════════════════════════╗
# ║           🔧 공통 초기화 헬퍼                                    ║
# ╚══════════════════════════════════════════════════════════════╝

def init_components(cfg: dict = None):
    """
    exchange, coinglass, btc_filter 초기화

    ★ BTCFilter 연결 조건:
      BTCFilter(cg_client, exchange) — cg_client가 필수 첫 번째 인자
      → CoinGlass API 키가 있어야 생성 가능
      → .env 에 COINGLASS_API_KEY=키값 추가하면 자동 활성화
    """
    cfg = cfg or CONFIG
    ex = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})

    cg_key = cfg.get("COINGLASS_API_KEY", "")
    cg = None
    btc_f = None

    if cg_key:
        cg = CoinGlassClient(cg_key)
        btc_f = BTCFilter(cg, ex, cache_seconds=120, fast_cache_seconds=30)
        print("  ✅ CoinGlass + BTCFilter 연결 완료")
    else:
        print("  ⚠️ COINGLASS_API_KEY 미설정 → .env에 추가 필요")

    return ex, cg, btc_f


# ═══════════════════════════════════════════════════════════════
#  1. 급락/폭락 감지기 (CrashDetector)
# ═══════════════════════════════════════════════════════════════

@dataclass
class CrashEvent:
    symbol: str; severity: str; drop_pct: float; drop_duration_min: int
    lowest_price: float; pre_crash_price: float; current_price: float
    bounce_pct: float; volume_spike: float; liq_cascade: bool
    oi_collapse_pct: float; rsi: float
    reasons: List[str] = field(default_factory=list)
    counter_signal: str = ""; timestamp: str = ""


class CrashDetector:
    def __init__(self, exchange=None, cg_client=None, cfg: dict = None):
        self.cfg = cfg or CONFIG
        self.ex = exchange or ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.cg = cg_client

    def detect(self, symbol: str) -> Optional[CrashEvent]:
        worst = None
        for tf in self.cfg["CRASH_TIMEFRAMES"]:
            ev = self._check_tf(symbol, tf)
            if ev and (worst is None or ev.drop_pct < worst.drop_pct):
                worst = ev
        if worst:
            self._enrich(worst)
            worst.counter_signal = self._judge(worst)
        return worst

    def _check_tf(self, symbol, tf) -> Optional[CrashEvent]:
        try:
            ohlcv = self.ex.fetch_ohlcv(symbol, timeframe=tf, limit=50)
            df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
            df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        except: return None
        if len(df) < 10: return None

        price = df.iloc[-1]['Close']
        worst_drop = 0; peak_price = price; peak_idx = len(df) - 1
        for lb in [5, 10, 20, 30]:
            if lb > len(df): continue
            w = df.iloc[-lb:]
            lp = w['High'].max(); li = w['High'].idxmax()
            d = (price - lp) / lp * 100
            if d < worst_drop:
                worst_drop = d; peak_price = lp; peak_idx = li

        if worst_drop > self.cfg["FLASH_DIP_THRESHOLD"]: return None

        since = df.loc[peak_idx:]
        lowest = since['Low'].min()
        lowest_drop = (lowest - peak_price) / peak_price * 100
        bounce = (price - lowest) / lowest * 100 if lowest > 0 else 0
        tf_min = {'1m':1,'3m':3,'5m':5,'15m':15,'30m':30,'1h':60,'4h':240}
        dur = len(since) * tf_min.get(tf, 15)
        avg_v = df.iloc[:-5]['Volume'].mean() if len(df)>5 else df['Volume'].mean()
        vol_spike = df.iloc[-5:]['Volume'].mean() / (avg_v + 1e-9)

        if lowest_drop <= self.cfg["BLACK_SWAN_THRESHOLD"]:     sev = "BLACK_SWAN"
        elif lowest_drop <= self.cfg["CRASH_THRESHOLD"]:        sev = "CRASH"
        else:                                                    sev = "FLASH_DIP"

        df_i = calculate_indicators(df.set_index('Date').copy())
        rsi = float(df_i.iloc[-1].get('rsi_14', 50)) if df_i is not None else 50

        reasons = [f"고점 대비 {lowest_drop:.1f}% 하락", f"소요: {dur}분"]
        if vol_spike > 3: reasons.append(f"거래량 {vol_spike:.1f}x 폭발")
        if bounce > 1:    reasons.append(f"저점서 {bounce:.1f}% 반등")
        if rsi < 25:       reasons.append(f"RSI {rsi:.0f} 극과매도")

        return CrashEvent(
            symbol=symbol, severity=sev, drop_pct=round(lowest_drop,2),
            drop_duration_min=dur, lowest_price=lowest, pre_crash_price=peak_price,
            current_price=price, bounce_pct=round(bounce,2),
            volume_spike=round(vol_spike,1), liq_cascade=False, oi_collapse_pct=0,
            rsi=round(rsi,1), reasons=reasons, timestamp=datetime.now(KST).isoformat())

    def _enrich(self, ev):
        """★ 수정: 중첩 딕셔너리 올바르게 접근"""
        if not self.cg: return
        base = ev.symbol.split('/')[0]
        try:
            oi_data = self.cg.get_oi_change(base, "1h")
            if oi_data:
                ev.oi_collapse_pct = oi_data['change_pct']
                if oi_data['change_pct'] < -5:
                    ev.reasons.append(f"OI 급감 {oi_data['change_pct']:+.1f}%")
                    ev.liq_cascade = True
        except: pass
        try:
            liq_data = self.cg.get_recent_liquidation_pressure(base)
            if liq_data and liq_data['dominant'] == 'LONG_LIQUIDATION':
                ev.liq_cascade = True
                ev.reasons.append(f"롱 연쇄청산 ${liq_data['long_liquidation_usd']/1e6:.1f}M")
        except: pass

    def _judge(self, ev) -> str:
        if ev.severity == "BLACK_SWAN" and ev.bounce_pct < 3: return "AVOID"
        if ev.liq_cascade and ev.oi_collapse_pct < -8: return "WAIT"
        if ev.rsi < 20 and ev.bounce_pct > 1.5 and ev.volume_spike > 2: return "SCALP_LONG"
        if ev.severity == "CRASH" and ev.bounce_pct > 2 and ev.oi_collapse_pct > -5: return "SWING_LONG"
        if ev.severity == "FLASH_DIP" and ev.rsi < 30: return "SCALP_LONG"
        return "WAIT"

    @staticmethod
    def format_event(ev) -> str:
        si = {"FLASH_DIP":"⚡","CRASH":"💥","BLACK_SWAN":"☠️"}
        ci = {"WAIT":"⏳ 대기","SCALP_LONG":"🎯 스캘핑 롱","SWING_LONG":"🟢 스윙 롱","AVOID":"🚫 금지"}
        lines = [
            f"{si.get(ev.severity,'⚡')} [{ev.severity}] {ev.symbol}",
            f"📉 하락: {ev.drop_pct:.1f}% ({ev.drop_duration_min}분)",
            f"💰 {ev.pre_crash_price:.4f}→{ev.lowest_price:.4f} (현재 {ev.current_price:.4f})",
            f"📊 거래량: {ev.volume_spike:.1f}x | RSI: {ev.rsi:.0f} | 반등: {ev.bounce_pct:.1f}%",
        ]
        if ev.liq_cascade: lines.append(f"🔥 연쇄청산! OI {ev.oi_collapse_pct:+.1f}%")
        lines.append(f"🎯 판단: {ci.get(ev.counter_signal,'⏳')}")
        for r in ev.reasons[:5]: lines.append(f"  • {r}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  2. 반대매매 전략 엔진 (CounterTradeEngine)
# ═══════════════════════════════════════════════════════════════

@dataclass
class CounterTrade:
    strategy: str; direction: str; entry_price: float; stop_loss: float
    tp1: float; tp2: float; tp3: float
    risk_reward: float; confidence: int; hold_time: str
    reasons: List[str] = field(default_factory=list)


class CounterTradeEngine:
    def __init__(self, exchange=None, cg_client=None, cfg: dict = None):
        self.cfg = cfg or CONFIG
        self.ex = exchange or ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.cg = cg_client

    def analyze(self, symbol, crash=None) -> List[CounterTrade]:
        trades = []
        try:
            ohlcv = self.ex.fetch_ohlcv(symbol, '15m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
            df['Date'] = pd.to_datetime(df['Date'], unit='ms'); df.set_index('Date', inplace=True)
            df = calculate_indicators(df)
            if df is None or len(df) < 30: return trades
        except: return trades

        # ★ 수정: _cg_extract()로 안전하게 추출
        raw_cg = self.cg.get_full_analysis(symbol.split('/')[0]) if self.cg else {}
        cg = _cg_extract(raw_cg)

        for fn in [self._v_bounce, self._dead_cat, self._liq_cascade, self._funding_sq]:
            t = fn(df, crash, cg)
            if t: trades.append(t)
        trades.sort(key=lambda t: t.confidence, reverse=True)
        return trades

    def _v_bounce(self, df, crash, cg) -> Optional[CounterTrade]:
        L = df.iloc[-1]; P = df.iloc[-2]; p = L['Close']; a = L.get('atr', p*0.015); rsi = L.get('rsi_14',50)
        s = 0; R = []
        if rsi<20:   s+=3; R.append(f"RSI {rsi:.0f} 극과매도")
        elif rsi<28: s+=2; R.append(f"RSI {rsi:.0f} 과매도")
        elif rsi<35: s+=1; R.append(f"RSI {rsi:.0f}")
        if L['Close']>L['Open']: s+=1; R.append("반등 양봉")
        vs=[df.iloc[i]['Volume'] for i in range(-5,0)]; pk=vs.index(max(vs))
        if pk<3 and vs[-1]<max(vs)*0.6: s+=2; R.append("거래량 절정 후 감소")
        lw=min(L['Open'],L['Close'])-L['Low']; bd=abs(L['Close']-L['Open'])
        if bd>0 and lw>bd*2: s+=1; R.append("긴 아래꼬리")
        if L.get('macdh',0)>P.get('macdh',0) and P.get('macdh',0)<0: s+=1; R.append("MACD 반등")
        if s < self.cfg["V_BOUNCE_MIN_SCORE"]: return None
        sl=L['Low']-a*0.5; t1=p+a*1.5; t2=p+a*3; t3=p+a*5
        rr=(t1-p)/abs(p-sl) if abs(p-sl)>0 else 0
        return CounterTrade("V_BOUNCE","long",round(p,6),round(sl,6),round(t1,6),round(t2,6),round(t3,6),round(rr,2),min(95,40+s*7),"15분~2시간",R)

    def _dead_cat(self, df, crash, cg) -> Optional[CounterTrade]:
        L = df.iloc[-1]; p = L['Close']; a = L.get('atr', p*0.015); rsi = L.get('rsi_14',50)
        s = 0; R = []
        pk = df.iloc[-20:]['High'].max(); drp = (p-pk)/pk*100
        if drp > -3: return None
        lo = df.iloc[-10:]['Low'].min(); bn = (p-lo)/lo*100; rt = bn/abs(drp)*100 if drp!=0 else 0
        if 20<rt<50: s+=2; R.append(f"약한 반등 ({rt:.0f}% 되돌림)")
        elif rt<20: return None
        dv=df.iloc[-15:-5]['Volume'].mean(); bv=df.iloc[-5:]['Volume'].mean()
        if bv<dv*0.6: s+=2; R.append("반등 거래량 부족")
        if 35<rsi<55: s+=1; R.append(f"RSI {rsi:.0f} 중간 정체")
        uw=L['High']-max(L['Open'],L['Close']); rng=L['High']-L['Low']
        if rng>0 and uw/rng>0.5: s+=1; R.append("긴 윗꼬리")

        # ★ 수정: cg 플랫 딕셔너리로 접근
        if cg['oi_change_pct'] < -3: s+=1; R.append(f"OI 미회복 {cg['oi_change_pct']:+.1f}%")

        if s < self.cfg["DEAD_CAT_MIN_SCORE"]: return None
        sl=L['High']+a*0.5; t1=p-a*1.5; t2=lo+a*0.3; t3=lo-a*0.5
        rr=abs(p-t1)/abs(sl-p) if abs(sl-p)>0 else 0
        return CounterTrade("DEAD_CAT_SHORT","short",round(p,6),round(sl,6),round(t1,6),round(t2,6),round(t3,6),round(rr,2),min(90,35+s*8),"30분~4시간",R)

    def _liq_cascade(self, df, crash, cg) -> Optional[CounterTrade]:
        """★ 수정: cg 플랫 딕셔너리로 접근 → 이제 정상 작동"""
        L = df.iloc[-1]; p = L['Close']; a = L.get('atr', p*0.015); rsi = L.get('rsi_14',50)
        s = 0; R = []

        oi = cg['oi_change_pct']
        if oi<-10:   s+=3; R.append(f"OI 대량감소 {oi:+.1f}%")
        elif oi<-5:  s+=2; R.append(f"OI 감소 {oi:+.1f}%")
        else: return None

        ll = cg['liq_long_usd']
        if ll>5e6: s+=2; R.append(f"롱 대량청산 ${ll/1e6:.1f}M")
        elif ll>1e6: s+=1; R.append(f"롱 청산 ${ll/1e6:.1f}M")

        if rsi<22: s+=2; R.append(f"RSI {rsi:.0f}")
        elif rsi<30: s+=1; R.append(f"RSI {rsi:.0f}")

        fr = cg['funding_rate']
        if fr < -0.0003: s+=1; R.append(f"펀딩 음수 {fr*100:.4f}%")

        if s < self.cfg["LIQ_CASCADE_MIN_SCORE"]: return None
        sl=df.iloc[-10:]['Low'].min()-a*0.3; t1=p+a*2; t2=p+a*4; t3=p+a*6
        rr=(t1-p)/abs(p-sl) if abs(p-sl)>0 else 0
        return CounterTrade("LIQ_CASCADE_LONG","long",round(p,6),round(sl,6),round(t1,6),round(t2,6),round(t3,6),round(rr,2),min(95,40+s*6),"1~6시간 (최고수익률)",R)

    def _funding_sq(self, df, crash, cg) -> Optional[CounterTrade]:
        """★ 수정: cg 플랫 딕셔너리로 접근 → 이제 정상 작동"""
        L = df.iloc[-1]; p = L['Close']; a = L.get('atr', p*0.015)
        s = 0; R = []

        f = cg['funding_rate']
        if f<-0.0005: s+=3; R.append(f"펀딩 극음수 {f*100:.4f}%")
        elif f<-0.0003: s+=2; R.append(f"펀딩 음수 {f*100:.4f}%")
        else: return None

        ls = cg['ls_short_pct']
        if ls>60: s+=2; R.append(f"숏 비율 {ls:.0f}%")
        elif ls>55: s+=1; R.append(f"숏 비율 {ls:.0f}%")

        if cg['taker_ratio'] < 0.85: s+=1; R.append(f"Taker 매수 전환")

        if s < self.cfg["FUNDING_SQ_MIN_SCORE"]: return None
        sl=p-a*2; t1=p+a*1.5; t2=p+a*3; t3=p+a*5
        rr=(t1-p)/abs(p-sl) if abs(p-sl)>0 else 0
        return CounterTrade("FUNDING_SQUEEZE","long",round(p,6),round(sl,6),round(t1,6),round(t2,6),round(t3,6),round(rr,2),min(90,35+s*8),"30분~3시간",R)

    @staticmethod
    def format_trade(t) -> str:
        ic = {"V_BOUNCE":"🔄","DEAD_CAT_SHORT":"🐱💀","LIQ_CASCADE_LONG":"💥🟢","FUNDING_SQUEEZE":"🔥🟢"}
        d = "🟢롱" if t.direction=="long" else "🔴숏"
        def fmt(p): return f"{p:,.4f}" if p<10 else f"{p:,.2f}"
        lines = [
            f"{ic.get(t.strategy,'🎯')} [{t.strategy}] {d}",
            f"확신: {t.confidence}/100 | R:R {t.risk_reward}:1 | {t.hold_time}",
            f"📍진입: {fmt(t.entry_price)} 🛡️SL: {fmt(t.stop_loss)}",
            f"🎯 TP1:{fmt(t.tp1)}(50%) TP2:{fmt(t.tp2)}(30%) TP3:{fmt(t.tp3)}(20%)",
        ]
        for r in t.reasons: lines.append(f"  • {r}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  3. 수익 관리 시스템 (ProfitManager)
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProfitAction:
    action: str; pct_to_close: int; new_sl: float; reason: str
    re_entry_price: float = 0.0; re_entry_sl: float = 0.0

class ProfitManager:
    def __init__(self, cfg: dict = None):
        c = cfg or CONFIG; self.lev = c["LEVERAGE"]; self.cfg = c
        self.levels = [
            {'roe': c["TP1_ROE"], 'close': c["TP1_CLOSE_PCT"], 'sl_roe': 0, 'label': '1차 반익절'},
            {'roe': c["TP2_ROE"], 'close': c["TP2_CLOSE_PCT"], 'sl_roe': c["TP1_ROE"], 'label': '2차 반익절'},
            {'roe': c["TP3_ROE"], 'close': 0, 'sl_roe': c["TP2_ROE"], 'label': '트레일링 전환'},
        ]

    def evaluate(self, direction, entry, current, current_sl, atr, closed_pct=0, df=None) -> ProfitAction:
        spot = (current-entry)/entry*100 if direction=="long" else (entry-current)/entry*100
        roe = spot * self.lev
        if roe < -30: return ProfitAction("FULL_EXIT",100,current_sl,f"🔴 ROE {roe:+.1f}%")
        if roe < 0: return ProfitAction("HOLD",0,current_sl,f"⏳ ROE {roe:+.1f}%")

        cum = [self.levels[0]['close'], self.levels[0]['close']+self.levels[1]['close'],
               self.levels[0]['close']+self.levels[1]['close']]
        for i, lv in enumerate(self.levels):
            if roe >= lv['roe'] and closed_pct < cum[i]:
                tc = cum[i] - closed_pct
                if direction=="long": ns = max(entry*(1+lv['sl_roe']/100/self.lev), current_sl)
                else: ns = min(entry*(1-lv['sl_roe']/100/self.lev), current_sl)
                return ProfitAction("PARTIAL_TP",tc,round(ns,6),
                    f"🎯 {lv['label']} (ROE {roe:+.1f}%) {tc}%청산 SL→ROE{lv['sl_roe']}%")

        if df is not None and roe > self.cfg["ADD_MIN_ROE"] and closed_pct == 0:
            add = self._check_add(df, direction, atr)
            if add: return add

        m = 2.5 if roe > 20 else 1.5
        ts = current-atr*m if direction=="long" else current+atr*m
        if direction=="long": ts=max(ts,current_sl)
        else: ts=min(ts,current_sl)
        return ProfitAction("TIGHTEN_TS",0,round(ts,6),f"📈 ROE {roe:+.1f}% T-Stop")

    def _check_add(self, df, direction, atr):
        L = df.iloc[-1]; p = L['Close']; rsi = L.get('rsi_14',50); ema20 = L.get('close_20_ema',p)
        near = abs(p-ema20)/ema20*100 < 0.5; pct = self.cfg["ADD_SIZE_PCT"]
        if direction=="long" and 38<=rsi<=55 and near:
            return ProfitAction("ADD",0,round(p-atr*1.5,6),f"➕ 롱 추가: RSI{rsi:.0f}+EMA20 | {pct}%",
                re_entry_price=round(p,6), re_entry_sl=round(p-atr*1.5,6))
        if direction=="short" and 50<=rsi<=65 and near:
            return ProfitAction("ADD",0,round(p+atr*1.5,6),f"➕ 숏 추가: RSI{rsi:.0f}+EMA20 | {pct}%",
                re_entry_price=round(p,6), re_entry_sl=round(p+atr*1.5,6))
        return None

    def check_re_entry(self, df, prev_dir, prev_entry, prev_exit, atr):
        if not self.cfg["REENTRY_ENABLED"] or df is None or len(df)<30: return None
        L=df.iloc[-1]; p=L['Close']; rsi=L.get('rsi_14',50)
        e20=L.get('close_20_ema',p); e60=L.get('close_60_ema',p)
        if prev_dir=="long" and prev_exit<=prev_entry: return None
        if prev_dir=="short" and prev_exit>=prev_entry: return None
        if prev_dir=="long":
            if e20<=e60 or not(35<=rsi<=55) or p>prev_exit: return None
            return ProfitAction("RE_ENTRY",0,round(p-atr*1.5,6),
                f"🔄 롱 재진입: 정배열+RSI{rsi:.0f}", re_entry_price=round(p,6), re_entry_sl=round(p-atr*1.5,6))
        else:
            if e20>=e60 or not(50<=rsi<=65) or p<prev_exit: return None
            return ProfitAction("RE_ENTRY",0,round(p+atr*1.5,6),
                f"🔄 숏 재진입: 역배열+RSI{rsi:.0f}", re_entry_price=round(p,6), re_entry_sl=round(p+atr*1.5,6))

    @staticmethod
    def format_action(a):
        ic={"HOLD":"⏳","PARTIAL_TP":"🎯","TIGHTEN_TS":"📈","ADD":"➕","RE_ENTRY":"🔄","FULL_EXIT":"🔴"}
        def fmt(p): return f"{p:,.4f}" if p<10 else f"{p:,.2f}"
        lines=[f"{ic.get(a.action,'📋')} [{a.action}] {a.reason}"]
        if a.pct_to_close>0: lines.append(f"  💰 {a.pct_to_close}% 청산")
        if a.new_sl: lines.append(f"  🛡️ SL → {fmt(a.new_sl)}")
        if a.re_entry_price>0: lines.append(f"  📍 진입: {fmt(a.re_entry_price)}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  4. 양방향 근거 분석기 (EvidenceAnalyzer)
# ═══════════════════════════════════════════════════════════════

@dataclass
class Evidence:
    symbol: str; direction: str; verdict: str; total_score: int
    technical_score: int; onchain_score: int; macro_score: int
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class EvidenceAnalyzer:
    """
    롱/숏 동일 프레임워크 (direction 파라미터로 자동 반전)
    [기술적 40점] [온체인 35점] [매크로 25점]
    """

    def __init__(self, exchange=None, cg_client=None, btc_filter=None, cfg: dict = None):
        self.cfg = cfg or CONFIG
        self.ex = exchange or ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.cg = cg_client; self.btc = btc_filter

    def analyze(self, symbol: str, direction: str = "short") -> Evidence:
        ev = Evidence(symbol=symbol, direction=direction, verdict="", total_score=0,
                      technical_score=0, onchain_score=0, macro_score=0)
        self._tech(ev, symbol, direction)
        self._onchain(ev, symbol.split('/')[0], direction)
        self._macro(ev, direction)
        ev.total_score = ev.technical_score + ev.onchain_score + ev.macro_score
        S = self.cfg["STRONG_VERDICT_SCORE"]; G = self.cfg["VERDICT_SCORE"]; W = self.cfg["WEAK_VERDICT_SCORE"]
        D = direction.upper()
        if ev.total_score >= S:   ev.verdict = f"STRONG_{D}"
        elif ev.total_score >= G: ev.verdict = D
        elif ev.total_score >= W: ev.verdict = f"WEAK_{D}"
        else:                     ev.verdict = f"NO_{D}"
        return ev

    def analyze_both(self, symbol: str) -> Dict[str, Evidence]:
        return {"long": self.analyze(symbol, "long"), "short": self.analyze(symbol, "short")}

    def _tech(self, ev, symbol, direction):
        score = 0; tfs = self.cfg["EVIDENCE_TIMEFRAMES"]; tf_data = {}; is_long = direction == "long"
        for tf, w in tfs.items():
            try:
                ohlcv = self.ex.fetch_ohlcv(symbol, tf, limit=100)
                df = pd.DataFrame(ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
                df['Date'] = pd.to_datetime(df['Date'], unit='ms'); df.set_index('Date', inplace=True)
                df = calculate_indicators(df)
                if df is None or len(df) < 30: continue
                df = apply_strategies(df); tf_data[tf] = df
                L = df.iloc[-1]; P = df.iloc[-2]
                e20=L.get('close_20_ema',0); e60=L.get('close_60_ema',0)
                rsi=L.get('rsi_14',50); macdh=L.get('macdh',0); mp=P.get('macdh',0); adx=L.get('adx',0)

                if is_long and e20 > e60:
                    pts=3*w; score+=pts; ev.reasons.append(f"[{tf}] EMA 정배열 +{pts}")
                elif not is_long and e20 < e60:
                    pts=3*w; score+=pts; ev.reasons.append(f"[{tf}] EMA 역배열 +{pts}")

                if is_long and rsi < 35:
                    pts=2*w; score+=pts; ev.reasons.append(f"[{tf}] RSI {rsi:.0f} 과매도 +{pts}")
                elif is_long and 40<rsi<60 and P.get('rsi_14',50)<rsi:
                    pts=1*w; score+=pts; ev.reasons.append(f"[{tf}] RSI {rsi:.0f}↑ +{pts}")
                elif not is_long and rsi > 70:
                    pts=2*w; score+=pts; ev.reasons.append(f"[{tf}] RSI {rsi:.0f} 과매수 +{pts}")
                elif not is_long and 55<rsi<70 and P.get('rsi_14',50)>rsi:
                    pts=1*w; score+=pts; ev.reasons.append(f"[{tf}] RSI {rsi:.0f}↓ +{pts}")

                if is_long and macdh>0 and macdh>mp:
                    pts=2*w; score+=pts; ev.reasons.append(f"[{tf}] MACD 상승가속 +{pts}")
                elif not is_long and macdh<0 and macdh<mp:
                    pts=2*w; score+=pts; ev.reasons.append(f"[{tf}] MACD 하락가속 +{pts}")

                trend_ok = (is_long and e20>e60) or (not is_long and e20<e60)
                if adx > 25 and trend_ok:
                    pts=2*w; score+=pts
                    ev.reasons.append(f"[{tf}] 강한 {'상승' if is_long else '하락'}추세 ADX{adx:.0f} +{pts}")
                time.sleep(0.2)
            except: continue

        # 15분봉 캔들 패턴
        if '15m' in tf_data:
            df=tf_data['15m']; L=df.iloc[-1]; P=df.iloc[-2]
            if is_long:
                if L['Close']>L['Open'] and P['Close']<P['Open']:
                    if L['Close']>P['Open'] and L['Open']<P['Close']:
                        score+=3; ev.reasons.append("상승 장악형 +3")
                lw=min(L['Open'],L['Close'])-L['Low']; rng=L['High']-L['Low']
                if rng>0 and lw/rng>0.6: score+=2; ev.reasons.append("긴 아래꼬리 +2")
            else:
                if L['Close']<L['Open'] and P['Close']>P['Open']:
                    if L['Close']<P['Open'] and L['Open']>P['Close']:
                        score+=3; ev.reasons.append("하락 장악형 +3")
                uw=L['High']-max(L['Open'],L['Close']); rng=L['High']-L['Low']
                if rng>0 and uw/rng>0.6: score+=2; ev.reasons.append("긴 윗꼬리 +2")
            if 'boll_ub' in df.columns:
                if is_long and P['Low']<P.get('boll_lb',0) and L['Close']>L.get('boll_lb',0):
                    score+=2; ev.reasons.append("BB 하단 반등 +2")
                elif not is_long and P['High']>P.get('boll_ub',0) and L['Close']<L.get('boll_ub',0):
                    score+=2; ev.reasons.append("BB 상단 이탈→반락 +2")

        # MTF 합의
        if len(tf_data) >= 2:
            rs = {}
            for tf, df in tf_data.items():
                l=df.iloc[-1]; e2=l.get('close_20_ema',0); e6=l.get('close_60_ema',0); a=l.get('adx',0)
                rs[tf]={'trend':"BULL" if e2>e6 and a>20 else ("BEAR" if e2<e6 and a>20 else "NEUTRAL")}
            mtf = calculate_mtf_confluence(rs)
            tgt = ['FULL_BULL','PARTIAL_BULL'] if is_long else ['FULL_BEAR','PARTIAL_BEAR']
            bad = ['FULL_BEAR','PARTIAL_BEAR'] if is_long else ['FULL_BULL','PARTIAL_BULL']
            if mtf['confluence'] in tgt: score+=3; ev.reasons.append(f"MTF 합의 ({mtf['confluence']}) +3")
            elif mtf['confluence'] in bad:
                ev.warnings.append(f"⚠️ MTF 반대 합의 ({mtf['confluence']})")
        ev.technical_score = min(40, score)

    def _onchain(self, ev, base, direction):
        """★ 수정: _cg_extract()로 올바른 키 접근"""
        if not self.cg:
            ev.warnings.append("⚠️ CoinGlass 미연결 (COINGLASS_API_KEY 필요)")
            return

        score = 0; is_long = direction == "long"
        try:
            raw = self.cg.get_full_analysis(base)
            cg = _cg_extract(raw)

            fr = cg['funding_rate']
            ls_l = cg['ls_long_pct']; ls_s = cg['ls_short_pct']
            taker = cg['taker_ratio']; sc = cg['short_conviction']
            liq = cg['liq_dominant']

            # 펀딩비
            if is_long and fr<-0.0005:
                score+=8; ev.reasons.append(f"펀딩 극음수 {fr*100:.4f}% (숏스퀴즈 기대) +8")
            elif is_long and fr<-0.0003:
                score+=5; ev.reasons.append(f"펀딩 음수 {fr*100:.4f}% +5")
            elif not is_long and fr>0.0005:
                score+=8; ev.reasons.append(f"펀딩 과열 {fr*100:.4f}% (롱과열→숏유리) +8")
            elif not is_long and fr>0.0003:
                score+=5; ev.reasons.append(f"펀딩 양수 {fr*100:.4f}% +5")
            # 경고
            if is_long and fr>0.0005:
                ev.warnings.append(f"⚠️ 펀딩 과열 {fr*100:.4f}% (롱 위험)")
            elif not is_long and fr<-0.0003:
                ev.warnings.append(f"⚠️ 펀딩 음수 {fr*100:.4f}% (숏 스퀴즈 주의)")

            # LS 비율 (역지표)
            if is_long and ls_s>65:   score+=7; ev.reasons.append(f"개미 숏쏠림 {ls_s:.0f}% (역지표→롱유리) +7")
            elif is_long and ls_s>58: score+=3; ev.reasons.append(f"숏 비율 {ls_s:.0f}% +3")
            elif not is_long and ls_l>65: score+=7; ev.reasons.append(f"개미 롱쏠림 {ls_l:.0f}% (역지표→숏유리) +7")
            elif not is_long and ls_l>58: score+=3; ev.reasons.append(f"롱 비율 {ls_l:.0f}% +3")

            # Taker (★ sell_buy_ratio: >1이면 매도 우세, <1이면 매수 우세)
            if is_long and taker<0.85:     score+=5; ev.reasons.append(f"Taker 매수 ({taker:.2f}) +5")
            elif is_long and taker<0.95:   score+=3; ev.reasons.append(f"Taker 매수 ({taker:.2f}) +3")
            elif not is_long and taker>1.15: score+=5; ev.reasons.append(f"Taker 매도 ({taker:.2f}) +5")
            elif not is_long and taker>1.05: score+=3; ev.reasons.append(f"Taker 매도 ({taker:.2f}) +3")

            # 청산
            if is_long and liq=='SHORT_LIQUIDATION': score+=5; ev.reasons.append("숏 청산 진행 (상승압력) +5")
            elif not is_long and liq=='LONG_LIQUIDATION': score+=5; ev.reasons.append("롱 청산 진행 (하락압력) +5")

            # 확신도
            if is_long and sc>=3: score+=5; ev.reasons.append(f"온체인 롱성향 {sc}/8 +5")
            elif not is_long and sc<=-4: score+=5; ev.reasons.append(f"온체인 숏확신 {sc}/8 +5")
            elif not is_long and sc<=-2: score+=3; ev.reasons.append(f"온체인 숏성향 {sc}/8 +3")
        except Exception as e:
            ev.warnings.append(f"⚠️ 온체인 에러: {e}")
        ev.onchain_score = min(35, score)

    def _macro(self, ev, direction):
        if not self.btc:
            ev.warnings.append("⚠️ BTCFilter 미연결 (.env에 COINGLASS_API_KEY 추가)")
            return
        score = 0; is_long = direction == "long"
        try:
            ctx = self.btc.analyze(fast=True)
            bull=["STRONG_BULL","BULL"]; bear=["CRASH","BEAR"]
            if is_long:
                if ctx.market_state in bull: score+=10; ev.reasons.append(f"BTC 상승장 ({ctx.market_state}) +10")
                elif ctx.market_state=="MILD_BULL": score+=5; ev.reasons.append(f"BTC 약상승 +5")
                elif ctx.market_state in bear: ev.warnings.append(f"⚠️ BTC 하락장 → 롱 역풍"); score-=5
            else:
                if ctx.market_state in bear: score+=10; ev.reasons.append(f"BTC 하락장 ({ctx.market_state}) +10")
                elif ctx.market_state=="MILD_BEAR": score+=5; ev.reasons.append(f"BTC 약하락 +5")
                elif ctx.market_state in bull: ev.warnings.append(f"⚠️ BTC 강세 → 숏 역풍"); score-=5

            if is_long and ctx.btc_trend=="BULL": score+=5; ev.reasons.append("BTC 기술적 상승 +5")
            elif not is_long and ctx.btc_trend=="BEAR": score+=5; ev.reasons.append("BTC 기술적 하락 +5")

            if is_long and ctx.mood_shift=="IMPROVING": score+=3; ev.reasons.append("BTC 분위기 개선 +3")
            elif not is_long and ctx.mood_shift in ("CRASHING","WEAKENING"):
                p = 5 if ctx.mood_shift=="CRASHING" else 3
                score+=p; ev.reasons.append(f"BTC 분위기 {'급락' if p==5 else '약화'} +{p}")

            if is_long and ctx.btc_rsi<30: score+=5; ev.reasons.append(f"BTC RSI {ctx.btc_rsi:.0f} 과매도 +5")
            elif not is_long and ctx.btc_rsi>75: score+=5; ev.reasons.append(f"BTC RSI {ctx.btc_rsi:.0f} 과매수 +5")
        except: pass
        ev.macro_score = max(0, min(25, score))

    @staticmethod
    def format_report(ev) -> str:
        dl = "롱" if ev.direction=="long" else "숏"
        di = "🟢" if ev.direction=="long" else "🔴"
        D = ev.direction.upper()
        vm = {f"STRONG_{D}":f"{di}{di} {dl} 강력 추천", D:f"{di} {dl} 가능",
              f"WEAK_{D}":f"🟡 {dl} 근거 약함", f"NO_{D}":f"⚪ {dl} 비추"}
        b = ev.total_score//5; bar = "▓"*b + "░"*(20-b)
        lines = [
            f"{'='*55}", f"  {di} {dl.upper()} 근거 리포트: {ev.symbol}", f"{'='*55}",
            f"  {vm.get(ev.verdict,'?')}", f"  [{bar}] {ev.total_score}/100",
            f"  📊기술:{ev.technical_score}/40 🔗온체인:{ev.onchain_score}/35 ₿매크로:{ev.macro_score}/25",
        ]
        if ev.reasons:
            lines.append(f"\n  ─── {dl} 근거 ───")
            for r in ev.reasons: lines.append(f"  👍 {r}")
        if ev.warnings:
            lines.append(f"\n  ─── 주의 ───")
            for w in ev.warnings: lines.append(f"  {w}")
        lines.append(f"{'='*55}")
        return "\n".join(lines)

    @staticmethod
    def format_both(le, se) -> str:
        def bar(s): b=s//5; return "▓"*b+"░"*(20-b)
        lines = [
            f"{'='*55}", f"  ⚔️ 양방향 비교: {le.symbol}", f"{'='*55}",
            f"  🟢롱 [{bar(le.total_score)}] {le.total_score}점  (기술:{le.technical_score} 온체인:{le.onchain_score} 매크로:{le.macro_score})",
            f"  🔴숏 [{bar(se.total_score)}] {se.total_score}점  (기술:{se.technical_score} 온체인:{se.onchain_score} 매크로:{se.macro_score})", "",
        ]
        d = le.total_score - se.total_score
        if d>20:    lines.append(f"  🟢🟢 결론: 롱 우세 (+{d}점)")
        elif d>5:   lines.append(f"  🟢 결론: 롱 약간 유리 (+{d}점)")
        elif d<-20: lines.append(f"  🔴🔴 결론: 숏 우세 ({d}점)")
        elif d<-5:  lines.append(f"  🔴 결론: 숏 약간 유리 ({d}점)")
        else:       lines.append(f"  ⚪ 결론: 중립 (차이 {d}점)")

        lines.append(f"\n  ─── 🟢 롱 핵심 ───")
        for r in le.reasons[:4]: lines.append(f"  {r}")
        lines.append(f"\n  ─── 🔴 숏 핵심 ───")
        for r in se.reasons[:4]: lines.append(f"  {r}")
        if le.warnings or se.warnings:
            lines.append(f"\n  ─── ⚠️ 주의 ───")
            seen = set()
            for w in le.warnings + se.warnings:
                if w not in seen: lines.append(f"  {w}"); seen.add(w)
        lines.append(f"{'='*55}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  5. 모니터봇 연동 플러그인 (CrashMonitorPlugin)
# ═══════════════════════════════════════════════════════════════

class CrashMonitorPlugin:
    """
    smart_monitor_cloud.py에 연동하는 플러그인

    ★ 텔레그램 분리:
      - 모니터봇: TELEGRAM_TOKEN_MONITOR → 모니터 채널로 전송
      - CRASH 플러그인: TELEGRAM_TOKEN_CRASH → CRASH 전용 채널로 전송
      - tg_send 미지정 시 자체 TelegramCrash 인스턴스 사용

    ── smart_monitor의 __init__에 추가 ──
      from crash_strategy import CrashMonitorPlugin
      self.crash_plugin = CrashMonitorPlugin(
          exchange=self.chart.exchange,
          cg_client=self.cg,
          btc_filter=self.btc_filter,
          # tg_send 생략 → CRASH 전용 토큰 자동 사용
      )

    ── 메인 루프(run)에 추가 ──
      self.crash_plugin.run_cycle(self.cfg['SIGNAL_WATCHLIST'])
    """

    def __init__(self, exchange, cg_client=None, btc_filter=None,
                 tg_send: Callable = None, cfg: dict = None):
        self.cfg = cfg or CONFIG
        self.detector = CrashDetector(exchange, cg_client, self.cfg)
        self.counter = CounterTradeEngine(exchange, cg_client, self.cfg)
        self.evidence = EvidenceAnalyzer(exchange, cg_client, btc_filter, self.cfg)

        # ★ 텔레그램 분리: tg_send 안 주면 CRASH 전용 채널 자동 사용
        if tg_send:
            self.tg_send = tg_send
        else:
            self._tg = TelegramCrash()
            self.tg_send = self._tg.send

        self._last_crash = 0; self._last_ev = 0; self._cooldown = {}

    def run_cycle(self, symbols: List[str] = None):
        now = time.time(); syms = symbols or self.cfg["WATCH_SYMBOLS"]

        if self.cfg["PLUGIN_CRASH_ENABLED"] and now - self._last_crash >= self.cfg["CRASH_CHECK_INTERVAL"]:
            self._check_crashes(syms); self._last_crash = now

        if self.cfg["PLUGIN_EVIDENCE_ENABLED"] and now - self._last_ev >= self.cfg["EVIDENCE_INTERVAL"]:
            ev_syms = self.cfg["PLUGIN_EVIDENCE_SYMBOLS"] or syms[:5]
            self._run_evidence(ev_syms); self._last_ev = now

    def _check_crashes(self, symbols):
        now = time.time()
        for sym in symbols:
            if now - self._cooldown.get(sym, 0) < 600: continue
            try:
                ev = self.detector.detect(sym)
                if ev:
                    msg = f"🚨 급락!\n\n{CrashDetector.format_event(ev)}"
                    if ev.counter_signal in ("SCALP_LONG","SWING_LONG"):
                        for t in self.counter.analyze(sym, ev)[:2]:
                            msg += f"\n\n{CounterTradeEngine.format_trade(t)}"
                    self.tg_send(msg); self._cooldown[sym] = now
                time.sleep(0.3)
            except: pass

    def _run_evidence(self, symbols):
        reports = []
        for sym in symbols:
            try:
                both = self.evidence.analyze_both(sym)
                reports.append(EvidenceAnalyzer.format_both(both['long'], both['short']))
                time.sleep(1)
            except: pass
        if reports:
            hdr = f"📊 양방향 근거 ({datetime.now(KST).strftime('%H:%M')})\n"
            full = hdr
            for r in reports:
                if len(full)+len(r) > 3800: self.tg_send(full); full = ""
                full += r + "\n\n"
            if full.strip(): self.tg_send(full)

    def force_evidence(self, symbol: str) -> str:
        both = self.evidence.analyze_both(symbol)
        report = EvidenceAnalyzer.format_both(both['long'], both['short'])
        self.tg_send(report); return report


# ═══════════════════════════════════════════════════════════════
#  🚀 단독 실행
# ═══════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]
    ex, cg, btc_f = init_components()
    tg = TelegramCrash()  # CRASH 전용 채널

    if '--evidence' in args:
        idx = args.index('--evidence')
        coins = args[idx+1:] if idx+1 < len(args) else ['BTC']
        an = EvidenceAnalyzer(ex, cg, btc_f)
        for c in coins:
            sym = f"{c.upper()}/USDT"; print(f"\n분석: {sym}...")
            b = an.analyze_both(sym); r = EvidenceAnalyzer.format_both(b['long'], b['short'])
            print(r); tg.send(r); time.sleep(1)
        return

    if '--short-report' in args:
        idx = args.index('--short-report')
        coins = args[idx+1:] if idx+1 < len(args) else ['BTC']
        an = EvidenceAnalyzer(ex, cg, btc_f)
        for c in coins:
            sym = f"{c.upper()}/USDT"; ev = an.analyze(sym, "short")
            r = EvidenceAnalyzer.format_report(ev); print(r); tg.send(r); time.sleep(1)
        return

    if '--long-report' in args:
        idx = args.index('--long-report')
        coins = args[idx+1:] if idx+1 < len(args) else ['BTC']
        an = EvidenceAnalyzer(ex, cg, btc_f)
        for c in coins:
            sym = f"{c.upper()}/USDT"; ev = an.analyze(sym, "long")
            r = EvidenceAnalyzer.format_report(ev); print(r); tg.send(r); time.sleep(1)
        return

    if '--watch' in args:
        idx = args.index('--watch')
        coins = args[idx+1:] if idx+1 < len(args) else ['BTC','ETH','SOL']
        det = CrashDetector(ex, cg); ctr = CounterTradeEngine(ex, cg)
        syms = [f"{c.upper()}/USDT" for c in coins]
        tg.send(f"👁️ 급락 감시: {', '.join(coins)}"); print(f"👁️ 급락 감시: {', '.join(coins)}")
        while True:
            for sym in syms:
                ev = det.detect(sym)
                if ev:
                    msg = f"🚨 급락!\n\n{CrashDetector.format_event(ev)}"
                    if ev.counter_signal in ("SCALP_LONG","SWING_LONG"):
                        for t in ctr.analyze(sym, ev)[:2]: msg += f"\n\n{CounterTradeEngine.format_trade(t)}"
                    print(msg); tg.send(msg)
                time.sleep(0.5)
            print(f"  ⏳ {datetime.now(KST).strftime('%H:%M:%S')}"); time.sleep(CONFIG["CRASH_CHECK_INTERVAL"])
        return

    # 기본: 전체 분석
    syms = CONFIG["WATCH_SYMBOLS"]
    print("="*55); print("  🔬 급락 + 반대매매 + 양방향 근거 종합분석"); print("="*55)
    det = CrashDetector(ex, cg); ctr = CounterTradeEngine(ex, cg); an = EvidenceAnalyzer(ex, cg, btc_f)
    reports = []
    for sym in syms:
        print(f"\n{'─'*40}\n  📊 {sym}\n{'─'*40}")
        ev = det.detect(sym)
        if ev:
            print(CrashDetector.format_event(ev))
            for t in ctr.analyze(sym, ev)[:2]: print(f"\n{CounterTradeEngine.format_trade(t)}")
        else: print("  ✅ 급락 없음")
        b = an.analyze_both(sym); r = EvidenceAnalyzer.format_both(b['long'], b['short'])
        print(r); reports.append(r); time.sleep(1)

    hdr = f"🔬 전체분석 ({datetime.now(KST).strftime('%H:%M')})\n\n"
    full = hdr
    for r in reports:
        if len(full)+len(r) > 3800: tg.send(full); full = ""
        full += r + "\n\n"
    if full.strip(): tg.send(full)

if __name__ == "__main__":
    main()
