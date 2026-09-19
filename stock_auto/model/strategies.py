"""Money 3 + Price 7 전략, 실효점수, 최종 매수 판정.

명세 출처는 `현재_적용_로직_전수.md` §2.4~2.7이다. 조건·가중치·스타일을
그 표 그대로 옮겼다. 문서에 없는 세부(예: "EMA20 터치 후 회복"의 허용폭)는
가정을 세웠고 `docs/MODEL_REPRODUCTION.md`에 적었다.

각 전략의 **스타일**(단타/중단기/스윙)이 백테스트에서 결정적으로 중요하다.
프로덕션에서는 보유기간 라벨을 LLM(PortfolioAgent)이 붙이는데, 과거를
소급해서 LLM을 돌릴 수는 없다. 대신 **발동한 전략의 스타일로 라벨을
정한다** — 문서가 전략마다 스타일을 명시해 두었으므로 근거 있는 규칙이고,
LLM보다 재현 가능하다는 장점도 있다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stock_auto.horizons import DAYTRADE, SWING, SWING_SHORT

#: Money 전략 — "자금이 실제로 움직였는가". (이름, 가중치)
MONEY_STRATEGIES: tuple[tuple[str, float], ...] = (
    ("VolBreakout", 2.0),
    ("VolPumpSustained", 2.0),
    ("MoneyFlowSurge", 1.5),
)

#: Price 전략 — "가격 구조가 유리한가".
PRICE_STRATEGIES: tuple[tuple[str, float], ...] = (
    ("MeanReversion", 1.5),
    ("GapMomentum", 1.0),
    ("PullbackBuy", 1.5),
    ("MACDReversal", 1.0),
    ("SqueezeBreakout", 1.5),
    ("IchimokuTrend", 1.0),
    ("TrendContinuation", 1.5),
)

#: 전략 → 보유기간 스타일. 문서 §2.4·§2.5 "스타일" 열 그대로.
#: GapMomentum은 문서가 "당일~1일"이라 단타로 묶었다.
STRATEGY_STYLE: dict[str, str] = {
    "VolBreakout": DAYTRADE,
    "VolPumpSustained": DAYTRADE,
    "MoneyFlowSurge": SWING_SHORT,
    "MeanReversion": DAYTRADE,
    "GapMomentum": DAYTRADE,
    "PullbackBuy": SWING_SHORT,
    "MACDReversal": SWING_SHORT,
    "SqueezeBreakout": SWING_SHORT,
    "IchimokuTrend": SWING,
    "TrendContinuation": SWING,
}

ALL_STRATEGIES: tuple[str, ...] = tuple(
    n for n, _ in MONEY_STRATEGIES + PRICE_STRATEGIES
)
WEIGHTS: dict[str, float] = dict(MONEY_STRATEGIES + PRICE_STRATEGIES)

#: Money Cap — 자금 신호가 없으면 가격 점수를 이 값으로 제한한다.
#: "거래량 없이 기술지표만으로 고득점"을 구조적으로 차단하는 장치(문서 §2.6).
MONEY_CAP_THRESHOLD = 0.5
PRICE_CAP_WHEN_NO_MONEY = 2.0


def apply_strategies(ind: pd.DataFrame) -> pd.DataFrame:
    """지표 DataFrame에 전략 발동 여부 + 점수 컬럼을 붙인다."""
    d = ind
    out = ind.copy()
    c, o = d["close"], d["open"]

    # --- Money 3 ---------------------------------------------------------
    out["VolBreakout"] = (
        (d["rvol"] > 2.0)
        & d["is_bull"]
        & d["break_high20"]
        & (d["adx"] > 20.0)
        & ~d["vai_spike_only"]
    )
    out["VolPumpSustained"] = (
        d["vai_sustained"]
        & d["is_bull"]
        & (c > c.shift(1))
        & (d["liquidity_score"] > 0.7)
    )
    out["MoneyFlowSurge"] = (
        (d["obv"] > d["obv_ma20"])
        & (d["mfi_14"] > 60.0)
        & (d["rvol"] > 1.5)
        & (c > d["EMA20"])
    )

    # --- Price 7 ---------------------------------------------------------
    out["MeanReversion"] = (
        (d["rsi_14"] < 30.0)
        & (d["bb_pctb"] < 0.1)
        & (d["mfi_14"] < 35.0)
        & d["is_bull"]
    )
    out["GapMomentum"] = (
        (d["gap_pct"] > 1.5) & (c >= o) & (d["rvol"] > 1.0)
    )
    # "EMA20 터치 후 회복" — 당일 저가가 EMA20을 건드렸거나 1% 이내로
    # 근접했고, 종가는 EMA20 위에서 마감. 허용폭 1%는 가정(A5).
    touched = (d["low"] <= d["EMA20"] * 1.01)
    out["PullbackBuy"] = (
        (d["EMA20"] > d["EMA60"])
        & touched
        & (c > d["EMA20"])
        & (d["rsi_14"] > 40.0)
        & (d["rsi_14"] < 60.0)
    )
    out["MACDReversal"] = (
        (d["macdh"] > 0.0)
        & (d["macdh"].shift(1) <= 0.0)
        & (c > d["EMA60"])
        & (d["obv"] > d["obv_ma20"])
    )
    # "5일 중 3일 스퀴즈 → 해제" — 전일까지의 5봉 중 3봉 이상 스퀴즈였고
    # 당일은 해제. shift(1)이 없으면 당일 스퀴즈 상태를 자기참조한다.
    squeeze_recent = d["squeeze_on"].rolling(5, min_periods=5).sum().shift(1)
    out["SqueezeBreakout"] = (
        (squeeze_recent >= 3)
        & ~d["squeeze_on"]
        & (c > d["boll_ub"])
        & (d["rvol"] > 1.5)
    )
    out["IchimokuTrend"] = (
        (c > d["ichimoku_tenkan"])
        & (d["ichimoku_tenkan"] > d["ichimoku_kijun"])
        & (c > d["cloud_top"])
    )
    out["TrendContinuation"] = (
        (d["MA5"] > d["MA10"])
        & (d["MA10"] > d["MA20"])
        & (d["MA20"] > d["MA60"])
        & (d["adx"] > 25.0)
        & (d["rsi_14"] > 50.0)
        & (d["rsi_14"] < 70.0)
        & (d["obv"] > d["obv_ma20"])
    )

    # NaN(워밍업 구간)이 True로 새지 않도록 명시적으로 False로 채운다.
    for name in ALL_STRATEGIES:
        out[name] = out[name].fillna(False).astype(bool)

    # --- 점수 합성 (문서 §2.6) ----------------------------------------------
    out["Money_Score"] = sum(
        out[n].astype(float) * w for n, w in MONEY_STRATEGIES
    )
    raw_price = sum(out[n].astype(float) * w for n, w in PRICE_STRATEGIES)
    out["Price_Score_raw"] = raw_price

    # ★ Money Cap
    no_money = out["Money_Score"] < MONEY_CAP_THRESHOLD
    out["Price_Score"] = raw_price.where(
        ~no_money, np.minimum(raw_price, PRICE_CAP_WHEN_NO_MONEY)
    )
    out["Technical_Score"] = out["Money_Score"] + out["Price_Score"]
    out["Effective_Score"] = (
        out["Technical_Score"] * out["liquidity_score"] - out["Penalty"]
    )
    return out


def final_buy(
    scored: pd.DataFrame,
    *,
    regime: pd.Series,
    macro_ok: pd.Series | None = None,
    eff_strong: float = 4.0,
    eff_weak: float = 2.5,
    money_min: float = 1.5,
    liq_min: float = 0.7,
    min_regime: int = 1,
) -> pd.Series:
    """최종 매수 판정 (문서 §2.7).

    ```
    (Eff ≥ 4.0  OR  (Eff ≥ 2.5 AND Money ≥ 1.5 AND liquidity ≥ 0.7))
    AND Warning_Count < 2
    AND Penalty < 2.0
    AND 종목레짐 ≥ 1
    AND 매크로 게이트 통과
    ```

    섹터 게이트는 섹터 ETF 일봉이 필요해 여기서는 선택이다
    (`--sector-etf` 미지정 시 비활성). `docs/MODEL_REPRODUCTION.md` A7.
    """
    eff = scored["Effective_Score"]
    score_ok = (eff >= eff_strong) | (
        (eff >= eff_weak)
        & (scored["Money_Score"] >= money_min)
        & (scored["liquidity_score"] >= liq_min)
    )
    gate = (
        score_ok
        & (scored["Warning_Count"] < 2)
        & (scored["Penalty"] < 2.0)
        & (regime >= min_regime)
    )
    if macro_ok is not None:
        gate = gate & macro_ok.reindex(scored.index).fillna(False)
    return gate.fillna(False)


def assign_label(row: pd.Series) -> str:
    """발동한 전략들로 보유기간 라벨을 정한다.

    스타일별 가중치 합이 가장 큰 쪽을 택하고, 동점이면 **더 긴 지평**을
    쓴다. 청산은 언제든 짧게 끝낼 수 있지만 상한을 넘겨 버는 건 불가능하므로,
    애매할 때 긴 쪽이 덜 파괴적이다.
    """
    totals = {DAYTRADE: 0.0, SWING_SHORT: 0.0, SWING: 0.0}
    for name in ALL_STRATEGIES:
        if bool(row.get(name, False)):
            totals[STRATEGY_STYLE[name]] += WEIGHTS[name]
    if not any(totals.values()):
        return SWING_SHORT          # 발동 전략이 없는 건은 중간 지평으로
    best = max(totals.values())
    # 동점 시 긴 지평 우선 → 역순으로 훑는다.
    for style in (SWING, SWING_SHORT, DAYTRADE):
        if totals[style] == best:
            return style
    return SWING_SHORT


def active_strategies(row: pd.Series) -> list[str]:
    """그 봉에서 발동한 전략 이름 목록. 리포트·디버깅용."""
    return [n for n in ALL_STRATEGIES if bool(row.get(n, False))]
