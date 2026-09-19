"""
gap_analysis_v2.py — A/B/C 3설정 × GAP별 분석 + 코인 추천
%run gap_analysis_v2.py
"""
import pandas as pd, numpy as np, os, sys, time, warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, '.')
from convergence_strategy import ConvergenceBreakout, CONVERGENCE_CONFIG

DATA_DIR = "./Raw_Data/CRYPTO_BINANCE_15M"
SIGNAL_DIR = "./Results/cloud_scan_v2/all"
OUTPUT_DIR = "./Results/gap_analysis_v2"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LEVERAGE = 20; TO_CANDLES = 768
CONF_MIN = 70; ADX_MIN = 25

CFGS = {
    'A_CURRENT': {
        'desc': 'CasTrail EE2-4h (현재봇)',
        'sl':1.5,'lk':2.0,'tr':50,'tp':0,'ee_s':8,'ee_e':16,'ee_t':-10,
        'coins': ['ETH','SOL','APT','SAND','MANA','DOGE','XRP','UNI','COMP','BCH','NEAR'],
        'btc_sqz': False,
    },
    'B_PROPOSED': {
        'desc': 'Cascade EE4-6h (추천)',
        'sl':1.5,'lk':2.0,'tr':0,'tp':8.0,'ee_s':16,'ee_e':24,'ee_t':-20,
        'coins': 'ALL',
        'btc_sqz': False,
    },
    'C_BTC_SQZ': {
        'desc': 'Cascade EE4-6h + BTC squeeze',
        'sl':1.5,'lk':2.0,'tr':0,'tp':8.0,'ee_s':16,'ee_e':24,'ee_t':-20,
        'coins': 'ALL',
        'btc_sqz': True,
    },
}

GAPS = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
CURRENT_11 = ['ETH','SOL','APT','SAND','MANA','DOGE','XRP','UNI','COMP','BCH','NEAR']

_cc = {}
def load_candles(code):
    if code in _cc: return _cc[code]
    p = os.path.join(DATA_DIR, f"{code}_USDT.csv")
    if not os.path.exists(p): return None
    df = pd.read_csv(p); df['Date'] = pd.to_datetime(df['Date'])
    _cc[code] = df; return df

def precompute_btc_squeeze():
    btc = load_candles('BTC')
    if btc is None: return None, None
    c = btc['Close'].values.astype(float)
    ema_s = pd.Series(c).ewm(span=12).mean().values
    ema_l = pd.Series(c).ewm(span=26).mean().values
    gap = np.abs(ema_s - ema_l) / (c + 1e-9) * 100
    return btc['Date'].values, gap

def is_btc_squeeze(btc_dates, btc_gap, t, threshold=0.5):
    if btc_dates is None: return False
    idx = np.searchsorted(btc_dates, np.datetime64(pd.Timestamp(t)), side='right') - 1
    if idx < 7 or idx >= len(btc_gap): return False
    return np.sum(btc_gap[idx-7:idx+1] < threshold) >= 6

def simulate(idx, d, ep, sl, tp, atr, code, cfg):
    df = load_candles(code)
    if df is None: return None
    end = min(idx+TO_CANDLES+1, len(df))
    if idx+10>=len(df): return None
    h=df['High'].values[idx+1:end]; lo=df['Low'].values[idx+1:end]
    cl=df['Close'].values[idx+1:end]; n=len(h)
    if n<4: return None
    sq=abs(tp-ep)/2.618 if abs(tp-ep)>0 else atr*2
    sl_d=abs(ep-sl)*cfg['sl']
    sl_p=ep-sl_d if d=='long' else ep+sl_d
    tp_p=(ep+sq*cfg['tp'] if d=='long' else ep-sq*cfg['tp']) if cfg['tp']>0 else 0
    lk_p=(ep+sq*cfg['lk'] if d=='long' else ep-sq*cfg['lk']) if cfg['lk']>0 else 0
    locked=False;mf=0;reason='TO';ec=n;xp=cl[-1] if n>0 else ep
    for j in range(n):
        if d=='long':
            rs=(cl[j]-ep)/ep*100;rh=(h[j]-ep)/ep*100
            if cfg['lk']>0 and not locked and h[j]>=lk_p: locked=True;sl_p=ep
            if lo[j]<=sl_p: reason='SL_BE' if locked else 'SL';ec=j;xp=sl_p;break
            if tp_p>0 and h[j]>=tp_p: reason='TP';ec=j;xp=tp_p;break
            mf=max(mf,rh)
            if cfg['tr']>0 and locked and mf>0.5 and rs<=mf*(1-cfg['tr']/100):
                reason='TRAIL';ec=j;xp=cl[j];break
            if cfg['ee_s']>0 and cfg['ee_s']<=j<=cfg['ee_e'] and rs*LEVERAGE<=cfg['ee_t']:
                reason='EE';ec=j;xp=cl[j];break
        else:
            rs=(ep-cl[j])/ep*100;rh=(ep-lo[j])/ep*100
            if cfg['lk']>0 and not locked and lo[j]<=lk_p: locked=True;sl_p=ep
            if h[j]>=sl_p: reason='SL_BE' if locked else 'SL';ec=j;xp=sl_p;break
            if tp_p>0 and lo[j]<=tp_p: reason='TP';ec=j;xp=tp_p;break
            mf=max(mf,rh)
            if cfg['tr']>0 and locked and mf>0.5 and rs<=mf*(1-cfg['tr']/100):
                reason='TRAIL';ec=j;xp=cl[j];break
            if cfg['ee_s']>0 and cfg['ee_s']<=j<=cfg['ee_e'] and rs*LEVERAGE<=cfg['ee_t']:
                reason='EE';ec=j;xp=cl[j];break
    froe=(xp-ep)/ep*100 if d=='long' else (ep-xp)/ep*100
    return {'roe':round(froe,4),'reason':reason,'hold_h':round((ec+1)*0.25,1)}

