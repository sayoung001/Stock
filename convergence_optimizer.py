"""
convergence_optimizer.py — 수렴 돌파 전략 최적화 마스터
══════════════════════════════════════════════════════════

Phase 1: 코인 30개 분류 → 5그룹
Phase 2: 진입/청산 최적화 (SL × TP × 스퀴즈 그리드서치)
Phase 3: 리스크 스윕 (5% ~ 15%)
Phase 4: 기간별 분석 (상승/하락/보합/변동)
Phase 5: 최종 선별 + 전체 기간 검증

사용법:
  # 전체 파이프라인 (순차 실행)
  %run convergence_optimizer.py

  # 개별 Phase 실행 (Jupyter)
  import convergence_optimizer as opt
  opt.run_phase1()                    # 코인 분류
  opt.run_phase2(coins=[...])         # 그리드서치
  opt.run_phase3(best_config={...})   # 리스크 스윕
  opt.run_phase4(best_config={...})   # 기간별 분석
  opt.run_phase5()                    # 최종 검증
"""

import pandas as pd, numpy as np, os, sys, json, glob, time
from datetime import datetime, timezone, timedelta
from itertools import product
from tqdm.auto import tqdm

sys.path.insert(0, '.')
from convergence_strategy import ConvergenceBreakout, CONVERGENCE_CONFIG

