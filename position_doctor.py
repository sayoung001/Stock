"""
position_doctor.py — 물린 포지션 홀딩/손절 진단기 (20배 레버리지 특화)
"""
import ccxt, pandas as pd, time, os
from datetime import datetime, timedelta, timezone

from indicators_v2 import calculate_indicators, calculate_mtf_confluence
from coinglass_client import CoinGlassClient
from btc_filter import BTCFilter
from dotenv import load_dotenv
load_dotenv()
KST = timezone(timedelta(hours=9))

CONFIG = {
    "COINGLASS_API_KEY": os.environ.get("COINGLASS_API_KEY", ""),
    "LEVERAGE": 20,  # 🔥 본인의 레버리지 설정 (20배)
    
    "MY_POSITIONS": [
        {"symbol": "DOT/USDT", "direction": "short", "entry_price": 1.538},
        {"symbol": "1000PEPE/USDT", "direction": "short", "entry_price": 0.0034068},
        {"symbol": "DOGE/USDT", "direction": "short", "entry_price": 0.09165},
        #         {'sym': 'DOT/USDT', 'dir': 'short', 'entry_price': 1.538, 'entry_btc_score': -3, 'entry_btc_state': 'MILD_BEAR'},
    ]
}

class PositionDoctor:
    def __init__(self, coinglass_api_key):
        print("🩺 닥터 초기화 중 (데이터 연결)...")
        self.ex = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        self.cg = CoinGlassClient(coinglass_api_key)
        self.btc = BTCFilter(self.cg, self.ex, cache_seconds=60)
        self.lev = CONFIG["LEVERAGE"]
        print("✅ 초기화 완료!\n")

    def fetch_data(self, sym, tf='15m'):
        try:
            ohlcv = self.ex.fetch_ohlcv(sym, timeframe=tf, limit=100)
            df = pd.DataFrame(ohlcv, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['Date'] = pd.to_datetime(df['Date'], unit='ms')
            df.set_index('Date', inplace=True)
            df = calculate_indicators(df)
            return df
        except Exception as e:
            return None

    def analyze_mtf(self, sym):
        tfs = ['4h', '1h', '15m']
        rs = {}
        for tf in tfs:
            df = self.fetch_data(sym, tf)
            if df is not None and len(df) > 5:
                l = df.iloc[-1]
                trend = "BULL" if l["close_20_ema"] > l["close_60_ema"] and l.get("adx", 0) > 20 else \
                        ("BEAR" if l["close_20_ema"] < l["close_60_ema"] and l.get("adx", 0) > 20 else "NEUTRAL")
                rs[tf] = {"trend": trend, "close": l["Close"], "rsi": l["rsi_14"], 
                          "adx": l.get("adx", 0), "atr_pct": l.get("atr_pct", 0)}
            time.sleep(0.2)
        return rs, calculate_mtf_confluence(rs)

    def diagnose(self, symbol, direction, entry_price):
        base = symbol.split('/')[0]
        
        print(f"🔍 [{symbol}] {direction.upper()} (진입가: {entry_price}) 진단 중...")
        btc_ctx = self.btc.analyze(fast=True)
        mtf_data, mtf_conf = self.analyze_mtf(symbol)
        cg_data = self.cg.get_full_analysis(base)
        
        if not mtf_data or '15m' not in mtf_data:
            print(f"❌ {symbol} 차트 데이터를 불러올 수 없습니다.\n")
            return

        curr_price = mtf_data['15m']['close']
        atr = mtf_data['15m']['atr_pct'] / 100 * curr_price
        atr_pct = mtf_data['15m']['atr_pct']
        adx = mtf_data['15m']['adx']
        
        # ── 🔥 레버리지 기반 수익률 및 청산가 계산 ──
        spot_pnl_pct = (curr_price - entry_price) / entry_price * 100 if direction == 'long' else (entry_price - curr_price) / entry_price * 100
        roe_pct = spot_pnl_pct * self.lev  # 실제 체감 ROE
        
        # 예상 청산가 (유지증거금 등 고려하여 대략 95% 손실 지점)
        liq_margin = 0.95 / self.lev
        liq_price = entry_price * (1 - liq_margin) if direction == 'long' else entry_price * (1 + liq_margin)
        
        # ── 동적 손절가 계산 ──
        base_mult = 2.0 if adx > 25 else (1.5 if adx > 18 else 1.2)
        cs = cg_data.get('short_conviction', 0) if cg_data else 0
        if (direction == "long" and cs <= -4) or (direction == "short" and cs >= 4):
            base_mult *= 0.5
            
        hard_sl = entry_price - (atr * base_mult) if direction == 'long' else entry_price + (atr * base_mult)

        score = 80
        reasons = []
        warnings = []

        # A. 레버리지/청산 위험도
        if roe_pct < -50:
            score -= 30; warnings.append(f"🚨 반토막(-50% ROE) 이상 손실권! 강제청산 주의")
        elif roe_pct < -20:
            score -= 10; warnings.append(f"⚠️ ROE -20% 이상 손실 진행 중")
            
        if atr_pct * self.lev > 40:
            score -= 20; warnings.append(f"🚨 변동성 극대화: 캔들 1~2개로 청산당할 수 있는 위험한 종목입니다 (레버리지 축소 요망)")

        # B. 가격 위치 (마지노선)
        if direction == 'long':
            if curr_price < hard_sl:
                score -= 30; warnings.append("🚨 차트상 구조적 마지노선(지저선) 붕괴")
        else:
            if curr_price > hard_sl:
                score -= 30; warnings.append("🚨 차트상 구조적 마지노선(저항선) 돌파당함")

        # C. BTC 시장 분위기
        if direction == 'long':
            if btc_ctx.market_state in ["CRASH", "BEAR"]:
                score -= 20; warnings.append(f"🔴 BTC 하락장 ({btc_ctx.market_state})")
            elif btc_ctx.market_state in ["STRONG_BULL", "BULL"]:
                score += 10; reasons.append(f"🟢 BTC 상승장 ({btc_ctx.market_state})")
        else:
            if btc_ctx.market_state in ["STRONG_BULL", "BULL"]:
                score -= 20; warnings.append(f"🔴 BTC 강세장 ({btc_ctx.market_state})")
            elif btc_ctx.market_state in ["CRASH", "BEAR"]:
                score += 10; reasons.append(f"🟢 BTC 하락장 ({btc_ctx.market_state})")

        # D. 다중 시간대(MTF)
        c_status = mtf_conf['confluence']
        if direction == 'long':
            if c_status in ['FULL_BEAR', 'PARTIAL_BEAR']:
                score -= 20; warnings.append(f"📉 차트 하락 추세 ({c_status})")
            elif c_status in ['FULL_BULL', 'PARTIAL_BULL']:
                score += 10; reasons.append(f"📈 차트 상승 추세 ({c_status})")
        else:
            if c_status in ['FULL_BULL', 'PARTIAL_BULL']:
                score -= 20; warnings.append(f"📈 차트 상승 추세 ({c_status})")
            elif c_status in ['FULL_BEAR', 'PARTIAL_BEAR']:
                score += 10; reasons.append(f"📉 차트 하락 추세 ({c_status})")

        score = max(0, min(100, score))
        
        print(f"\n{'='*60}")
        print(f" 🩺 [진단 결과] {symbol} {direction.upper()} ({self.lev}x 레버리지)")
        print(f"{'='*60}")
        print(f" 💰 진입가: {entry_price:.5f} | 현재가: {curr_price:.5f}")
        print(f" 📊 현재 수익률: ROE {roe_pct:+.2f}% (현물 {spot_pnl_pct:+.2f}%)")
        print(f" 💀 예상 청산가: {liq_price:.5f} (도달 시 원금 100% 손실)")
        print(f" 🛡️ 논리적 손절가: {hard_sl:.5f} (차트 이탈 지점)")
        print(f" 🌡️ 생존 점수: {score}/100")
        print("-" * 60)
        
        if score >= 70:
            print(" ✅ [HOLD] 근거가 튼튼합니다. 흔들리지 말고 홀딩하세요.")
        elif score >= 40:
            print(" ⚠️ [REDUCE] 애매합니다. 반등/반락 시 물량을 절반으로 줄여 리스크를 낮추세요.")
        else:
            print(" 🔴 [CUT LOSS] 근거가 박살났습니다. 청산당하기 전에 시장가 손절을 강력 권장합니다.")
        
        print("\n [요약]")
        all_msgs = [f"👍 {r}" for r in reasons] + [f"🚨 {w}" for w in warnings]
        for m in all_msgs: print(f"  {m}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    doctor = PositionDoctor(CONFIG["COINGLASS_API_KEY"])
    
    positions = CONFIG.get("MY_POSITIONS", [])
    if not positions:
        print("⚠️ CONFIG에 입력된 포지션이 없습니다.")
    else:
        for pos in positions:
            doctor.diagnose(pos["symbol"], pos["direction"], pos["entry_price"])
            time.sleep(1) # 여러 개 진단 시 API 보호를 위한 1초 대기