def calc_pf(roes):
    if len(roes)<3: return 0,0,0
    w=[r for r in roes if r>0];l=[r for r in roes if r<=0]
    return round(sum(w)/(abs(sum(l))+0.001),3),round(len(w)/len(roes)*100,1),round(sum(roes),2)

# ── 로드 ──
t0 = time.time()
vsigs = pd.read_csv(os.path.join("./Results/full_experiment_v2", 'verified_signals.csv'))
vsigs['entry_time'] = pd.to_datetime(vsigs['entry_time'])
print(f"시그널: {len(vsigs):,}건")

btc_dates, btc_gap = precompute_btc_squeeze()
print(f"BTC squeeze 계산 완료")

oos_start = pd.Timestamp('2024-03-01')
oos = vsigs[vsigs['entry_time'] >= oos_start]
print(f"OOS (2024.03~): {len(oos):,}건")
months_oos = (oos['entry_time'].max() - oos_start).days / 30

# ═══════════════════════════════════════════════════
#  1. GAP별 개별 성과
# ═══════════════════════════════════════════════════
print(f"\n{'='*110}")
print(f"  1. GAP별 개별 성과 (OOS)")
print(f"{'='*110}")

all_gap_results = []

for cfg_name, cfg in CFGS.items():
    coin_filter = [f"{c}_USDT" for c in cfg['coins']] if cfg['coins'] != 'ALL' else None

    for gap in GAPS:
        filt = oos[(oos['scan_gap']==gap) & (oos['confidence']>=CONF_MIN) & (oos['adx_value']>=ADX_MIN)]
        if coin_filter: filt = filt[filt['symbol'].isin(coin_filter)]
        if len(filt) < 3: continue

        roes = []
        for _, sig in filt.iterrows():
            if cfg['btc_sqz'] and not is_btc_squeeze(btc_dates, btc_gap, sig['entry_time']):
                continue
            code = sig['symbol'].replace('_USDT','')
            r = simulate(int(sig['candle_idx']), sig['direction'],
                         sig['entry_price'], sig['stop_loss'], sig['take_profit'],
                         sig.get('atr', sig['entry_price']*0.015), code, cfg)
            if r: roes.append(r['roe'])
        if len(roes)<3: continue
        pf,wr,total=calc_pf(roes)
        all_gap_results.append({
            'cfg':cfg_name,'gap':gap,'n':len(roes),'pf':pf,'wr':wr,
            'total_roe':total,'coins':filt['symbol'].nunique(),
            'monthly':round(len(roes)/max(months_oos,1),1),
        })

gdf = pd.DataFrame(all_gap_results)
gdf.to_csv(os.path.join(OUTPUT_DIR, 'gap_by_config.csv'), index=False)

for cfg_name in CFGS:
    cd = gdf[gdf['cfg']==cfg_name]
    if len(cd)==0: continue
    print(f"\n  ■ {cfg_name} ({CFGS[cfg_name]['desc']})")
    print(f"  {'GAP':>4} {'N':>5} {'PF':>5} {'WR':>5} {'ROE':>8} {'코인':>4} {'월':>5}")
    for _, r in cd.iterrows():
        flag='★' if r['pf']>2.0 else ('✓' if r['pf']>1.0 else '✗')
        print(f"  {r['gap']:>4.1f} {int(r['n']):>5} {r['pf']:>4.2f} {r['wr']:>4.0f}% "
              f"{r['total_roe']:>+7.1f}% {int(r['coins']):>4} {r['monthly']:>4.1f} {flag}")

