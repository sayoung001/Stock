"""전략·점수·게이트 단위 검증.

특히 **어떤 전략도 죽은 코드가 아님**을 확인한다. 조건이 서로 모순되어
영원히 발동하지 않는 전략이 있으면, 그 스타일의 라벨이 통째로 사라져
백테스트가 조용히 편향된다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_auto.horizons import DAYTRADE, SWING, SWING_SHORT
from stock_auto.model.indicators import calculate_indicators
from stock_auto.model.regime import macro_gate, stock_regime, synthetic_index
from stock_auto.model.strategies import (
    ALL_STRATEGIES,
    MONEY_CAP_THRESHOLD,
    PRICE_CAP_WHEN_NO_MONEY,
    STRATEGY_STYLE,
    WEIGHTS,
    apply_strategies,
    assign_label,
    final_buy,
)


def _rich_bars(n: int = 1500, seed: int = 7) -> pd.DataFrame:
    """전략이 골고루 발동하도록 국면을 섞은 긴 합성 계열.

    추세 구간·횡보 구간·급락 구간을 번갈아 넣고, 거래량에 군집성과
    스파이크를 준다. 그래야 스퀴즈나 거래량 폭증 같은 조건이 실제로 성립한다.
    """
    rng = np.random.default_rng(seed)
    ret = np.empty(n)
    i = 0
    while i < n:
        seg = min(int(rng.integers(30, 90)), n - i)
        mode = rng.integers(0, 4)
        if mode == 0:      # 추세 상승
            ret[i : i + seg] = rng.normal(0.004, 0.012, seg)
        elif mode == 1:    # 횡보 (스퀴즈 재료)
            ret[i : i + seg] = rng.normal(0.0, 0.004, seg)
        elif mode == 2:    # 급락
            ret[i : i + seg] = rng.normal(-0.004, 0.025, seg)
        else:              # 보통
            ret[i : i + seg] = rng.normal(0.0005, 0.016, seg)
        i += seg

    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.empty(n)
    open_[0] = close[0] * (1 - ret[0])
    # 갭을 자주 내 GapMomentum이 성립하게 한다.
    open_[1:] = close[:-1] * np.exp(rng.normal(0.0, 0.012, n - 1))
    span = np.abs(rng.normal(0, 0.011, n))

    lv = np.zeros(n)
    for k in range(1, n):
        lv[k] = 0.8 * lv[k - 1] + rng.normal(0, 0.45)
    spike = 1.0 + 4.0 * (rng.random(n) < 0.04)      # 4% 확률 거래량 폭증
    volume = 3e7 * np.exp(lv) * spike

    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + span),
            "low": np.minimum(open_, close) * (1 - span),
            "close": close,
            "volume": volume,
        },
        index=pd.bdate_range("2019-01-02", periods=n),
    )


@pytest.fixture(scope="module")
def scored() -> pd.DataFrame:
    return apply_strategies(calculate_indicators(_rich_bars(), "US"))


# --- 죽은 전략 없음 ---------------------------------------------------------
@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_can_fire(name: str, scored: pd.DataFrame):
    """조건 모순으로 영원히 발동 안 하는 전략이 있으면 라벨이 편향된다."""
    assert scored[name].sum() > 0, (
        f"{name}이 1500봉 동안 한 번도 발동하지 않았다 — 조건이 모순일 수 있다"
    )


def test_all_three_styles_are_represented(scored: pd.DataFrame):
    """단타/중단기/스윙 세 스타일이 모두 나와야 라벨 분리가 의미 있다."""
    styles = {
        STRATEGY_STYLE[n] for n in ALL_STRATEGIES if scored[n].sum() > 0
    }
    assert styles == {DAYTRADE, SWING_SHORT, SWING}


def test_strategy_flags_are_boolean_not_nan(scored: pd.DataFrame):
    """워밍업 구간의 NaN이 True로 새면 안 된다."""
    for n in ALL_STRATEGIES:
        assert scored[n].dtype == bool
        assert not scored[n].isna().any()


# --- Money Cap -------------------------------------------------------------
def test_money_cap_limits_price_score_without_money(scored: pd.DataFrame):
    """Money 신호가 없으면 Price 점수가 2.0으로 제한된다 (문서 §2.6)."""
    no_money = scored["Money_Score"] < MONEY_CAP_THRESHOLD
    assert (scored.loc[no_money, "Price_Score"] <= PRICE_CAP_WHEN_NO_MONEY + 1e-9).all()


def test_money_cap_does_not_touch_score_with_money(scored: pd.DataFrame):
    has_money = scored["Money_Score"] >= MONEY_CAP_THRESHOLD
    assert (
        scored.loc[has_money, "Price_Score"]
        == scored.loc[has_money, "Price_Score_raw"]
    ).all()


def test_money_cap_actually_bites_somewhere(scored: pd.DataFrame):
    """캡이 한 번도 작동하지 않으면 그 장치를 검증한 게 아니다."""
    capped = (
        (scored["Money_Score"] < MONEY_CAP_THRESHOLD)
        & (scored["Price_Score_raw"] > PRICE_CAP_WHEN_NO_MONEY)
    )
    assert capped.sum() > 0


def test_effective_score_formula(scored: pd.DataFrame):
    expected = (
        scored["Technical_Score"] * scored["liquidity_score"] - scored["Penalty"]
    )
    pd.testing.assert_series_equal(
        scored["Effective_Score"], expected, check_names=False
    )


# --- 최종 매수 판정 ---------------------------------------------------------
def test_final_buy_requires_money_when_score_is_weak(scored: pd.DataFrame):
    """Eff가 4.0 미만이면 Money≥1.5 + 유동성≥0.7이 있어야 통과한다."""
    regime = pd.Series(4, index=scored.index)
    buy = final_buy(scored, regime=regime)
    weak = buy & (scored["Effective_Score"] < 4.0)
    assert (scored.loc[weak, "Money_Score"] >= 1.5).all()
    assert (scored.loc[weak, "liquidity_score"] >= 0.7).all()


def test_final_buy_blocks_on_warnings_and_penalty(scored: pd.DataFrame):
    regime = pd.Series(4, index=scored.index)
    buy = final_buy(scored, regime=regime)
    assert (scored.loc[buy, "Warning_Count"] < 2).all()
    assert (scored.loc[buy, "Penalty"] < 2.0).all()


def test_final_buy_blocks_low_regime(scored: pd.DataFrame):
    """레짐 0(하락위험)은 전부 차단."""
    assert not final_buy(scored, regime=pd.Series(0, index=scored.index)).any()


def test_macro_gate_blocks_everything_when_index_regime_is_zero(scored: pd.DataFrame):
    regime = pd.Series(4, index=scored.index)
    closed = pd.Series(False, index=scored.index)
    assert not final_buy(scored, regime=regime, macro_ok=closed).any()


def test_final_buy_fires_somewhere(scored: pd.DataFrame):
    regime = stock_regime(scored)
    assert final_buy(scored, regime=regime).sum() > 0


# --- 레짐 -----------------------------------------------------------------
def test_regime_is_within_range(scored: pd.DataFrame):
    r = stock_regime(scored)
    assert r.min() >= 0 and r.max() <= 4


def test_regime_warmup_is_blocked_not_guessed(scored: pd.DataFrame):
    """MA60이 없는 구간은 0(차단). 모르면 사지 않는다."""
    r = stock_regime(scored)
    assert (r[scored["MA60"].isna()] == 0).all()


def test_perfect_bull_alignment_is_regime_4():
    n = 120
    close = np.linspace(100, 200, n)          # 단조 상승 → 완전 정배열
    df = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1e7)},
        index=pd.bdate_range("2022-01-03", periods=n),
    )
    assert int(stock_regime(calculate_indicators(df, "US")).iloc[-1]) == 4


# --- 라벨 배정 -------------------------------------------------------------
def test_label_follows_highest_weighted_style():
    row = pd.Series({n: False for n in ALL_STRATEGIES})
    row["TrendContinuation"] = True          # 스윙, 1.5
    row["GapMomentum"] = True                # 단타, 1.0
    assert assign_label(row) == SWING


def test_label_prefers_longer_horizon_on_tie():
    """동점이면 긴 지평. 짧게 끝내는 건 언제든 가능하지만 반대는 불가능하다."""
    row = pd.Series({n: False for n in ALL_STRATEGIES})
    row["MeanReversion"] = True              # 단타, 1.5
    row["PullbackBuy"] = True                # 중단기, 1.5
    assert assign_label(row) == SWING_SHORT


def test_label_defaults_to_mid_when_nothing_fired():
    row = pd.Series({n: False for n in ALL_STRATEGIES})
    assert assign_label(row) == SWING_SHORT


def test_money_strategies_sum_to_expected_weights():
    """가중치가 문서 §2.4·§2.5 표와 일치하는지."""
    assert WEIGHTS["VolBreakout"] == 2.0
    assert WEIGHTS["VolPumpSustained"] == 2.0
    assert WEIGHTS["MoneyFlowSurge"] == 1.5
    assert WEIGHTS["IchimokuTrend"] == 1.0
    assert WEIGHTS["TrendContinuation"] == 1.5


# --- 유동성 통화 정규화 ------------------------------------------------------
def test_liquidity_score_is_market_aware():
    """KR 종목에 US 중심값을 쓰면 전 종목이 포화된다 (US_KR 문서 §3.1 버그)."""
    n = 80
    # 원화 거래대금 규모 (수백억)
    close = np.full(n, 70_000.0)
    volume = np.full(n, 500_000.0)          # 350억 원
    df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": volume},
        index=pd.bdate_range("2022-01-03", periods=n),
    )
    us = calculate_indicators(df, "US")["liquidity_score"].iloc[-1]
    kr = calculate_indicators(df, "KR")["liquidity_score"].iloc[-1]
    assert us > 1.35, "US 기준이면 포화되어야 한다 (버그 재현 확인)"
    assert kr < us, "KR 기준으로는 포화가 풀려야 한다"


# --- 합성지수 -------------------------------------------------------------
def test_synthetic_index_has_ohlc_and_matches_calendar():
    frames = {f"S{i}": _rich_bars(n=200, seed=i) for i in range(3)}
    idx = synthetic_index(frames)
    assert set(["open", "high", "low", "close", "volume"]) <= set(idx.columns)
    assert len(idx) > 0


def test_macro_gate_opens_when_no_index_data():
    """지수를 못 구했다고 게이트를 닫으면 시그널 0건을 '결과'로 위장하게 된다."""
    cal = pd.bdate_range("2022-01-03", periods=50)
    ok, vals = macro_gate({}, cal)
    assert ok.all()
    assert vals.isna().all()