KST = timezone(timedelta(hours=9))
DATA_DIR = "./Raw_Data/CRYPTO_BINANCE_15M"
RESULT_DIR = "./Results/convergence_optimizer"
os.makedirs(RESULT_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
#  공통 백테스트 엔진 (경량 버전)
# ═══════════════════════════════════════════════════════════════
def fast_backtest(coins, config, risk_per_trade=0.015, leverage=20,
                  fee_rate=0.0004, max_pos_usdt=None, start_date=None, end_date=None,
                  show_progress=False):
    """
    빠른 백테스트 — 코인 목록 + CONFIG → summary dict
    start_date/end_date: 'YYYY-MM-DD' 형식으로 기간 제한 가능
    """
    cfg = {**CONVERGENCE_CONFIG, **config}
    detector = ConvergenceBreakout(cfg)

    balance = 1000.0
    initial = balance
    peak = balance
    max_dd = 0
    trades = []

    for code in coins:
        path = f"{DATA_DIR}/{code}_USDT.csv"
        if not os.path.exists(path):
            continue

        df = pd.read_csv(path)
        if len(df) < 100:
            continue
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)

        # 기간 필터
        if start_date:
            df = df[df.index >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df.index <= pd.Timestamp(end_date)]
        if len(df) < 100:
            continue

        rng = range(80, len(df))
        if show_progress:
            rng = tqdm(rng, desc=f"    {code}", leave=True)

        for i in rng:
            window = df.iloc[max(0, i-100):i+1].copy()
            if len(window) < 60:
                continue

            result = detector.detect(window)
            if result is None:
                continue
            if result['confidence'] < cfg.get('MIN_CONFIDENCE', 40):
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
            if max_pos_usdt and margin * leverage > max_pos_usdt:
                margin = max_pos_usdt / leverage

            qty = margin * leverage / price
            fee = qty * price * fee_rate * 2

            # ── 청산 시뮬레이션 (4가지 모드) ──
            tp_mode = cfg.get('EXIT_MODE', cfg.get('TP_MODE', 'fibo'))
            future = df.iloc[i+1:min(i+97, len(df))]  # 최대 24시간
            exit_price = price
            exit_reason = "timeout"
            hold_candles = 0
            atr = result['atr'] if result['atr'] > 0 else price * 0.01

            # T-Stop 상태
            trail_active = False
            trail_activate_roe = cfg.get('TRAIL_ACTIVATE_ROE', 5.0)
            trail_mult = cfg.get('TRAIL_MULT', 2.0)
            best_price = price  # 최고/최저 추적
            trail_sl = sl       # 트레일링 SL

            for j, (_, frow) in enumerate(future.iterrows()):
                hold_candles = j + 1
                curr_close = frow['Close']

                # ── 공통: SL 체크 (모든 모드) ──
                if direction == 'long':
                    if frow['Low'] <= trail_sl:
                        exit_price = trail_sl
                        exit_reason = "T-Stop" if trail_active else "SL"
                        break
                else:
                    if frow['High'] >= trail_sl:
                        exit_price = trail_sl
                        exit_reason = "T-Stop" if trail_active else "SL"
                        break

                # ── 모드별 TP 체크 ──
                if tp_mode in ('fibo', 'atr'):
                    # 고정 TP
                    if direction == 'long' and frow['High'] >= tp:
                        exit_price = tp; exit_reason = "TP"; break
                    elif direction == 'short' and frow['Low'] <= tp:
                        exit_price = tp; exit_reason = "TP"; break

                elif tp_mode == 'tstop':
                    # T-Stop Only: TP 없음, 트레일링만
                    spot_pnl = (curr_close - price) / price * 100 if direction == 'long' \
                               else (price - curr_close) / price * 100
                    roe = spot_pnl * leverage

                    # 최고/최저 갱신
                    if direction == 'long':
                        if frow['High'] > best_price:
                            best_price = frow['High']
                    else:
                        if frow['Low'] < best_price:
                            best_price = frow['Low']

                    # 활성화 체크
                    if not trail_active and roe >= trail_activate_roe:
                        trail_active = True

                    # 트레일링 SL 업데이트
                    if trail_active:
                        trail_dist = atr * trail_mult
                        if direction == 'long':
                            new_sl = best_price - trail_dist
                            if new_sl > trail_sl:
                                trail_sl = new_sl
                        else:
                            new_sl = best_price + trail_dist
                            if new_sl < trail_sl:
                                trail_sl = new_sl

                elif tp_mode == 'dynamic':
                    # 동적 TP: SL 거리 × RR 배수
                    dynamic_rr = cfg.get('DYNAMIC_RR_MULT', 2.0)
                    sl_dist = abs(price - trail_sl)
                    if direction == 'long':
                        dynamic_tp = price + sl_dist * dynamic_rr
                        if frow['High'] >= dynamic_tp:
                            exit_price = dynamic_tp; exit_reason = "DynamicTP"; break
                    else:
                        dynamic_tp = price - sl_dist * dynamic_rr
                        if frow['Low'] <= dynamic_tp:
                            exit_price = dynamic_tp; exit_reason = "DynamicTP"; break

                    # 동적 TP에도 T-Stop 추가 (보너스)
                    spot_pnl = (curr_close - price) / price * 100 if direction == 'long' \
                               else (price - curr_close) / price * 100
                    roe = spot_pnl * leverage
                    if direction == 'long':
                        if frow['High'] > best_price: best_price = frow['High']
                    else:
                        if frow['Low'] < best_price: best_price = frow['Low']
                    if not trail_active and roe >= trail_activate_roe:
                        trail_active = True
                    if trail_active:
                        trail_dist = atr * trail_mult
                        if direction == 'long':
                            new_sl = best_price - trail_dist
                            if new_sl > trail_sl: trail_sl = new_sl
                        else:
                            new_sl = best_price + trail_dist
                            if new_sl < trail_sl: trail_sl = new_sl

            if exit_reason == "timeout":
                exit_price = future.iloc[-1]['Close'] if len(future) > 0 else price
                hold_candles = len(future)

            pnl = ((exit_price - price) * qty - fee) if direction == 'long' \
                  else ((price - exit_price) * qty - fee)

            balance += pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100
            max_dd = max(max_dd, dd)

            roe_pct = ((exit_price - price) / price * leverage * 100) if direction == 'long' \
                      else ((price - exit_price) / price * leverage * 100)

            exit_idx = min(i + hold_candles, len(df) - 1)
            exit_time_val = df.index[exit_idx] if exit_idx < len(df) else window.index[-1]

            trades.append({
                'symbol': f"{code}_USDT", 'direction': direction,
                'strategy': 'convergence',
                'entry_price': round(price, 6),
                'exit_price': round(exit_price, 6),
                'stop_loss': round(sl, 6),
                'take_profit': round(tp, 6) if tp_mode in ('fibo','atr') else 0,
                'pnl': pnl, 'fees': fee,
                'net_pnl': pnl,
                'roe_pct': round(roe_pct, 2),
                'reason': exit_reason, 'exit_reason': exit_reason,
                'squeeze': result['squeeze_candles'],
                'confidence': result['confidence'],
                'hold_candles': hold_candles,
                'hold_min': hold_candles * 15,
                'entry_time': str(window.index[-1]),
                'exit_time': str(exit_time_val),
                'tp_mode': tp_mode,
            })

    # 결과 요약
    n = len(trades)
    if n == 0:
        return {'total_trades': 0, 'roi_pct': 0, 'win_rate': 0,
                'max_drawdown_pct': 0, 'profit_factor': 0, 'total_pnl': 0,
                'trades_list': []}

    df_t = pd.DataFrame(trades)
    wins = df_t[df_t['pnl'] > 0]
    losses = df_t[df_t['pnl'] <= 0]
    avg_w = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_l = losses['pnl'].mean() if len(losses) > 0 else 0

    return {
        'total_trades': n,
        'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins) / n * 100, 1),
        'roi_pct': round((balance - initial) / initial * 100, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'profit_factor': round(avg_w / abs(avg_l), 2) if avg_l != 0 else 0,
        'total_pnl': round(balance - initial, 2),
        'avg_win': round(avg_w, 2),
        'avg_loss': round(avg_l, 2),
        'total_fees': round(sum(t['pnl'] for t in trades) - (balance - initial), 2),
        'gross_pnl': round(sum(t['pnl'] for t in trades), 2),
        'tp_count': len(df_t[df_t['reason'] == 'TP']),
        'sl_count': len(df_t[df_t['reason'] == 'SL']),
        'tstop_count': len(df_t[df_t['reason'] == 'T-Stop']),
        'dynamic_tp_count': len(df_t[df_t['reason'] == 'DynamicTP']),
        'timeout_count': len(df_t[df_t['reason'] == 'timeout']),
        'avg_squeeze': round(df_t['squeeze'].mean(), 1),
        'config': config,
        'trades_list': trades,
    }


def get_available_coins():
    """데이터가 있는 코인 목록"""
    csvs = sorted(glob.glob(f"{DATA_DIR}/*.csv"))
    return [os.path.basename(f).replace('_USDT.csv', '') for f in csvs if 'BTC' not in f]


def calc_coin_features(code):
    """코인 특성 계산"""
    path = f"{DATA_DIR}/{code}_USDT.csv"
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    if len(df) < 200:
        return None
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    c = df['Close']; h = df['High']; lo = df['Low']

    tr = pd.concat([h-lo, (h-c.shift(1)).abs(), (lo-c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14).mean()
    atr_pct = (atr / c * 100).iloc[-1]

    up = h.diff(); down = -lo.diff()
    p_dm = up.where((up>down)&(up>0), 0)
    m_dm = down.where((down>up)&(down>0), 0)
    tr_s = tr.ewm(alpha=1/14).mean()
    pdi = 100 * p_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
    mdi = 100 * m_dm.ewm(alpha=1/14).mean() / (tr_s + 1e-9)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-9)
    adx = dx.ewm(alpha=1/14).mean().iloc[-1]

    ema20 = c.ewm(span=20).mean()
    ema60 = c.ewm(span=60).mean()
    ema_gap = abs(ema20.iloc[-1] - ema60.iloc[-1]) / c.iloc[-1] * 100

    # 일평균 거래량(USDT)
    avg_vol = (df['Volume'] * c).tail(96).mean()

    # BTC 상관계수
    btc_path = f"{DATA_DIR}/BTC_USDT.csv"
    btc_corr = 0.5
    if os.path.exists(btc_path):
        btc = pd.read_csv(btc_path)
        btc['Date'] = pd.to_datetime(btc['Date'])
        btc.set_index('Date', inplace=True)
        common = c.index.intersection(btc.index)
        if len(common) > 60:
            btc_corr = c.loc[common].pct_change().tail(200).corr(
                btc['Close'].loc[common].pct_change().tail(200))
            if np.isnan(btc_corr):
                btc_corr = 0.5

    return {
        'code': code,
        'atr_pct': round(atr_pct, 3),
        'adx': round(adx, 1),
        'ema_gap': round(ema_gap, 3),
        'btc_corr': round(btc_corr, 3),
        'avg_vol_usdt': round(avg_vol, 0),
        'candles': len(df),
        'start': str(df.index[0].date()),
        'end': str(df.index[-1].date()),
    }


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 코인 30개 분류
# ═══════════════════════════════════════════════════════════════
def run_phase1():
    """코인 특성 분석 + 5그룹 자동 분류"""
    print(f"\n{'═'*70}")
    print(f"  📊 Phase 1: 코인 분류")
    print(f"{'═'*70}")

    coins = get_available_coins()
    print(f"  데이터 보유 코인: {len(coins)}개")

    features = []
    for code in tqdm(coins, desc="특성 분석"):
        f = calc_coin_features(code)
        if f:
            features.append(f)

    df = pd.DataFrame(features).sort_values('atr_pct')

    # 5그룹 분류 (ATR + ADX + BTC상관 기반)
    groups = {
        'A.초저변동': [],     # ATR < 0.2%
        'B.저변동추세': [],   # ATR 0.2~0.5%, ADX > 20
        'C.중변동메이저': [], # ATR 0.3~0.6%, 거래량 상위
        'D.고변동알트': [],   # ATR > 0.5%
        'E.독립적': [],       # BTC상관 < 0.4
    }

    for _, r in df.iterrows():
        code = r['code']
        if r['btc_corr'] < 0.4:
            groups['E.독립적'].append(code)
        elif r['atr_pct'] < 0.2:
            groups['A.초저변동'].append(code)
        elif r['atr_pct'] <= 0.5 and r['adx'] > 20:
            groups['B.저변동추세'].append(code)
        elif r['avg_vol_usdt'] > 5_000_000:
            groups['C.중변동메이저'].append(code)
        else:
            groups['D.고변동알트'].append(code)

    # 출력
    print(f"\n  {'코인':<12s} {'ATR%':>6s} {'ADX':>5s} {'BTC상관':>7s} {'거래량':>12s} {'기간':>24s}")
    print(f"  {'-'*70}")
    for _, r in df.iterrows():
        print(f"  {r['code']:<12s} {r['atr_pct']:>5.3f}% {r['adx']:>5.1f} {r['btc_corr']:>6.3f} "
              f"${r['avg_vol_usdt']:>10,.0f} {r['start']}~{r['end']}")

    print(f"\n  📊 그룹 분류:")
    for gname, gcoins in groups.items():
        print(f"    {gname}: {len(gcoins)}개 → {gcoins}")

    # 저장
    df.to_csv(f"{RESULT_DIR}/phase1_coin_features.csv", index=False)
    with open(f"{RESULT_DIR}/phase1_groups.json", 'w') as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

    print(f"\n  💾 {RESULT_DIR}/phase1_coin_features.csv")
    print(f"  💾 {RESULT_DIR}/phase1_groups.json")
    return df, groups


# ═══════════════════════════════════════════════════════════════
#  Phase 2: 그리드서치 (SL × TP × 스퀴즈)
# ═══════════════════════════════════════════════════════════════
def run_phase2(coins=None, save=True):
    """
    SL방식 × TP방식 × 스퀴즈캔들 × 피보레벨 그리드서치
    → 최적 조합 찾기
    """
    if coins is None:
        coins = ["AVAX", "TRX", "XRP", "DOGE", "ETH", "SOL"]  # Phase1 수익 코인

    print(f"\n{'═'*70}")
    print(f"  🔬 Phase 2: 그리드서치 (SL × TP × 스퀴즈)")
    print(f"  코인: {coins}")
    print(f"{'═'*70}")

    # 그리드 정의
    grid = {
        'squeeze': [15, 20, 30],
        'fibo': [1.618, 2.0, 2.618, 3.236],
        'sl_mode': ['range', 'atr'],
        'breakout': [1.5, 2.0],
        'tp_mode': ['fibo'],  # ATR은 별도
    }

    # 추가: ATR TP 모드
    extra_configs = [
        {'EXIT_MODE': 'atr', 'TP_MODE': 'atr', 'TP_ATR_MULT': 2.0, 'MIN_SQUEEZE_CANDLES': s}
        for s in grid['squeeze']
    ] + [
        {'EXIT_MODE': 'atr', 'TP_MODE': 'atr', 'TP_ATR_MULT': 3.0, 'MIN_SQUEEZE_CANDLES': s}
        for s in grid['squeeze']
    ]

    # 추가: T-Stop Only 모드
    tstop_configs = []
    for sq in grid['squeeze']:
        for t_roe in [3, 5, 10]:
            for t_mult in [1.5, 2.0, 3.0]:
                tstop_configs.append({
                    'EXIT_MODE': 'tstop',
                    'MIN_SQUEEZE_CANDLES': sq,
                    'TRAIL_ACTIVATE_ROE': t_roe,
                    'TRAIL_MULT': t_mult,
                })

    # 추가: Dynamic TP 모드
    dynamic_configs = []
    for sq in grid['squeeze']:
        for rr in [1.5, 2.0, 2.5, 3.0]:
            for t_roe in [5, 10]:
                dynamic_configs.append({
                    'EXIT_MODE': 'dynamic',
                    'MIN_SQUEEZE_CANDLES': sq,
                    'DYNAMIC_RR_MULT': rr,
                    'TRAIL_ACTIVATE_ROE': t_roe,
                    'TRAIL_MULT': 2.0,
                })

    # 피보 그리드
    fibo_configs = []
    for sq, fib, sl, br in product(grid['squeeze'], grid['fibo'],
                                     grid['sl_mode'], grid['breakout']):
        fibo_configs.append({
            'EXIT_MODE': 'fibo',
            'MIN_SQUEEZE_CANDLES': sq,
            'TP_FIBO_LEVEL': fib,
            'SL_MODE': sl,
            'BREAKOUT_ATR_MULT': br,
        })

    all_configs = fibo_configs + extra_configs + tstop_configs + dynamic_configs
    print(f"  총 {len(all_configs)}개 조합 테스트")
    print(f"    피보: {len(fibo_configs)} | ATR: {len(extra_configs)} | "
          f"T-Stop: {len(tstop_configs)} | Dynamic: {len(dynamic_configs)}")

    results = []
    pbar = tqdm(all_configs, desc="🔬 그리드서치", unit="cfg")
    for cfg in pbar:
        mode = cfg.get('EXIT_MODE', 'fibo')
        sq = cfg.get('MIN_SQUEEZE_CANDLES', '')
        pbar.set_postfix_str(f"{mode} sq{sq}")
        s = fast_backtest(coins, cfg)
        s['config_str'] = str(cfg)
        for k, v in cfg.items():
            s[k] = v
        results.append(s)

    df = pd.DataFrame(results)
    df = df[df['total_trades'] > 0].sort_values('roi_pct', ascending=False)

    # 상위 20개 출력
    print(f"\n{'═'*70}")
    print(f"  🏆 Top 20 조합 (ROI순)")
    print(f"{'═'*70}")
    print(f"  {'거래':>4s} {'승률':>5s} {'ROI':>7s} {'MDD':>6s} {'PF':>5s} "
          f"{'모드':>7s} {'스퀴즈':>5s} {'TP설정':>8s} {'SL':>5s} {'돌파':>4s}")
    print(f"  {'-'*70}")

    for _, r in df.head(20).iterrows():
        mode = r.get('EXIT_MODE', 'fibo')
        if mode == 'fibo':
            tp_str = f"F{r.get('TP_FIBO_LEVEL','')}"
        elif mode == 'atr':
            tp_str = f"A{r.get('TP_ATR_MULT','')}"
        elif mode == 'tstop':
            tp_str = f"T{r.get('TRAIL_ACTIVATE_ROE','')}r{r.get('TRAIL_MULT','')}"
        elif mode == 'dynamic':
            tp_str = f"D{r.get('DYNAMIC_RR_MULT','')}r{r.get('TRAIL_ACTIVATE_ROE','')}"
        else:
            tp_str = mode
        print(f"  {r['total_trades']:>4.0f} {r['win_rate']:>4.1f}% {r['roi_pct']:>+6.2f}% "
              f"{r['max_drawdown_pct']:>5.2f}% {r['profit_factor']:>5.2f} "
              f"{mode:>7s} {r.get('MIN_SQUEEZE_CANDLES',''):>5} {tp_str:>8s} "
              f"{r.get('SL_MODE',''):>5s} {r.get('BREAKOUT_ATR_MULT',''):>4}")

    # 스퀴즈별 평균
    print(f"\n  📊 스퀴즈 캔들별 평균 ROI:")
    for sq in grid['squeeze']:
        sub = df[df.get('MIN_SQUEEZE_CANDLES', 0) == sq]
        if len(sub) > 0:
            print(f"    스퀴즈 {sq:>3d}: 평균ROI {sub['roi_pct'].mean():>+.2f}% "
                  f"최고{sub['roi_pct'].max():>+.2f}% 거래평균{sub['total_trades'].mean():.0f}건")

    # 피보별 평균
    print(f"\n  📊 피보 레벨별 평균 ROI:")
    for fib in grid['fibo']:
        sub = df[df.get('TP_FIBO_LEVEL', 0) == fib]
        if len(sub) > 0:
            print(f"    피보 {fib:>5.3f}: 평균ROI {sub['roi_pct'].mean():>+.2f}% "
                  f"최고{sub['roi_pct'].max():>+.2f}%")

    # SL별 평균
    print(f"\n  📊 SL 방식별 평균 ROI:")
    for sl in grid['sl_mode']:
        sub = df[df.get('SL_MODE', '') == sl]
        if len(sub) > 0:
            print(f"    {sl:>6s}: 평균ROI {sub['roi_pct'].mean():>+.2f}% "
                  f"최고{sub['roi_pct'].max():>+.2f}%")

    # 🔥 청산 모드별 비교 (핵심!)
    print(f"\n{'─'*50}")
    print(f"  🔥 청산 모드별 비교 (핵심)")
    print(f"{'─'*50}")
    print(f"  {'모드':<10s} {'조합수':>5s} {'평균ROI':>8s} {'최고ROI':>8s} {'평균승률':>7s} "
          f"{'평균MDD':>7s} {'평균PF':>6s} {'평균거래':>6s}")
    print(f"  {'-'*65}")
    for mode in ['fibo', 'atr', 'tstop', 'dynamic']:
        sub = df[df.get('EXIT_MODE', 'fibo') == mode]
        if len(sub) == 0:
            continue
        valid = sub[sub['total_trades'] > 0]
        if len(valid) == 0:
            print(f"  {mode:<10s}  거래 없음")
            continue
        e = "🟢" if valid['roi_pct'].mean() > 0 else "🔴"
        print(f"  {e} {mode:<8s} {len(valid):>5d} {valid['roi_pct'].mean():>+7.2f}% "
              f"{valid['roi_pct'].max():>+7.2f}% {valid['win_rate'].mean():>6.1f}% "
              f"{valid['max_drawdown_pct'].mean():>6.2f}% {valid['profit_factor'].mean():>5.2f} "
              f"{valid['total_trades'].mean():>5.0f}")

    # 스퀴즈 × 피보 히트맵
    print(f"\n  📊 스퀴즈 × 피보 ROI 히트맵:")
    print(f"  {'':>8s}", end="")
    for fib in grid['fibo']:
        print(f" F{fib:>5.3f}", end="")
    print()
    for sq in grid['squeeze']:
        print(f"  SQ{sq:>3d}  ", end="")
        for fib in grid['fibo']:
            sub = df[(df.get('MIN_SQUEEZE_CANDLES', 0) == sq) &
                     (df.get('TP_FIBO_LEVEL', 0) == fib)]
            if len(sub) > 0:
                best = sub['roi_pct'].max()
                e = "🟢" if best > 3 else ("🟡" if best > 0 else "🔴")
                print(f" {e}{best:>+4.1f}%", end="")
            else:
                print(f"   ---  ", end="")
        print()

    # 최적 CONFIG
    if len(df) > 0:
        best = df.iloc[0]
        best_config = {k: v for k, v in best.items()
                      if k in ['MIN_SQUEEZE_CANDLES', 'TP_FIBO_LEVEL', 'TP_MODE',
                               'TP_ATR_MULT', 'SL_MODE', 'BREAKOUT_ATR_MULT']}
        print(f"\n  🏆 최적 조합: {best_config}")
        print(f"     ROI {best['roi_pct']:+.2f}% | 승률 {best['win_rate']:.1f}% | "
              f"MDD {best['max_drawdown_pct']:.2f}% | {best['total_trades']:.0f}건")
    else:
        best_config = {}

    if save:
        df.to_csv(f"{RESULT_DIR}/phase2_grid_results.csv", index=False)
        with open(f"{RESULT_DIR}/phase2_best_config.json", 'w') as f:
            json.dump(best_config, f, indent=2, default=str)
        print(f"\n  💾 {RESULT_DIR}/phase2_grid_results.csv")

    return df, best_config


# ═══════════════════════════════════════════════════════════════
#  Phase 3: 리스크 스윕
# ═══════════════════════════════════════════════════════════════
def run_phase3(coins=None, best_config=None):
    """리스크 1%~15% 스윕"""
    if coins is None:
        coins = ["AVAX", "TRX", "XRP", "DOGE", "ETH", "SOL"]
    if best_config is None:
        # Phase2 결과 로드
        try:
            with open(f"{RESULT_DIR}/phase2_best_config.json") as f:
                best_config = json.load(f)
        except:
            best_config = {"TP_FIBO_LEVEL": 2.618, "MIN_SQUEEZE_CANDLES": 15}

    print(f"\n{'═'*70}")
    print(f"  💰 Phase 3: 리스크 스윕")
    print(f"  CONFIG: {best_config}")
    print(f"{'═'*70}")

    risks = [0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.125, 0.15]
    results = []

    for risk in tqdm(risks, desc="💰 리스크 스윕"):
        s = fast_backtest(coins, best_config, risk_per_trade=risk)
        s['risk_pct'] = risk * 100
        results.append(s)

    print(f"\n  {'리스크':>6s} {'거래':>4s} {'승률':>5s} {'ROI':>8s} {'MDD':>7s} "
          f"{'ROI/MDD':>7s} {'PnL':>8s}")
    print(f"  {'-'*55}")
    for r in results:
        ratio = r['roi_pct'] / r['max_drawdown_pct'] if r['max_drawdown_pct'] > 0 else 0
        e = "🟢" if ratio > 3 else ("🟡" if ratio > 1.5 else "🔴")
        print(f"  {e} {r['risk_pct']:>5.1f}% {r['total_trades']:>4d} {r['win_rate']:>4.1f}% "
              f"{r['roi_pct']:>+7.2f}% {r['max_drawdown_pct']:>6.2f}% "
              f"{ratio:>6.2f}x ${r['total_pnl']:>+7.2f}")

    # 최적 리스크 (ROI/MDD 최대)
    best_idx = max(range(len(results)),
                   key=lambda i: results[i]['roi_pct'] / max(results[i]['max_drawdown_pct'], 0.01))
    best = results[best_idx]
    print(f"\n  🏆 최적 리스크: {best['risk_pct']:.1f}% (ROI/MDD 최대)")

    df = pd.DataFrame(results)
    df.to_csv(f"{RESULT_DIR}/phase3_risk_sweep.csv", index=False)
    return df, best['risk_pct'] / 100


# ═══════════════════════════════════════════════════════════════
#  Phase 4: 기간별 분석
# ═══════════════════════════════════════════════════════════════
def run_phase4(coins=None, best_config=None, risk=0.05):
    """
    시장 구간별 성과 분석
    → 상승/하락/보합/변동 각각에서 어떻게 작동하는지
    """
    if coins is None:
        coins = ["AVAX", "TRX", "XRP", "DOGE", "ETH", "SOL"]
    if best_config is None:
        try:
            with open(f"{RESULT_DIR}/phase2_best_config.json") as f:
                best_config = json.load(f)
        except:
            best_config = {"TP_FIBO_LEVEL": 2.618, "MIN_SQUEEZE_CANDLES": 15}

    print(f"\n{'═'*70}")
    print(f"  📅 Phase 4: 기간별 분석")
    print(f"{'═'*70}")

    # BTC 가격으로 시장 구간 자동 분류
    btc_path = f"{DATA_DIR}/BTC_USDT.csv"
    if not os.path.exists(btc_path):
        print("  ❌ BTC 데이터 필요")
        return None

    btc = pd.read_csv(btc_path)
    btc['Date'] = pd.to_datetime(btc['Date'])
    btc.set_index('Date', inplace=True)

    # 월별 BTC 수익률로 시장 구간 분류
    btc_monthly = btc['Close'].resample('M').last().pct_change() * 100
    print(f"\n  📊 BTC 월별 수익률:")

    periods = []
    for date, ret in btc_monthly.items():
        if pd.isna(ret):
            continue
        month_start = date.replace(day=1).strftime('%Y-%m-%d')
        month_end = date.strftime('%Y-%m-%d')

        if ret > 10:
            regime = "🟢상승"
        elif ret > 2:
            regime = "🟡약상승"
        elif ret > -2:
            regime = "⚪보합"
        elif ret > -10:
            regime = "🟠약하락"
        else:
            regime = "🔴하락"

        periods.append({
            'month': date.strftime('%Y-%m'),
            'start': month_start, 'end': month_end,
            'btc_return': round(ret, 1),
            'regime': regime,
        })
        print(f"    {date.strftime('%Y-%m')} BTC {ret:>+5.1f}% {regime}")

    # 구간별 백테스트
    print(f"\n  🔄 구간별 백테스트...")
    results = []
    for p in tqdm(periods, desc="📅 기간별"):
        s = fast_backtest(coins, best_config, risk_per_trade=risk,
                          start_date=p['start'], end_date=p['end'])
        s['month'] = p['month']
        s['btc_return'] = p['btc_return']
        s['regime'] = p['regime']
        results.append(s)

    print(f"\n  {'월':>7s} {'BTC':>6s} {'구간':>6s} {'거래':>4s} {'승률':>5s} "
          f"{'ROI':>7s} {'PnL':>8s}")
    print(f"  {'-'*55}")
    for r in results:
        if r['total_trades'] == 0:
            print(f"  {r['month']:>7s} {r['btc_return']:>+5.1f}% {r['regime']:>6s}   거래없음")
        else:
            print(f"  {r['month']:>7s} {r['btc_return']:>+5.1f}% {r['regime']:>6s} "
                  f"{r['total_trades']:>4d} {r['win_rate']:>4.1f}% "
                  f"{r['roi_pct']:>+6.2f}% ${r['total_pnl']:>+7.2f}")

    # 구간별 요약
    print(f"\n  📊 시장 구간별 요약:")
    df = pd.DataFrame(results)
    for regime in ['🟢상승', '🟡약상승', '⚪보합', '🟠약하락', '🔴하락']:
        sub = df[df['regime'] == regime]
        if len(sub) == 0:
            continue
        total_trades = sub['total_trades'].sum()
        total_pnl = sub['total_pnl'].sum()
        avg_wr = sub[sub['total_trades'] > 0]['win_rate'].mean() if len(sub[sub['total_trades'] > 0]) > 0 else 0
        print(f"    {regime}: {len(sub)}개월 {total_trades}건 "
              f"승률{avg_wr:.0f}% PnL${total_pnl:+.2f}")

    df.to_csv(f"{RESULT_DIR}/phase4_period_analysis.csv", index=False)
    return df


# ═══════════════════════════════════════════════════════════════
#  Phase 5: 최종 검증
# ═══════════════════════════════════════════════════════════════
def run_phase5(coins=None, best_config=None, risk=0.05):
    """전체 기간 최종 검증 + 상세 리포트"""
    if coins is None:
        coins = ["AVAX", "TRX", "XRP", "DOGE", "ETH", "SOL"]
    if best_config is None:
        try:
            with open(f"{RESULT_DIR}/phase2_best_config.json") as f:
                best_config = json.load(f)
        except:
            best_config = {"TP_FIBO_LEVEL": 2.618, "MIN_SQUEEZE_CANDLES": 15}

    print(f"\n{'═'*70}")
    print(f"  🏆 Phase 5: 최종 검증")
    print(f"  코인: {coins}")
    print(f"  CONFIG: {best_config}")
    print(f"  리스크: {risk*100:.1f}%")
    print(f"{'═'*70}")

    s = fast_backtest(coins, best_config, risk_per_trade=risk, show_progress=True)

    print(f"\n{'═'*70}")
    print(f"  📊 최종 결과")
    print(f"{'═'*70}")
    print(f"  $1,000 → ${1000 + s['total_pnl']:,.2f} (ROI {s['roi_pct']:+.2f}%)")
    print(f"  거래: {s['total_trades']}건 | 승률: {s['win_rate']:.1f}%")
    print(f"  MDD: {s['max_drawdown_pct']:.2f}% | 손익비: {s['profit_factor']:.2f}")
    exit_parts = [f"TP:{s['tp_count']}", f"SL:{s['sl_count']}"]
    if s.get('tstop_count', 0) > 0:
        exit_parts.append(f"T-Stop:{s['tstop_count']}")
    if s.get('dynamic_tp_count', 0) > 0:
        exit_parts.append(f"DynTP:{s['dynamic_tp_count']}")
    exit_parts.append(f"시간초과:{s['timeout_count']}")
    print(f"  청산: {' | '.join(exit_parts)}")

    if s['max_drawdown_pct'] > 0:
        ratio = s['roi_pct'] / s['max_drawdown_pct']
        print(f"  ROI/MDD: {ratio:.2f}x")

    # 코인별
    if s['trades_list']:
        df_t = pd.DataFrame(s['trades_list'])

        # 청산사유별 PnL
        print(f"\n  📊 청산사유별:")
        for reason in df_t['reason'].unique():
            sub = df_t[df_t['reason'] == reason]
            wr = (sub['pnl'] > 0).mean() * 100
            print(f"    {reason:12s}: {len(sub):3d}건 승률{wr:.0f}% ${sub['pnl'].sum():+.2f}")

        print(f"\n  📊 코인별:")
        for sym in df_t['symbol'].unique():
            sub = df_t[df_t['symbol'] == sym]
            wr = (sub['pnl'] > 0).mean() * 100
            pnl = sub['pnl'].sum()
            print(f"    {sym:12s} {len(sub):3d}건 승률{wr:.0f}% ${pnl:+.2f}")

        # 월별
        df_t['month'] = pd.to_datetime(df_t['entry_time']).dt.to_period('M')
        print(f"\n  📊 월별:")
        for m, sub in df_t.groupby('month'):
            wr = (sub['pnl'] > 0).mean() * 100
            pnl = sub['pnl'].sum()
            print(f"    {m}: {len(sub):3d}건 승률{wr:.0f}% ${pnl:+.2f}")

    # 저장
    summary = {
        'config': best_config,
        'risk_per_trade': risk,
        'coins': coins,
        'results': {k: v for k, v in s.items() if k != 'trades_list'},
    }
    with open(f"{RESULT_DIR}/phase5_final.json", 'w') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    trades_csv = f"{RESULT_DIR}/phase5_trades.csv"
    if s['trades_list']:
        pd.DataFrame(s['trades_list']).to_csv(trades_csv, index=False)

    # ── 📊 차트 자동 생성 ──
    chart_dir = f"{RESULT_DIR}/phase5_charts"
    os.makedirs(chart_dir, exist_ok=True)
    print(f"\n  📊 거래별 차트 생성 중...")

    try:
        from trade_chart_viewer import load_candles, plot_trade
        df_trades = pd.DataFrame(s['trades_list'])
        df_trades['entry_time'] = pd.to_datetime(df_trades['entry_time'])
        df_trades['exit_time'] = pd.to_datetime(df_trades['exit_time'])

        saved = 0
        for idx, (_, trade) in enumerate(tqdm(df_trades.iterrows(),
                                               total=len(df_trades),
                                               desc="  📊 차트 저장")):
            candles = load_candles(trade['symbol'], trade['entry_time'],
                                    trade['exit_time'],
                                    before_hours=2, after_hours=8)
            if candles is not None and len(candles) > 5:
                win_str = "WIN" if trade['net_pnl'] > 0 else "LOSS"
                entry_str = trade['entry_time'].strftime('%m%d_%H%M')
                fname = (f"{idx:03d}_{win_str}_{trade['symbol']}_"
                         f"{trade['direction']}_{entry_str}.png")
                fpath = os.path.join(chart_dir, fname)
                plot_trade(trade, candles, show=False, save_path=fpath)
                saved += 1

        print(f"  ✅ {saved}/{len(df_trades)}건 차트 저장 → {chart_dir}/")

        # 수익/손실 분류 폴더
        win_dir = f"{chart_dir}/wins"
        loss_dir = f"{chart_dir}/losses"
        os.makedirs(win_dir, exist_ok=True)
        os.makedirs(loss_dir, exist_ok=True)

        import shutil
        for f in glob.glob(f"{chart_dir}/*.png"):
            fname = os.path.basename(f)
            if fname.startswith(('0', '1', '2', '3', '4', '5', '6', '7', '8', '9')):
                if '_WIN_' in fname:
                    shutil.copy2(f, win_dir)
                elif '_LOSS_' in fname:
                    shutil.copy2(f, loss_dir)

        win_count = len(glob.glob(f"{win_dir}/*.png"))
        loss_count = len(glob.glob(f"{loss_dir}/*.png"))
        print(f"  📂 wins/: {win_count}건 | losses/: {loss_count}건")

    except ImportError:
        print("  ⚠️ trade_chart_viewer.py가 같은 폴더에 없어서 차트 생략")
    except Exception as e:
        print(f"  ⚠️ 차트 생성 오류: {e}")

    print(f"\n  💾 {RESULT_DIR}/phase5_final.json")
    print(f"  💾 {trades_csv}")
    print(f"  📊 {chart_dir}/")
    print(f"{'═'*70}")
    return s


# ═══════════════════════════════════════════════════════════════
#  전체 파이프라인
# ═══════════════════════════════════════════════════════════════
def run_all():
    """Phase 1~5 순차 실행"""
    print(f"\n{'═'*70}")
    print(f"  🚀 수렴 돌파 전략 최적화 파이프라인 시작")
    print(f"  {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*70}")

    # Phase 1
    features_df, groups = run_phase1()

    # Phase 1에서 추천 코인 자동 선택 (거래량 상위 + ATR 적절)
    good_coins = features_df[
        (features_df['atr_pct'] < 0.6) &
        (features_df['avg_vol_usdt'] > 1_000_000)
    ]['code'].tolist()
    if len(good_coins) < 5:
        good_coins = features_df.nlargest(10, 'avg_vol_usdt')['code'].tolist()
    print(f"\n  ✅ Phase 1 추천 코인: {good_coins}")

    # Phase 2
    grid_df, best_config = run_phase2(coins=good_coins)

    # Phase 3
    risk_df, best_risk = run_phase3(coins=good_coins, best_config=best_config)

    # Phase 4
    period_df = run_phase4(coins=good_coins, best_config=best_config, risk=best_risk)

    # Phase 5
    final = run_phase5(coins=good_coins, best_config=best_config, risk=best_risk)

    print(f"\n{'═'*70}")
    print(f"  🏆 최적화 완료!")
    print(f"  코인: {good_coins}")
    print(f"  CONFIG: {best_config}")
    print(f"  리스크: {best_risk*100:.1f}%")
    print(f"  최종 ROI: {final['roi_pct']:+.2f}% | MDD: {final['max_drawdown_pct']:.2f}%")
    print(f"  결과 폴더: {os.path.abspath(RESULT_DIR)}")
    print(f"{'═'*70}")


if __name__ == "__main__":
    run_all()
