"""
download_data_v2.py — 바이낸스 선물 15분봉 데이터 다운로드 (확장판)
═══════════════════════════════════════════════════════════════
변경점 (v1 대비):
  1. 바이낸스 선물 전체 USDT 코인 자동 감지 (--auto)
  2. 3년 기본 (--months 36)
  3. 기존 데이터 있으면 이어받기 (--append, 기본 ON)
  4. 최소 거래량/OI 필터 (쓰레기 코인 제외)
  5. 진행률 + 예상 시간 표시

사용법:
  python download_data_v2.py                     # 기존 28코인 3년
  python download_data_v2.py --auto              # 바이낸스 전체 코인 자동
  python download_data_v2.py --auto --months 36  # 전체 3년
  python download_data_v2.py --coins BTC ETH SOL # 특정 코인만
  python download_data_v2.py --auto --min-volume 50  # 일 거래량 $50M 이상만
  python download_data_v2.py --list              # 다운 가능 코인 목록만 출력
"""

import pandas as pd, numpy as np
import os, sys, time, json
from datetime import datetime, timedelta

try:
    import ccxt
except ImportError:
    print("ccxt 설치 필요: pip install ccxt")
    sys.exit(1)

DATA_DIR = "./Raw_Data/CRYPTO_BINANCE_15M"

DEFAULT_COINS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT",
    "LTC", "LINK", "UNI", "BCH", "ALGO", "ATOM", "FIL", "VET",
    "TRX", "XLM", "1000PEPE", "MATIC", "COMP", "APT", "ARB",
    "OP", "NEAR", "FTM", "SAND", "MANA"
]

# 제외할 코인 (레버리지 토큰, 스테이블 등)
EXCLUDE_PATTERNS = [
    'UP', 'DOWN', 'BULL', 'BEAR',  # 레버리지 토큰
    'BUSD', 'TUSD', 'FDUSD',       # 스테이블
    'BTCDOM', 'DEFI',              # 인덱스
]


def get_binance_futures_coins(exchange, min_volume_m=0):
    """바이낸스 선물 USDT 페어 전체 자동 감지"""
    print("  바이낸스 선물 코인 조회 중...")
    for retry in range(3):
        try:
            markets = exchange.load_markets()
            break
        except Exception as e:
            if '418' in str(e) or 'ban' in str(e).lower() or 'ddos' in str(e).lower():
                wait = 120 * (retry + 1)
                print(f"  ⚠️ API 제한 — {wait}초 대기 후 재시도 ({retry+1}/3)")
                time.sleep(wait)
            else:
                raise
    else:
        print("  ❌ 3회 재시도 실패. 5분 후 다시 실행하세요.")
        return []

    coins = []
    for sym, info in markets.items():
        # ccxt 선물 심볼: BTC/USDT:USDT 형식
        if '/USDT' not in sym: continue
        if info.get('type') not in ('swap', 'future'): continue
        if not info.get('active', True): continue
        if info.get('linear') is False: continue  # 인버스 제외

        base = info.get('base', '')
        # 제외 패턴
        skip = False
        for pat in EXCLUDE_PATTERNS:
            if pat in base.upper():
                skip = True; break
        if skip: continue

        coins.append({
            'base': base,
            'symbol': sym,           # BTC/USDT:USDT (ccxt 전체 심볼)
            'trade_sym': f"{base}/USDT",  # BTC/USDT (다운로드용)
        })

    print(f"  선물 코인 감지: {len(coins)}개")

    # 거래량 필터 (24h)
    if min_volume_m > 0:
        print(f"  24h 거래량 필터: ≥${min_volume_m}M...")
        try:
            tickers = exchange.fetch_tickers()
            filtered = []
            for c in coins:
                # ccxt 심볼로 조회 (BTC/USDT:USDT)
                t = tickers.get(c['symbol'], {})
                if not t:
                    # fallback: BTC/USDT로도 시도
                    t = tickers.get(c['trade_sym'], {})
                vol_usd = float(t.get('quoteVolume', 0) or 0) / 1e6
                c['volume_24h_m'] = round(vol_usd, 1)
                if vol_usd >= min_volume_m:
                    filtered.append(c)
            coins = sorted(filtered, key=lambda x: -x['volume_24h_m'])
            print(f"  거래량 필터 후: {len(coins)}개")
            # 상위 10개 출력
            for c in coins[:10]:
                print(f"    {c['base']:>12} ${c['volume_24h_m']:>8.1f}M")
            if len(coins) > 10:
                print(f"    ... 외 {len(coins)-10}개")
        except Exception as e:
            print(f"  거래량 조회 실패: {e} — 필터 없이 진행")

    return [c['base'] for c in coins]