# ═══════════════════════════════════════════════════
#  2. 누적 GAP (≤X)
# ═══════════════════════════════════════════════════
print(f"\n{'='*110}")
print(f"  2. 누적 GAP (≤X) 성과")
print(f"{'='*110}")

cum_results = []
for cfg_name, cfg in CFGS.items():
    coin_filter = [f"{c}_USDT" for c in cfg['coins']] if cfg['coins'] != 'ALL' else None

    for max_gap in [0.3, 0.5, 0.7, 0.9, 1.0, 1.2, 1.5, 2.0]:
        filt = oos[(oos['scan_gap']<=max_gap) & (oos['confidence']>=CONF_MIN) & (oos['adx_value']>=ADX_MIN)]
        if coin_filter: filt = filt[filt['symbol'].isin(coin_filter)]
        if len(filt)<3: continue

        roes = []
        for _, sig in filt.iterrows():
            if cfg['btc_sqz'] and not is_btc_squeeze(btc_dates, btc_gap, sig['entry_time']):
                continue
            code = sig['symbol'].replace('_USDT','')
            r = simulate(int(sig['candle_idx']), sig['direction'],
                         sig['entry_price'], sig['stop_loss'], sig['take_profit'],
                         sig.get('atr', sig['entry_price']*0.015), code, cfg)
            if r: roes.append(r['roe'])
        if len(roes)<3: continue
        pf,wr,total=calc_pf(roes)
        cum_results.append({
            'cfg':cfg_name,'max_gap':max_gap,'n':len(roes),'pf':pf,'wr':wr,
            'total_roe':total,'coins':filt['symbol'].nunique(),
            'monthly':round(len(roes)/max(months_oos,1),1),
        })

cdf = pd.DataFrame(cum_results)
cdf.to_csv(os.path.join(OUTPUT_DIR, 'gap_cumulative.csv'), index=False)

for cfg_name in CFGS:
    cd = cdf[cdf['cfg']==cfg_name]
    if len(cd)==0: continue
    print(f"\n  ■ {cfg_name} ({CFGS[cfg_name]['desc']})")
    print(f"  {'GAP≤':>5} {'N':>5} {'PF':>5} {'WR':>5} {'ROE':>8} {'코인':>4} {'월':>5}")
    for _, r in cd.iterrows():
        flag='★' if r['pf']>2.0 else ('✓' if r['pf']>1.0 else '✗')
        print(f"  {r['max_gap']:>5.1f} {int(r['n']):>5} {r['pf']:>4.2f} {r['wr']:>4.0f}% "
              f"{r['total_roe']:>+7.1f}% {int(r['coins']):>4} {r['monthly']:>4.1f} {flag}")

# ═══════════════════════════════════════════════════
#  3. GAP별 코인 성과 + 추천
# ═══════════════════════════════════════════════════
print(f"\n{'='*110}")
print(f"  3. GAP별 코인 성과 + 추천 (OOS)")
print(f"{'='*110}")

all_coin_gap = []

for cfg_name, cfg in CFGS.items():
    coin_filter = [f"{c}_USDT" for c in cfg['coins']] if cfg['coins'] != 'ALL' else None

    for gap in [0.3, 0.5, 0.7, 1.0, 1.5, 2.0]:
        filt = oos[(oos['scan_gap']==gap) & (oos['confidence']>=CONF_MIN) & (oos['adx_value']>=ADX_MIN)]
        if coin_filter: filt = filt[filt['symbol'].isin(coin_filter)]
        if len(filt)<5: continue

        coin_roes = {}
        for _, sig in filt.iterrows():
            if cfg['btc_sqz'] and not is_btc_squeeze(btc_dates, btc_gap, sig['entry_time']):
                continue
            code = sig['symbol'].replace('_USDT','')
            r = simulate(int(sig['candle_idx']), sig['direction'],
                         sig['entry_price'], sig['stop_loss'], sig['take_profit'],
                         sig.get('atr', sig['entry_price']*0.015), code, cfg)
            if r is None: continue
            sym = sig['symbol']
            if sym not in coin_roes: coin_roes[sym] = []
            coin_roes[sym].append(r['roe'])

        print(f"\n  ■ {cfg_name} GAP {gap:.1f}")
        print(f"    {'':>1} {'코인':>15} {'N':>4} {'PF':>5} {'WR':>5} {'ROE':>8} {'추천':>4}")

        for sym in sorted(coin_roes.keys(), key=lambda s:-calc_pf(coin_roes[s])[0]):
            roes = coin_roes[sym]
            if len(roes)<3: continue
            pf,wr,total = calc_pf(roes)
            in_cur = '●' if sym.replace('_USDT','') in CURRENT_11 else ' '
            flag='★' if pf>1.5 else ('✓' if pf>1.0 else '✗')

            # 추천 로직
            is_current = sym.replace('_USDT','') in CURRENT_11
            if pf > 1.3 and not is_current: rec = '추가'
            elif pf < 0.8 and is_current: rec = '제외'
            else: rec = ''

            all_coin_gap.append({
                'cfg':cfg_name,'gap':gap,'sym':sym,'n':len(roes),
                'pf':pf,'wr':wr,'total_roe':total,'in_current':is_current,'rec':rec,
            })
            print(f"    {in_cur} {sym:>15} {len(roes):>4} {pf:>4.2f} {wr:>4.0f}% "
                  f"{total:>+7.1f}% {flag} {rec}")

