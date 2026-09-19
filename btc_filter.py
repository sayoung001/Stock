"""
btc_filter.py v3 — 양방향 BTC 분위기 필터

  BTC 상태       롱             숏
  STRONG_BULL   ✅+15          ❌차단
  BULL          ✅+10          ⚠️-10
  MILD_BULL     ✅+5           ✅-5
  NEUTRAL       ✅ 0           ✅ 0
  MILD_BEAR     ⚠️-10          ✅+5
  BEAR          ❌차단         ✅+10
  CRASH         ❌차단         ✅+15(모멘텀만)
"""
import time, ccxt
import pandas as pd, numpy as np
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, List

KST = timezone(timedelta(hours=9))
MOOD_EMOJI = {"STRONG_BULL":"🟢🟢","BULL":"🟢","MILD_BULL":"🟢·","NEUTRAL":"⚪","MILD_BEAR":"🟠","BEAR":"🔴","CRASH":"🔴🔴"}

@dataclass
class BTCContext:
    oi_change_pct:float=0.0; oi_trend:str="UNKNOWN"; oi_change_4h_pct:float=0.0
    funding_rate:float=0.0; funding_signal:str="NEUTRAL"; funding_rates_str:str=""
    taker_ratio:float=1.0; taker_signal:str="NEUTRAL"
    taker_consecutive_sell:bool=False; taker_consecutive_buy:bool=False
    ls_long_pct:float=50.0; ls_short_pct:float=50.0
    liq_dominant:str="NONE"; liq_long_usd:float=0.0; liq_short_usd:float=0.0
    btc_price:float=0.0; btc_rsi:float=50.0; btc_adx:float=0.0
    btc_trend:str="NEUTRAL"; btc_macdh_rising:bool=False
    btc_ema_cross:str="NEUTRAL"; btc_atr_pct:float=0.0; btc_1h_change_pct:float=0.0
    market_score:int=0; market_state:str="NEUTRAL"
    reasons:List[str]=field(default_factory=list); timestamp:str=""
    # 양방향 필터
    long_safe:bool=True; long_boost:int=0
    short_safe:bool=True; short_boost:int=0
    danger_level:int=0
    # 하위 호환
    @property
    def alt_long_safe(self): return self.long_safe
    @property
    def alt_short_safe(self): return self.short_safe
    @property
    def alt_long_boost(self): return self.long_boost
    @property
    def alt_short_boost(self): return self.short_boost
    # 분위기 변화
    prev_score:int=0; prev_state:str="NEUTRAL"; score_delta:int=0; mood_shift:str="STABLE"

@dataclass
class SentimentShift:
    entry_score:int; entry_state:str; current_score:int; current_state:str
    delta:int; severity:str; should_exit:bool; message:str; direction:str="long"
    @staticmethod
    def evaluate(es, est, cs, cst, direction="long"):
        raw = cs - es
        d = raw if direction=="long" else -raw
        if d >= 0: sev,ex = "SAFE",False; msg=f"분위기 유지 ({raw:+d})"
        elif d >= -2: sev,ex = "CAUTION",False; msg=f"분위기 변화 ({raw:+d})"
        elif d >= -4: sev,ex = "DANGER",False; msg=f"⚠️ 분위기 악화 ({raw:+d})"
        else: sev,ex = "EMERGENCY",True; msg=f"🔴 급변! ({raw:+d}) 탈출!"
        levels=["CRASH","BEAR","MILD_BEAR","NEUTRAL","MILD_BULL","BULL","STRONG_BULL"]
        try:
            ei=levels.index(est); ci=levels.index(cst)
            drop=ei-ci if direction=="long" else ci-ei
            if drop>=3 and sev!="EMERGENCY": sev="DANGER"; msg=f"⚠️ {est}→{cst} 3단계! ({raw:+d})"
            if drop>=4: sev,ex="EMERGENCY",True; msg=f"🔴 {est}→{cst} 급변! ({raw:+d})"
        except: pass
        return SentimentShift(es,est,cs,cst,raw,sev,ex,msg,direction)