def download_coin(exchange, coin, months, data_dir, append=True):
    """단일 코인 다운로드 (이어받기 지원)"""
    sym = f"{coin}/USDT"
    fname = f"{coin}_USDT.csv"
    fpath = os.path.join(data_dir, fname)

    # 이어받기: 기존 데이터의 마지막 시점부터
    existing_len = 0
    if append and os.path.exists(fpath):
        try:
            existing = pd.read_csv(fpath)
            if len(existing) > 10:
                existing['Date'] = pd.to_datetime(existing['Date'])
                last_date = existing['Date'].max()
                since = int(last_date.timestamp() * 1000) + 1
                existing_len = len(existing)
            else:
                since = int((datetime.now() - timedelta(days=months * 30)).timestamp() * 1000)
        except:
            since = int((datetime.now() - timedelta(days=months * 30)).timestamp() * 1000)
    else:
        since = int((datetime.now() - timedelta(days=months * 30)).timestamp() * 1000)

    try:
        all_ohlcv = []
        s = since
        now_ms = int(datetime.now().timestamp() * 1000)

        while s < now_ms:
            try:
                batch = exchange.fetch_ohlcv(sym, '15m', since=s, limit=1500)
            except Exception as api_err:
                err_s = str(api_err).lower()
                if '418' in err_s or 'ban' in err_s or 'ddos' in err_s or '429' in err_s:
                    # IP 밴 → 대기 후 재시도
                    print(f"\n  ⚠️ API 제한 감지 — 120초 대기 중...", flush=True)
                    time.sleep(120)
                    continue  # 재시도
                raise  # 다른 에러는 그대로

            if not batch:
                break
            all_ohlcv.extend(batch)
            s = batch[-1][0] + 1
            if s >= now_ms:
                break
            time.sleep(0.5)  # rate limit (0.15→0.5초)

        if not all_ohlcv and existing_len > 0:
            return existing_len, 0, True  # 이미 최신

        new_df = pd.DataFrame(all_ohlcv, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        new_df['Date'] = pd.to_datetime(new_df['Date'], unit='ms')

        # 이어붙이기
        if append and existing_len > 0:
            existing = pd.read_csv(fpath)
            existing['Date'] = pd.to_datetime(existing['Date'])
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset='Date').sort_values('Date').reset_index(drop=True)
        else:
            combined = new_df.drop_duplicates(subset='Date').sort_values('Date').reset_index(drop=True)

        combined.to_csv(fpath, index=False)
        added = len(combined) - existing_len
        return len(combined), added, True

    except Exception as e:
        return existing_len, 0, False


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Binance Futures 15m Data Downloader v2')
    parser.add_argument('--months', type=int, default=36, help='다운로드 기간 (월, 기본 36)')
    parser.add_argument('--coins', nargs='+', help='특정 코인만')
    parser.add_argument('--auto', action='store_true', help='바이낸스 전체 코인 자동 감지')
    parser.add_argument('--min-volume', type=float, default=0, help='최소 24h 거래량 ($M)')
    parser.add_argument('--no-append', action='store_true', help='이어받기 비활성 (전체 재다운)')
    parser.add_argument('--list', action='store_true', help='코인 목록만 출력')
    parser.add_argument('--data-dir', default=DATA_DIR)
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)

    ex = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

    # 코인 목록 결정
    if args.coins:
        coins = [c.upper() for c in args.coins]
    elif args.auto:
        coins = get_binance_futures_coins(ex, min_volume_m=args.min_volume)
    else:
        coins = DEFAULT_COINS

    if args.list:
        print(f"\n다운로드 가능 코인: {len(coins)}개")
        for i, c in enumerate(coins, 1):
            exists = "✅" if os.path.exists(os.path.join(args.data_dir, f"{c}_USDT.csv")) else "  "
            print(f"  {exists} {i:>3}. {c}")
        return

    total = len(coins)
    append = not args.no_append

    print(f"\n{'='*60}")
    print(f"  Binance Futures 15m Downloader V2")
    print(f"  코인: {total}개 | 기간: ~{args.months}개월")
    print(f"  이어받기: {'ON' if append else 'OFF'}")
    print(f"  저장: {os.path.abspath(args.data_dir)}/")
    print(f"{'='*60}\n")

    t0 = time.time()
    success = 0; total_candles = 0; total_added = 0

    for idx, coin in enumerate(coins, 1):
        elapsed = time.time() - t0
        eta = (elapsed / max(idx-1, 1)) * (total - idx) if idx > 1 else 0
        eta_s = f"{eta/60:.0f}분" if eta > 60 else f"{eta:.0f}초"

        print(f"  [{idx}/{total}] {coin:>12}/USDT", end=" ", flush=True)

        n_total, n_added, ok = download_coin(ex, coin, args.months, args.data_dir, append)

        if ok:
            if n_added == 0:
                print(f"최신 ({n_total:>6}봉) ETA:{eta_s}")
            else:
                print(f"+{n_added:>5}봉 → {n_total:>6}봉  ETA:{eta_s}")
            success += 1
            total_candles += n_total
            total_added += n_added
        else:
            print(f"실패  ETA:{eta_s}")

        # 코인 간 대기 (밴 방지)
        time.sleep(1.0)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  완료: {success}/{total} ({elapsed/60:.1f}분)")
    print(f"  총 캔들: {total_candles:,} (신규 +{total_added:,})")
    print(f"{'='*60}")

    # 최종 파일 목록
    import glob
    csvs = glob.glob(f"{args.data_dir}/*.csv")
    print(f"\n  CSV 파일: {len(csvs)}개")
    sizes = []
    for c in sorted(csvs):
        df = pd.read_csv(c)
        sizes.append(len(df))
    if sizes:
        print(f"  캔들 수: {min(sizes):,} ~ {max(sizes):,} (평균 {np.mean(sizes):,.0f})")


if __name__ == "__main__":
    main()