# 저장
acdf = pd.DataFrame(all_coin_gap)
acdf.to_csv(os.path.join(OUTPUT_DIR, 'coin_by_gap_config.csv'), index=False)

# ═══════════════════════════════════════════════════
#  4. 종합 코인 추천 (A/B/C × GAP≤0.7 통합)
# ═══════════════════════════════════════════════════
print(f"\n{'='*110}")
print(f"  4. 종합 코인 추천 (GAP≤0.7 OOS 통합)")
print(f"{'='*110}")

for cfg_name, cfg in CFGS.items():
    coin_filter = [f"{c}_USDT" for c in cfg['coins']] if cfg['coins'] != 'ALL' else None
    filt = oos[(oos['scan_gap']<=0.7) & (oos['confidence']>=CONF_MIN) & (oos['adx_value']>=ADX_MIN)]
    if coin_filter: filt = filt[filt['symbol'].isin(coin_filter)]

    coin_roes = {}
    for _, sig in filt.iterrows():
        if cfg['btc_sqz'] and not is_btc_squeeze(btc_dates, btc_gap, sig['entry_time']):
            continue
        code = sig['symbol'].replace('_USDT','')
        r = simulate(int(sig['candle_idx']), sig['direction'],
                     sig['entry_price'], sig['stop_loss'], sig['take_profit'],
                     sig.get('atr', sig['entry_price']*0.015), code, cfg)
        if r is None: continue
        sym = sig['symbol']
        if sym not in coin_roes: coin_roes[sym] = []
        coin_roes[sym].append(r['roe'])

    print(f"\n  ■ {cfg_name} ({cfg['desc']})")
    print(f"    {'':>1} {'코인':>15} {'N':>4} {'PF':>5} {'WR':>5} {'ROE':>8} {'월':>5} {'추천':>4}")

    for sym in sorted(coin_roes.keys(), key=lambda s:-calc_pf(coin_roes[s])[0]):
        roes = coin_roes[sym]
        if len(roes)<3: continue
        pf,wr,total = calc_pf(roes)
        monthly = len(roes)/max(months_oos,1)
        in_cur = '●' if sym.replace('_USDT','') in CURRENT_11 else ' '
        flag='★' if pf>1.5 else ('✓' if pf>1.0 else '✗')
        is_current = sym.replace('_USDT','') in CURRENT_11
        if pf>1.3 and not is_current: rec='추가'
        elif pf<0.8 and is_current: rec='제외'
        else: rec=''
        print(f"    {in_cur} {sym:>15} {len(roes):>4} {pf:>4.2f} {wr:>4.0f}% "
              f"{total:>+7.1f}% {monthly:>4.1f} {flag} {rec}")

    # 최종 추천 리스트
    keep = [sym for sym,roes in coin_roes.items()
            if len(roes)>=3 and calc_pf(roes)[0]>=1.0]
    add = [sym for sym in keep if sym.replace('_USDT','') not in CURRENT_11]
    remove = [sym for sym in [f"{c}_USDT" for c in CURRENT_11]
              if sym in coin_roes and len(coin_roes[sym])>=3 and calc_pf(coin_roes[sym])[0]<0.8]

    print(f"\n    추천 유지: {[s for s in keep if s.replace('_USDT','') in CURRENT_11]}")
    print(f"    추천 추가: {add}")
    print(f"    추천 제외: {remove}")
    print(f"    총 코인: {len(keep)}개")

print(f"\n  완료: {time.time()-t0:.1f}초")
