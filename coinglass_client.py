"""
coinglass_client.py
===================
CoinGlass API v4 클라이언트 (Startup 플랜 최적화)
- Rate Limit: 80회/분 자동 관리
- 캐싱으로 불필요한 API 호출 최소화
- 전략별 데이터 전처리 내장
"""

import requests
import time
import json
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import deque
from functools import lru_cache

KST = timezone(timedelta(hours=9))

class CoinGlassClient:
    BASE_URL = "https://open-api-v4.coinglass.com/api"
    
    def __init__(self, api_key: str, rate_limit: int = 30):
        """
        :param api_key: CoinGlass API 키
        :param rate_limit: 분당 최대 호출 수 (v9+semi 동시 실행 고려하여 30으로 축소)
        """
        self.api_key = api_key
        self.rate_limit = rate_limit
        self.headers = {
            "accept": "application/json",
            "CG-API-KEY": api_key
        }
        # Rate limit 관리용 (슬라이딩 윈도우)
        self._call_times = deque()
        # 캐시 (키: endpoint+params, 값: (timestamp, data))
        self._cache = {}
        self._cache_ttl = 60  # 기본 60초 캐시
    
    def _rate_limit_wait(self):
        """슬라이딩 윈도우 Rate Limit 관리"""
        now = time.time()
        # 1분 이전 기록 제거
        while self._call_times and self._call_times[0] < now - 60:
            self._call_times.popleft()
        # 한도 초과 시 대기
        if len(self._call_times) >= self.rate_limit:
            wait_time = 60 - (now - self._call_times[0]) + 0.5
            if wait_time > 0:
                print(f"⏳ Rate Limit 대기: {wait_time:.1f}초...")
                time.sleep(wait_time)
        self._call_times.append(time.time())
    
    def _get_cache_key(self, endpoint, params):
        return f"{endpoint}:{json.dumps(params, sort_keys=True)}"
    
    def _request(self, endpoint: str, params: dict = None, cache_ttl: int = None):
        """
        API 요청 (Rate Limit + 캐싱)
        :param cache_ttl: 캐시 유효시간(초). None이면 기본값 사용. 0이면 캐시 비활성화.
        """
        if params is None:
            params = {}
        
        ttl = cache_ttl if cache_ttl is not None else self._cache_ttl
        cache_key = self._get_cache_key(endpoint, params)
        
        # 캐시 확인
        if ttl > 0 and cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < ttl:
                return cached_data
        
        # Rate Limit 체크
        self._rate_limit_wait()
        
        url = f"{self.BASE_URL}{endpoint}"
        try:
            res = requests.get(url, headers=self.headers, params=params, timeout=10)

            # 429 Too Many Requests → 60초 대기 후 재시도 1회
            if res.status_code == 429:
                print(f"⏳ CoinGlass 429 → 60초 대기 후 재시도: {endpoint}")
                time.sleep(60)
                res = requests.get(url, headers=self.headers, params=params, timeout=10)

            data = res.json()

            if str(data.get('code')) != '0':
                msg = data.get('msg', 'Unknown')
                # Too Many Requests는 반복 출력 방지 (캐시에 None 저장)
                if 'Too Many' in str(msg):
                    if ttl > 0:
                        self._cache[cache_key] = (time.time(), None)
                else:
                    print(f"⚠️ CoinGlass API 오류: {endpoint} → {msg}")
                return None
            
            result = data.get('data')
            # 캐시 저장
            if ttl > 0 and result is not None:
                self._cache[cache_key] = (time.time(), result)
            return result
            
        except Exception as e:
            print(f"❌ CoinGlass 요청 실패: {endpoint} → {e}")
            return None

    # =====================================================
    # 📊 미결제약정 (Open Interest)
    # =====================================================
    def get_oi_history(self, symbol: str, interval: str = "1h", limit: int = 24):
        """코인 합산 OI OHLC 히스토리"""
        return self._request(
            "/futures/open-interest/aggregated-history",
            {"symbol": symbol, "interval": interval, "limit": limit},
            cache_ttl=120
        )
    
    def get_oi_change(self, symbol: str, interval: str = "1h"):
        """OI 변화율 계산 (최근 2개 봉 비교)"""
        data = self.get_oi_history(symbol, interval, limit=2)
        if not data or len(data) < 2:
            return None
        
        prev_oi = float(data[-2].get('close', data[-2].get('c', 0)))
        curr_oi = float(data[-1].get('close', data[-1].get('c', 0)))
        
        if prev_oi == 0:
            return None
        
        change_pct = (curr_oi - prev_oi) / prev_oi * 100
        return {
            'prev_oi': prev_oi,
            'curr_oi': curr_oi,
            'change_pct': round(change_pct, 2),
            'trend': 'UP' if change_pct > 0.5 else ('DOWN' if change_pct < -0.5 else 'FLAT')
        }

    # =====================================================
    # 💰 펀딩비 (Funding Rate)
    # =====================================================
    def get_funding_rate_history(self, symbol: str, interval: str = "8h", limit: int = 6):
        """펀딩비 OHLC 히스토리 (기본 48시간 = 6개 × 8h)"""
        return self._request(
            "/futures/fundingRate/ohlc-history",
            {"symbol": symbol, "interval": interval, "limit": limit},
            cache_ttl=300  # 5분 캐시 (펀딩비는 느리게 변함)
        )
    
    def get_funding_rate_trend(self, symbol: str):
        """
        펀딩비 과열 분석
        - 3회 연속 양수 증가 → 롱 과열
        - 3회 연속 음수 감소 → 숏 과열
        """
        data = self.get_funding_rate_history(symbol, "8h", limit=4)
        if not data or len(data) < 3:
            return None
        
        rates = []
        for d in data[-3:]:
            rate = float(d.get('close', d.get('c', 0)))
            rates.append(rate)
        
        # 트렌드 판정
        all_positive = all(r > 0 for r in rates)
        all_negative = all(r < 0 for r in rates)
        increasing = all(rates[i] > rates[i-1] for i in range(1, len(rates)))
        decreasing = all(rates[i] < rates[i-1] for i in range(1, len(rates)))
        
        signal = "NEUTRAL"
        if all_positive and increasing:
            signal = "LONG_OVERHEATED"  # 숏 기회
        elif all_negative and decreasing:
            signal = "SHORT_OVERHEATED"  # 숏 위험 (스퀴즈 주의)
        elif all_positive:
            signal = "LONG_BIAS"
        elif all_negative:
            signal = "SHORT_BIAS"
        
        return {
            'rates': rates,
            'current': rates[-1],
            'signal': signal,
            'rates_str': ' → '.join([f"{r*100:.4f}%" for r in rates])
        }

    # =====================================================
    # 📈 롱/숏 비율 (Long-Short Ratio)
    # =====================================================
    def get_global_ls_ratio(self, symbol: str, interval: str = "1h", limit: int = 1):
        """글로벌 계좌 롱/숏 비율"""
        # symbol에서 USDT 붙이기
        clean = symbol.upper() + "USDT" if "USDT" not in symbol.upper() else symbol.upper()
        data = self._request(
            "/futures/global-long-short-account-ratio/history",
            {"exchange": "Binance", "symbol": clean, "interval": interval, "limit": limit},
            cache_ttl=120
        )
        if not data:
            return None
        
        latest = data[-1] if isinstance(data, list) else data
        long_pct = float(latest.get('global_account_long_percent', 
                         latest.get('longRatio', 50)))
        short_pct = float(latest.get('global_account_short_percent', 
                          latest.get('shortRatio', 50)))
        ratio = float(latest.get('global_account_long_short_ratio', 
                      latest.get('longShortRatio', long_pct / (short_pct + 1e-9))))
        
        return {
            'long_pct': round(long_pct, 1),
            'short_pct': round(short_pct, 1),
            'ratio': round(ratio, 2)
        }
    
    def get_top_trader_ls_ratio(self, symbol: str, interval: str = "1h", limit: int = 1):
        """탑 트레이더 롱/숏 비율"""
        clean = symbol.upper() + "USDT" if "USDT" not in symbol.upper() else symbol.upper()
        data = self._request(
            "/futures/top-long-short-account-ratio/history",
            {"exchange": "Binance", "symbol": clean, "interval": interval, "limit": limit},
            cache_ttl=120
        )
        if not data:
            return None
        
        latest = data[-1] if isinstance(data, list) else data
        long_pct = float(latest.get('longRatio', latest.get('longAccount', 50)))
        short_pct = float(latest.get('shortRatio', latest.get('shortAccount', 50)))
        ratio = round(long_pct / (short_pct + 1e-9), 2)
        
        return {
            'long_pct': round(long_pct, 1),
            'short_pct': round(short_pct, 1),
            'ratio': ratio
        }

    # =====================================================
    # 💥 청산 데이터 (Liquidation)
    # =====================================================
    def get_funding_rate_history(self, symbol: str, interval: str = "8h", limit: int = 6):
        """펀딩비 OHLC 히스토리 (기본 48시간 = 6개 × 8h)"""
        return self._request(
            "/futures/funding-rate/oi-weight-history",  # ⬅️ 여기 URL 수정
            {"symbol": symbol, "interval": interval, "limit": limit},
            cache_ttl=300
        )
    def get_liquidation_history(self, symbol: str, interval: str = "1h", limit: int = 24):
        """코인별 청산 히스토리"""
        return self._request(
            "/futures/liquidation/aggregated-history",
            {
                "symbol": symbol, 
                "interval": interval, 
                "limit": limit,
                "exchange_list": "Binance,OKX,Bybit"  # ⬅️ 필수 파라미터 추가!
            },
            cache_ttl=60
        )
    # def get_liquidation_map(self, symbol: str):
    #     """
    #     청산 맵 (가격대별 청산 물량)
    #     - 어디에 큰 청산 물량이 쌓여있는지 확인
    #     """
    #     return self._request(
    #         "/futures/liquidation/aggregated-map",
    #         {"symbol": symbol},
    #         cache_ttl=300
    #     )
    
    def get_liquidation_map(self, symbol: str):
        """
        청산 맵 (가격대별 청산 물량)
        - 어디에 큰 청산 물량(롱/숏)이 쌓여있는지 확인
        - 큰 물량이 모여있는 가격대로 가격이 끌려갈 가능성
        """
        return self._request(
            "/futures/liquidation/aggregated-map",
            {"symbol": symbol},
            cache_ttl=300
        )

    def get_liquidation_heatmap(self, symbol: str, range_type: str = "3d"):
        """
        청산 히트맵 (시간+가격 2D 분포)
        range_type: "1d", "3d", "7d", "14d", "30d"
        """
        return self._request(
            "/futures/liquidation/aggregated-heatmap",
            {"symbol": symbol, "range_type": range_type},
            cache_ttl=300
        )

    def analyze_liquidation_clusters(self, symbol: str, current_price: float = None):
        """
        🔥 청산 클러스터 분석 — 가격대별 청산 물량 집중도 파악
        
        반환:
        {
            'current_price': 현재가,
            'nearest_long_liq': {  # 현재가 아래 가장 큰 롱 청산 클러스터
                'price': 가격, 'volume_usd': 금액, 'distance_pct': 거리%
            },
            'nearest_short_liq': {  # 현재가 위 가장 큰 숏 청산 클러스터
                'price': 가격, 'volume_usd': 금액, 'distance_pct': 거리%
            },
            'gravity_direction': 'UP' / 'DOWN' / 'NEUTRAL',  # 끌어당기는 방향
            'gravity_strength': 0~10,  # 끌어당기는 강도
            'message': 요약 메시지,
            'long_clusters': [...],   # 아래쪽 롱 청산 클러스터 목록
            'short_clusters': [...],  # 위쪽 숏 청산 클러스터 목록
        }
        """
        result = {
            'current_price': current_price or 0,
            'nearest_long_liq': None,
            'nearest_short_liq': None,
            'gravity_direction': 'NEUTRAL',
            'gravity_strength': 0,
            'message': '',
            'long_clusters': [],
            'short_clusters': [],
            'total_long_liq_usd': 0,
            'total_short_liq_usd': 0,
        }

        # 청산맵 데이터 가져오기
        raw = self.get_liquidation_map(symbol)
        if not raw:
            # 맵이 안 되면 히트맵 시도
            raw = self.get_liquidation_heatmap(symbol, "3d")
        if not raw:
            result['message'] = "청산맵 데이터 없음"
            return result

        # API 응답 파싱 (코인글라스 형식에 맞춰 유연하게)
        long_clusters = []   # 현재가 아래 = 롱 포지션 청산 (가격 하락 시)
        short_clusters = []  # 현재가 위 = 숏 포지션 청산 (가격 상승 시)

        # 응답이 리스트인 경우
        data_list = raw if isinstance(raw, list) else raw.get('data', raw.get('list', []))

        if isinstance(data_list, dict):
            # 딕셔너리 형태: {'longs': [...], 'shorts': [...]}
            long_data = data_list.get('longs', data_list.get('longList', []))
            short_data = data_list.get('shorts', data_list.get('shortList', []))

            for item in long_data:
                price = float(item.get('price', item.get('p', 0)))
                vol = float(item.get('volUsd', item.get('v', item.get('liquidationUsd', 0))))
                if price > 0 and vol > 0:
                    long_clusters.append({'price': price, 'volume_usd': vol})

            for item in short_data:
                price = float(item.get('price', item.get('p', 0)))
                vol = float(item.get('volUsd', item.get('v', item.get('liquidationUsd', 0))))
                if price > 0 and vol > 0:
                    short_clusters.append({'price': price, 'volume_usd': vol})

        elif isinstance(data_list, list):
            # 리스트 형태: [{'price': ..., 'longLiqUsd': ..., 'shortLiqUsd': ...}, ...]
            for item in data_list:
                price = float(item.get('price', item.get('p', 0)))
                long_vol = float(item.get('longLiqUsd', item.get('longVolUsd',
                                item.get('buyVolUsd', item.get('longs', 0)))))
                short_vol = float(item.get('shortLiqUsd', item.get('shortVolUsd',
                                 item.get('sellVolUsd', item.get('shorts', 0)))))

                if price > 0:
                    if long_vol > 0:
                        long_clusters.append({'price': price, 'volume_usd': long_vol})
                    if short_vol > 0:
                        short_clusters.append({'price': price, 'volume_usd': short_vol})

        if not long_clusters and not short_clusters:
            result['message'] = "청산맵 파싱 불가 (API 응답 형식 확인 필요)"
            result['raw_sample'] = str(raw)[:500]
            return result

        # 현재가 기준 거리 계산
        if current_price and current_price > 0:
            cp = current_price
        elif long_clusters or short_clusters:
            all_prices = [c['price'] for c in long_clusters + short_clusters]
            cp = np.median(all_prices) if all_prices else 0
        else:
            cp = 0

        result['current_price'] = cp

        # 거리 계산 + 정렬
        for c in long_clusters:
            c['distance_pct'] = round((cp - c['price']) / cp * 100, 2) if cp > 0 else 0
        for c in short_clusters:
            c['distance_pct'] = round((c['price'] - cp) / cp * 100, 2) if cp > 0 else 0

        # 큰 물량순 정렬
        long_clusters.sort(key=lambda x: x['volume_usd'], reverse=True)
        short_clusters.sort(key=lambda x: x['volume_usd'], reverse=True)

        result['long_clusters'] = long_clusters[:10]  # 상위 10개
        result['short_clusters'] = short_clusters[:10]
        result['total_long_liq_usd'] = sum(c['volume_usd'] for c in long_clusters)
        result['total_short_liq_usd'] = sum(c['volume_usd'] for c in short_clusters)

        # 가장 가까운 큰 클러스터 찾기 (현재가 ±5% 이내에서)
        nearby_long = [c for c in long_clusters if 0 < c.get('distance_pct', 99) <= 5]
        nearby_short = [c for c in short_clusters if 0 < c.get('distance_pct', 99) <= 5]

        if nearby_long:
            best_long = max(nearby_long, key=lambda x: x['volume_usd'])
            result['nearest_long_liq'] = best_long
        elif long_clusters:
            result['nearest_long_liq'] = long_clusters[0]

        if nearby_short:
            best_short = max(nearby_short, key=lambda x: x['volume_usd'])
            result['nearest_short_liq'] = best_short
        elif short_clusters:
            result['nearest_short_liq'] = short_clusters[0]

        # 🧲 중력(Gravity) 판정 — 어느 쪽으로 끌려갈지
        total_long = result['total_long_liq_usd']
        total_short = result['total_short_liq_usd']

        # 가까운 클러스터의 금액으로 판단 (거리 반비례 가중)
        long_gravity = sum(c['volume_usd'] / max(c.get('distance_pct', 1), 0.1)
                          for c in nearby_long) if nearby_long else 0
        short_gravity = sum(c['volume_usd'] / max(c.get('distance_pct', 1), 0.1)
                           for c in nearby_short) if nearby_short else 0

        total_gravity = long_gravity + short_gravity
        if total_gravity > 0:
            if long_gravity > short_gravity * 1.5:
                result['gravity_direction'] = 'DOWN'  # 아래에 큰 롱 청산 → 하락 유인
                result['gravity_strength'] = min(10, int(long_gravity / short_gravity * 3)) if short_gravity > 0 else 8
            elif short_gravity > long_gravity * 1.5:
                result['gravity_direction'] = 'UP'  # 위에 큰 숏 청산 → 상승 유인
                result['gravity_strength'] = min(10, int(short_gravity / long_gravity * 3)) if long_gravity > 0 else 8
            else:
                result['gravity_direction'] = 'NEUTRAL'
                result['gravity_strength'] = 2

        # 메시지 생성
        msgs = []
        if result['nearest_long_liq']:
            nl = result['nearest_long_liq']
            msgs.append(f"⬇️ 롱청산 ${nl['volume_usd']/1e6:.1f}M @{nl['price']:.4f} ({nl['distance_pct']:.1f}% 아래)")
        if result['nearest_short_liq']:
            ns = result['nearest_short_liq']
            msgs.append(f"⬆️ 숏청산 ${ns['volume_usd']/1e6:.1f}M @{ns['price']:.4f} ({ns['distance_pct']:.1f}% 위)")

        gravity_emoji = {"UP": "🧲⬆️", "DOWN": "🧲⬇️", "NEUTRAL": "⚖️"}
        gravity_str = gravity_emoji.get(result['gravity_direction'], '⚖️')
        msgs.append(f"{gravity_str} 중력: {result['gravity_direction']} (강도 {result['gravity_strength']}/10)")

        result['message'] = " | ".join(msgs)
        return result
    
    def get_recent_liquidation_pressure(self, symbol: str):
        """최근 1시간 청산 압력 분석"""
        data = self.get_liquidation_history(symbol, "1h", limit=2)
        if not data or len(data) < 1:
            return None
        
        latest = data[-1]
        long_liq = float(latest.get('longLiquidationUsd', latest.get('buyVolUsd', 0)))
        short_liq = float(latest.get('shortLiquidationUsd', latest.get('sellVolUsd', 0)))
        total = long_liq + short_liq
        
        # 어느 쪽이 더 많이 청산되고 있는지
        if total == 0:
            dominant = "NONE"
        elif long_liq > short_liq * 1.5:
            dominant = "LONG_LIQUIDATION"  # 롱이 청산됨 → 하락 중
        elif short_liq > long_liq * 1.5:
            dominant = "SHORT_LIQUIDATION"  # 숏이 청산됨 → 상승 중
        else:
            dominant = "BALANCED"
        
        return {
            'long_liquidation_usd': round(long_liq, 0),
            'short_liquidation_usd': round(short_liq, 0),
            'total_usd': round(total, 0),
            'dominant': dominant
        }

    # =====================================================
    # 📦 Taker Buy/Sell (시장가 매수/매도)
    # =====================================================
    def get_taker_buy_sell(self, symbol: str, interval: str = "1h", limit: int = 6):
        """Taker 매수/매도 비율 히스토리"""
        return self._request(
            "/futures/aggregated-taker-buy-sell-volume/history",
            {
                "symbol": symbol, 
                "interval": interval, 
                "limit": limit,
                "exchange_list": "Binance,OKX,Bybit"  # ⬅️ 필수 파라미터 추가!
            },
            cache_ttl=120
        )
    
    def get_taker_pressure(self, symbol: str):
        """
        Taker 매수/매도 압력 분석
        - Sell/Buy > 1.3 → 매도 우위
        - Buy/Sell > 1.3 → 매수 우위
        - 3연속 같은 방향이면 강화 신호
        """
        data = self.get_taker_buy_sell(symbol, "1h", limit=4)
        if not data or len(data) < 3:
            return None
        
        ratios = []
        for d in data[-3:]:
            buy_vol = float(d.get('buyVol', d.get('buy', 1)))
            sell_vol = float(d.get('sellVol', d.get('sell', 1)))
            ratio = sell_vol / (buy_vol + 1e-9)
            ratios.append(ratio)
        
        current_ratio = ratios[-1]
        consecutive_sell = all(r > 1.1 for r in ratios)
        consecutive_buy = all(r < 0.9 for r in ratios)
        
        if consecutive_sell:
            signal = "STRONG_SELL_PRESSURE"
        elif current_ratio > 1.3:
            signal = "SELL_PRESSURE"
        elif consecutive_buy:
            signal = "STRONG_BUY_PRESSURE"
        elif current_ratio < 0.7:
            signal = "BUY_PRESSURE"
        else:
            signal = "NEUTRAL"
        
        return {
            'sell_buy_ratio': round(current_ratio, 2),
            'ratios_3h': [round(r, 2) for r in ratios],
            'signal': signal,
            'consecutive_sell': consecutive_sell,
            'consecutive_buy': consecutive_buy
        }

    # =====================================================
    # 📊 CVD (누적 거래량 델타)
    # =====================================================
    def get_cvd_history(self, symbol: str, interval: str = "1h", limit: int = 12):
        """CVD 히스토리"""
        return self._request(
            "/futures/aggregated-cvd-history",
            {"symbol": symbol, "interval": interval, "limit": limit},
            cache_ttl=120
        )

    # =====================================================
    # 🌍 시장 센티먼트
    # =====================================================
    def get_fear_greed_index(self):
        """공포탐욕지수"""
        return self._request(
            "/indicator/fear-greed",
            {},
            cache_ttl=600  # 10분 캐시
        )
    
    # =====================================================
    # 🎯 종합 분석 (한 코인에 대한 모든 데이터)
    # =====================================================
    def get_full_analysis(self, symbol: str):
        """
        한 코인에 대해 모든 핵심 데이터를 수집하고 종합 판정
        API 호출: 약 6~8회
        """
        result = {
            'symbol': symbol,
            'timestamp': datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        # 1. OI 변화
        oi = self.get_oi_change(symbol, "1h")
        result['oi'] = oi
        
        # 2. 펀딩비 트렌드
        funding = self.get_funding_rate_trend(symbol)
        result['funding'] = funding
        
        # 3. 글로벌 LS 비율
        global_ls = self.get_global_ls_ratio(symbol)
        result['global_ls'] = global_ls
        
        # 4. 탑 트레이더 LS 비율
        top_ls = self.get_top_trader_ls_ratio(symbol)
        result['top_ls'] = top_ls
        
        # 5. 청산 압력
        liq = self.get_recent_liquidation_pressure(symbol)
        result['liquidation'] = liq
        
        # 6. Taker 압력
        taker = self.get_taker_pressure(symbol)
        result['taker'] = taker

        # 7. 🔥 청산 클러스터 (청산맵)
        try:
            liq_map = self.analyze_liquidation_clusters(symbol)
            result['liq_clusters'] = liq_map
        except Exception as e:
            result['liq_clusters'] = {'message': f'청산맵 오류: {e}'}
        
        # === 종합 숏 점수 (Short Conviction Score) ===
        short_score = 0
        reasons = []
        
        # OI: 하락 중이면 숏 유리
        if oi and oi['trend'] == 'DOWN':
            short_score += 1
            reasons.append("OI 감소(자본이탈)")
        elif oi and oi['trend'] == 'UP':
            short_score -= 1
        
        # 펀딩비: 롱 과열이면 숏 유리
        if funding:
            if funding['signal'] == 'LONG_OVERHEATED':
                short_score += 2
                reasons.append(f"펀딩비 롱과열({funding['rates_str']})")
            elif funding['signal'] == 'SHORT_OVERHEATED':
                short_score -= 2
            elif funding['signal'] == 'LONG_BIAS':
                short_score += 1
                reasons.append("펀딩비 양수(롱 우위)")
        
        # LS 비율: 개미 롱 쏠림이면 숏 유리
        if global_ls and global_ls['ratio'] > 1.5:
            short_score += 1
            reasons.append(f"개미 롱쏠림({global_ls['long_pct']}%)")
        elif global_ls and global_ls['ratio'] < 0.7:
            short_score -= 1
        
        # 탑 트레이더: 고수가 숏이면 따라감
        if top_ls and top_ls['ratio'] < 0.8:
            short_score += 1
            reasons.append(f"고수 숏포지션({top_ls['short_pct']}%)")
        elif top_ls and top_ls['ratio'] > 1.5:
            short_score -= 1
        
        # 청산: 롱 청산이 많으면 하락 추세
        if liq and liq['dominant'] == 'LONG_LIQUIDATION':
            short_score += 1
            reasons.append("롱 청산 우위")
        elif liq and liq['dominant'] == 'SHORT_LIQUIDATION':
            short_score -= 1
        
        # Taker: 매도 압력이면 숏 유리
        if taker:
            if taker['signal'] == 'STRONG_SELL_PRESSURE':
                short_score += 2
                reasons.append("Taker 강한 매도압력")
            elif taker['signal'] == 'SELL_PRESSURE':
                short_score += 1
                reasons.append("Taker 매도우위")
            elif taker['signal'] in ('STRONG_BUY_PRESSURE', 'BUY_PRESSURE'):
                short_score -= 1
        
        # 7. 🧲 청산맵 중력 반영
        liq_map = result.get('liq_clusters', {})
        gravity = liq_map.get('gravity_direction', 'NEUTRAL')
        gravity_str = liq_map.get('gravity_strength', 0)
        if gravity == 'DOWN' and gravity_str >= 5:
            short_score += 1
            reasons.append(f"🧲 청산맵 하락유인 (강도{gravity_str})")
        elif gravity == 'UP' and gravity_str >= 5:
            short_score -= 1
            reasons.append(f"🧲 청산맵 상승유인 (강도{gravity_str})")
        
        result['short_conviction'] = short_score
        result['short_reasons'] = reasons
        
        # 판정
        if short_score >= 4:
            result['verdict'] = "🔥 STRONG SHORT (숏 강력 추천)"
        elif short_score >= 2:
            result['verdict'] = "📉 SHORT FAVORABLE (숏 유리)"
        elif short_score <= -3:
            result['verdict'] = "🚨 SHORT DANGER (숏 위험! 청산 주의)"
        elif short_score <= -1:
            result['verdict'] = "⚠️ CAUTION (숏 불리, 주의)"
        else:
            result['verdict'] = "😐 NEUTRAL (관망)"
        
        return result
    
    def format_telegram_msg(self, analysis: dict) -> str:
        """종합 분석 결과를 텔레그램 메시지로 포맷팅"""
        a = analysis
        lines = [
            f"🔬 [{a['symbol']}] 온체인 종합 분석",
            f"⏰ {a['timestamp']}",
            f"{'='*30}",
            f"",
            f"📊 숏 확신 점수: {a['short_conviction']:+d}/8",
            f"💡 판정: {a['verdict']}",
            f"",
        ]
        
        if a.get('short_reasons'):
            lines.append("📋 근거:")
            for r in a['short_reasons']:
                lines.append(f"  • {r}")
            lines.append("")
        
        # OI
        if a.get('oi'):
            oi = a['oi']
            lines.append(f"📈 OI: {oi['trend']} ({oi['change_pct']:+.1f}%)")
        
        # 펀딩비
        if a.get('funding'):
            f = a['funding']
            lines.append(f"💸 펀딩비: {f['rates_str']}")
            lines.append(f"   상태: {f['signal']}")
        
        # LS 비율
        if a.get('global_ls'):
            gl = a['global_ls']
            lines.append(f"👥 개미 L/S: 롱{gl['long_pct']}% / 숏{gl['short_pct']}%")
        if a.get('top_ls'):
            tl = a['top_ls']
            lines.append(f"🐋 고수 L/S: 롱{tl['long_pct']}% / 숏{tl['short_pct']}%")
        
        # 청산
        if a.get('liquidation'):
            liq = a['liquidation']
            lines.append(f"💥 청산: 롱${liq['long_liquidation_usd']:,.0f} / 숏${liq['short_liquidation_usd']:,.0f}")
        
        # Taker
        if a.get('taker'):
            t = a['taker']
            lines.append(f"🔄 Taker 매도/매수: {t['sell_buy_ratio']:.2f} ({t['signal']})")
        
        # 🧲 청산맵 클러스터
        if a.get('liq_clusters'):
            lc = a['liq_clusters']
            lines.append(f"")
            lines.append(f"🧲 청산맵 분석:")
            if lc.get('nearest_long_liq'):
                nl = lc['nearest_long_liq']
                lines.append(f"  ⬇️ 롱청산 ${nl['volume_usd']/1e6:.1f}M @{nl['price']:.4f} ({nl.get('distance_pct',0):.1f}%↓)")
            if lc.get('nearest_short_liq'):
                ns = lc['nearest_short_liq']
                lines.append(f"  ⬆️ 숏청산 ${ns['volume_usd']/1e6:.1f}M @{ns['price']:.4f} ({ns.get('distance_pct',0):.1f}%↑)")
            gdir = lc.get('gravity_direction', 'NEUTRAL')
            gstr = lc.get('gravity_strength', 0)
            gemoji = {"UP": "🧲⬆️", "DOWN": "🧲⬇️", "NEUTRAL": "⚖️"}.get(gdir, "⚖️")
            lines.append(f"  {gemoji} 중력: {gdir} (강도 {gstr}/10)")
            if lc.get('total_long_liq_usd') or lc.get('total_short_liq_usd'):
                lines.append(f"  💰 총 롱청산 ${lc.get('total_long_liq_usd',0)/1e6:.1f}M / 숏청산 ${lc.get('total_short_liq_usd',0)/1e6:.1f}M")
        
        return "\n".join(lines)


# =====================================================
# 사용 예시
# =====================================================
if __name__ == "__main__":
    # 테스트
    client = CoinGlassClient("d1423eeda6184ca0bbb163281e996467")
    
    analysis = client.get_full_analysis("COMP")
    print(client.format_telegram_msg(analysis))