class BTCFilter:
    def __init__(self, cg_client, exchange=None, cache_seconds=120, fast_cache_seconds=30):
        self.cg=cg_client; self.exchange=exchange or ccxt.binance({"enableRateLimit":True})
        self.cache_seconds=cache_seconds; self.fast_cache_seconds=fast_cache_seconds
        self._cached=None; self._cache_time=0; self._history=[]

    def analyze(self, fast=False, force=False):
        now=time.time(); ttl=self.fast_cache_seconds if fast else self.cache_seconds
        if not force and self._cached and (now-self._cache_time<ttl): return self._cached
        ctx=BTCContext(timestamp=datetime.now(KST).isoformat())
        if self._cached: ctx.prev_score=self._cached.market_score; ctx.prev_state=self._cached.market_state
        self._fetch_onchain(ctx); self._fetch_technical(ctx)
        self._calc_score(ctx); self._apply_dual_filter(ctx); self._detect_shift(ctx)
        self._cached=ctx; self._cache_time=now
        self._history.append((now,ctx.market_score,ctx.market_state))
        if len(self._history)>100: self._history=self._history[-50:]
        return ctx

    def check_shift(self, entry_score, entry_state, direction="long"):
        ctx=self.analyze(fast=True)
        return SentimentShift.evaluate(entry_score,entry_state,ctx.market_score,ctx.market_state,direction)

    def _fetch_onchain(self, ctx):
        try:
            oi=self.cg.get_oi_change("BTC","1h")
            if oi: ctx.oi_change_pct=oi["change_pct"]; ctx.oi_trend=oi["trend"]
        except: pass
        try:
            h=self.cg.get_oi_history("BTC","1h",limit=5)
            if h and len(h)>=5:
                o0=float(h[0].get("close",h[0].get("c",0))); o1=float(h[-1].get("close",h[-1].get("c",0)))
                if o0>0: ctx.oi_change_4h_pct=round((o1-o0)/o0*100,2)
        except: pass
        try:
            f=self.cg.get_funding_rate_trend("BTC")
            if f: ctx.funding_rate=f["current"]; ctx.funding_signal=f["signal"]; ctx.funding_rates_str=f["rates_str"]
        except: pass
        try:
            t=self.cg.get_taker_pressure("BTC")
            if t: ctx.taker_ratio=t["sell_buy_ratio"]; ctx.taker_signal=t["signal"]; ctx.taker_consecutive_sell=t.get("consecutive_sell",False); ctx.taker_consecutive_buy=t.get("consecutive_buy",False)
        except: pass
        try:
            ls=self.cg.get_global_ls_ratio("BTC")
            if ls: ctx.ls_long_pct=ls["long_pct"]; ctx.ls_short_pct=ls["short_pct"]
        except: pass
        try:
            liq=self.cg.get_recent_liquidation_pressure("BTC")
            if liq: ctx.liq_dominant=liq["dominant"]; ctx.liq_long_usd=liq["long_liquidation_usd"]; ctx.liq_short_usd=liq["short_liquidation_usd"]
        except: pass

    def _fetch_technical(self, ctx):
        try:
            ohlcv=self.exchange.fetch_ohlcv("BTC/USDT","15m",limit=80)
            df=pd.DataFrame(ohlcv,columns=["Date","Open","High","Low","Close","Volume"])
            df["Date"]=pd.to_datetime(df["Date"],unit="ms"); df.set_index("Date",inplace=True)
            if len(df)<30: return
            c=df["Close"]; h=df["High"]; lo=df["Low"]
            ema20=c.ewm(span=20).mean(); ema60=c.ewm(span=60).mean()
            delta=c.diff(); gain=delta.where(delta>0,0).rolling(14).mean()
            loss=(-delta.where(delta<0,0)).rolling(14).mean()
            rsi=100-(100/(1+gain/(loss+1e-9)))
            tr=pd.concat([h-lo,(h-c.shift(1)).abs(),(lo-c.shift(1)).abs()],axis=1).max(axis=1)
            atr=tr.rolling(14).mean()
            plus_dm=h.diff().where((h.diff()>-lo.diff())&(h.diff()>0),0)
            minus_dm=(-lo.diff()).where((-lo.diff()>h.diff())&(-lo.diff()>0),0)
            sa=atr.rolling(14).mean()
            pdi=100*(plus_dm.rolling(14).mean()/(sa+1e-9)); mdi=100*(minus_dm.rolling(14).mean()/(sa+1e-9))
            dx=100*(pdi-mdi).abs()/(pdi+mdi+1e-9); adx=dx.rolling(14).mean()
            e12=c.ewm(span=12).mean(); e26=c.ewm(span=26).mean()
            macdh=(e12-e26)-(e12-e26).ewm(span=9).mean()
            last=df.iloc[-1]
            ctx.btc_price=last["Close"]; ctx.btc_rsi=round(float(rsi.iloc[-1]),1)
            ctx.btc_adx=round(float(adx.iloc[-1]),1); ctx.btc_atr_pct=round(float(atr.iloc[-1])/last["Close"]*100,2)
            ctx.btc_macdh_rising=float(macdh.iloc[-1])>float(macdh.iloc[-2])
            e20v=float(ema20.iloc[-1]); e60v=float(ema60.iloc[-1]); av=float(adx.iloc[-1])
            ctx.btc_trend="BULL" if e20v>e60v and av>20 else ("BEAR" if e20v<e60v and av>20 else "NEUTRAL")
            for i in range(-3,0):
                pe20=float(ema20.iloc[i-1]); pe60=float(ema60.iloc[i-1])
                ce20=float(ema20.iloc[i]); ce60=float(ema60.iloc[i])
                if pe20<=pe60 and ce20>ce60: ctx.btc_ema_cross="GOLDEN"
                elif pe20>=pe60 and ce20<ce60: ctx.btc_ema_cross="DEATH"
            if len(df)>=5:
                p1h=float(df.iloc[-5]["Close"])
                ctx.btc_1h_change_pct=round((last["Close"]-p1h)/p1h*100,2)
        except Exception as e: print(f"  ⚠️ BTC 기술분석: {e}")

    def _calc_score(self, ctx):
        s=0; r=[]
        if ctx.oi_change_pct<-5: s-=3; r.append(f"⚠️ OI급감 {ctx.oi_change_pct:+.1f}%")
        elif ctx.oi_change_pct<-2: s-=1; r.append(f"OI↓ {ctx.oi_change_pct:+.1f}%")
        elif ctx.oi_change_pct>3: s+=1; r.append(f"OI↑ {ctx.oi_change_pct:+.1f}%")
        if ctx.oi_change_4h_pct<-8: s-=2; r.append(f"⚠️ OI 4h↓ {ctx.oi_change_4h_pct:+.1f}%")
        elif ctx.oi_change_4h_pct>5: s+=1
        if ctx.funding_rate>0.0005: s-=2; r.append(f"펀딩과열 {ctx.funding_rate*100:.4f}%")
        elif ctx.funding_rate>0.0003: s-=1; r.append(f"펀딩↑ {ctx.funding_rate*100:.4f}%")
        elif ctx.funding_rate<-0.0005: s+=2; r.append("펀딩극음수→숏스퀴즈")
        elif ctx.funding_rate<-0.0003: s+=1; r.append(f"펀딩↓ {ctx.funding_rate*100:.4f}%")
        if ctx.taker_consecutive_sell: s-=2; r.append(f"Taker연속매도({ctx.taker_ratio:.2f})")
        elif ctx.taker_signal in ("SELL_PRESSURE","STRONG_SELL_PRESSURE"): s-=1; r.append(f"Taker매도({ctx.taker_ratio:.2f})")
        elif ctx.taker_consecutive_buy: s+=2; r.append(f"Taker연속매수({ctx.taker_ratio:.2f})")
        elif ctx.taker_signal in ("BUY_PRESSURE","STRONG_BUY_PRESSURE"): s+=1; r.append(f"Taker매수({ctx.taker_ratio:.2f})")
        if ctx.ls_long_pct>65: s-=1; r.append(f"개미롱쏠림{ctx.ls_long_pct:.0f}%")
        elif ctx.ls_short_pct>60: s+=1; r.append(f"개미숏쏠림{ctx.ls_short_pct:.0f}%")
        if ctx.liq_dominant=="LONG_LIQUIDATION" and ctx.liq_long_usd>5e6: s-=2; r.append(f"롱대량청산${ctx.liq_long_usd/1e6:.1f}M")
        elif ctx.liq_dominant=="LONG_LIQUIDATION": s-=1; r.append("롱청산우위")
        elif ctx.liq_dominant=="SHORT_LIQUIDATION" and ctx.liq_short_usd>5e6: s+=1; r.append("숏청산(상승)")
        if ctx.btc_trend=="BULL": s+=1; r.append("기술상승")
        elif ctx.btc_trend=="BEAR": s-=1; r.append("기술하락")
        if ctx.btc_1h_change_pct<-2: s-=2; r.append(f"⚠️1h{ctx.btc_1h_change_pct:+.1f}%급락")
        elif ctx.btc_1h_change_pct<-1: s-=1; r.append(f"1h{ctx.btc_1h_change_pct:+.1f}%")
        elif ctx.btc_1h_change_pct>2: s+=1; r.append(f"1h{ctx.btc_1h_change_pct:+.1f}%")
        if ctx.btc_ema_cross=="DEATH": s-=1; r.append("데드크로스")
        elif ctx.btc_ema_cross=="GOLDEN": s+=1; r.append("골든크로스")
        if ctx.btc_rsi>80: s-=1; r.append(f"RSI과매수{ctx.btc_rsi:.0f}")
        elif ctx.btc_rsi<25: s+=1; r.append(f"RSI과매도{ctx.btc_rsi:.0f}")
        ctx.market_score=max(-10,min(10,s)); ctx.reasons=r
        if s>=5: ctx.market_state="STRONG_BULL"
        elif s>=3: ctx.market_state="BULL"
        elif s>=1: ctx.market_state="MILD_BULL"
        elif s<=-6: ctx.market_state="CRASH"
        elif s<=-4: ctx.market_state="BEAR"
        elif s<=-2: ctx.market_state="MILD_BEAR"
        else: ctx.market_state="NEUTRAL"

    def _apply_dual_filter(self, ctx):
        table={"STRONG_BULL":(True,+15,False,-20,0),"BULL":(True,+10,True,-10,0),
            "MILD_BULL":(True,+5,True,-5,0),"NEUTRAL":(True,0,True,0,0),
            "MILD_BEAR":(True,-10,True,+5,1),"BEAR":(False,-15,True,+10,2),
            "CRASH":(False,-20,True,+15,3)}
        ls,lb,ss,sb,d=table.get(ctx.market_state,(True,0,True,0,0))
        ctx.long_safe=ls; ctx.long_boost=lb; ctx.short_safe=ss; ctx.short_boost=sb; ctx.danger_level=d

    def _detect_shift(self, ctx):
        d=ctx.market_score-ctx.prev_score; ctx.score_delta=d
        if d>=2: ctx.mood_shift="IMPROVING"
        elif d<=-3: ctx.mood_shift="CRASHING"
        elif d<=-1: ctx.mood_shift="WEAKENING"
        else: ctx.mood_shift="STABLE"

    @staticmethod
    def mood_bar(ctx):
        filled=max(0,min(10,ctx.market_score+5)); bar="▓"*filled+"░"*(10-filled)
        e=MOOD_EMOJI.get(ctx.market_state,"⚪")
        sh={"IMPROVING":" 📈","WEAKENING":" 📉","CRASHING":" ⚠️📉📉"}.get(ctx.mood_shift,"")
        return f"{e} [{bar}] {ctx.market_state} ({ctx.market_score:+d}){sh}"

    @staticmethod
    def one_liner(ctx):
        e=MOOD_EMOJI.get(ctx.market_state,"⚪")
        sh={"CRASHING":"⚠️↓↓","WEAKENING":"↓","IMPROVING":"↑"}.get(ctx.mood_shift,"")
        ls="✅" if ctx.long_safe else "❌"; ss="✅" if ctx.short_safe else "❌"
        return f"₿{e} {ctx.market_state}({ctx.market_score:+d}) ${ctx.btc_price:,.0f} {ctx.btc_1h_change_pct:+.1f}%/1h 롱{ls}({ctx.long_boost:+d}) 숏{ss}({ctx.short_boost:+d}) {sh}"

    @staticmethod
    def detail_msg(ctx):
        lines=["₿ BTC 시장 분위기",BTCFilter.mood_bar(ctx),
            f"💰 ${ctx.btc_price:,.0f} ({ctx.btc_1h_change_pct:+.1f}%/1h)",
            f"📊 추세:{ctx.btc_trend} RSI:{ctx.btc_rsi:.0f} ADX:{ctx.btc_adx:.0f}",
            f"🔗 OI:{ctx.oi_change_pct:+.1f}%/1h ({ctx.oi_change_4h_pct:+.1f}%/4h)",
            f"   펀딩:{ctx.funding_rate*100:.4f}% Taker:{ctx.taker_ratio:.2f}",
            f"   LS:롱{ctx.ls_long_pct:.0f}%/숏{ctx.ls_short_pct:.0f}%",""]
        ls="✅가능" if ctx.long_safe else "❌차단"; ss="✅가능" if ctx.short_safe else "❌차단"
        lines.append(f"🟢롱:{ls}({ctx.long_boost:+d}) | 🔴숏:{ss}({ctx.short_boost:+d})")
        if ctx.mood_shift!="STABLE": lines.append(f"📉 변화: {ctx.prev_state}→{ctx.market_state} ({ctx.score_delta:+d})")
        lines.append("")
        for r in ctx.reasons[:5]: lines.append(f"  • {r}")
        return "\n".join(lines)
