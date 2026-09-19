"""미래 데이터 누출 검증 — 이 파일이 모델 백테스트의 신뢰 근거다.

핵심 아이디어: **절단 불변성(truncation invariance)**.

i번째 봉의 지표·시그널이 i번째까지의 데이터만 쓴다면, 데이터를 i에서
잘라 내고 다시 계산해도 i번째 값은 **똑같아야 한다.** 하나라도 달라지면
그 지표는 미래를 봤다는 뜻이다.

과거 candle_idx 오프바이원 사고(CLAUDE.md 교훈 2)가 바로 이 검사로
잡히는 종류의 버그다. 백테스트에서만 좋은 모델을 만드는 가장 흔한 경로이기도
하다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_auto.model.generator import ModelConfig, generate_signals_for_symbol
from stock_auto.model.indicators import WARMUP_BARS, calculate_indicators
from stock_auto.model.regime import stock_regime
from stock_auto.model.strategies import ALL_STRATEGIES, apply_strategies


def _bars(n: int = 420, seed: int = 42) -> pd.DataFrame:
    """재현 가능한 합성 일봉. 거래량에 군집성을 줘 전략이 실제로 발동하게 한다."""
    rng = np.random.default_rng(seed)
    sig = 0.02
    ret = rng.normal(0.0004, sig, n)
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.empty(n)
    open_[0] = close[0] * (1 - ret[0])
    open_[1:] = close[:-1] * np.exp(rng.normal(0, sig * 0.4, n - 1))
    span = np.abs(rng.normal(0, sig * 0.9, n))
    lv = np.zeros(n)
    for i in range(1, n):
        lv[i] = 0.85 * lv[i - 1] + rng.normal(0, 0.35)
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + span),
            "low": np.minimum(open_, close) * (1 - span),
            "close": close,
            "volume": 5e6 * np.exp(lv),
        },
        index=pd.bdate_range("2022-01-03", periods=n),
    )


#: 절단 불변이어야 하는 지표들. 누출이 생기면 여기서 먼저 터진다.
_CHECK_COLS = [
    "MA5", "MA10", "MA20", "MA60", "EMA20", "EMA60",
    "adx", "rsi_14", "atr", "macd_line", "macd_signal", "macdh",
    "vol_ma20", "rvol", "liquidity_score",
    "vai_stage1", "vai_stage2",
    "obv", "obv_ma20", "mfi_14",
    "boll_ub", "bb_pctb", "bb_width",
    "ichimoku_tenkan", "ichimoku_kijun", "senkou_a", "senkou_b", "cloud_top",
    "upper_wick_ratio", "high_close_drop", "gap_pct",
    "high20", "break_high20", "Warning_Count", "Penalty",
]


@pytest.mark.parametrize("cut", [200, 260, 330, 400])
def test_indicators_are_truncation_invariant(cut: int):
    """전체 계열로 계산한 값 == cut에서 자른 계열로 계산한 값 (cut 시점)."""
    df = _bars()
    full = calculate_indicators(df, "US")
    part = calculate_indicators(df.iloc[: cut + 1], "US")

    for col in _CHECK_COLS:
        a, b = full[col].iloc[cut], part[col].iloc[cut]
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (
            f"{col}이 미래 데이터에 의존한다 (cut={cut}): 전체={a} 절단={b}"
        )


@pytest.mark.parametrize("cut", [200, 300, 400])
def test_strategy_flags_are_truncation_invariant(cut: int):
    """전략 발동 여부도 절단에 불변이어야 한다."""
    df = _bars()
    full = apply_strategies(calculate_indicators(df, "US"))
    part = apply_strategies(calculate_indicators(df.iloc[: cut + 1], "US"))

    for name in ALL_STRATEGIES:
        assert bool(full[name].iloc[cut]) == bool(part[name].iloc[cut]), (
            f"{name}이 미래 데이터에 의존한다 (cut={cut})"
        )


@pytest.mark.parametrize("cut", [250, 350])
def test_scores_are_truncation_invariant(cut: int):
    df = _bars()
    full = apply_strategies(calculate_indicators(df, "US"))
    part = apply_strategies(calculate_indicators(df.iloc[: cut + 1], "US"))
    for col in ("Money_Score", "Price_Score", "Effective_Score"):
        assert full[col].iloc[cut] == pytest.approx(part[col].iloc[cut], rel=1e-9)


@pytest.mark.parametrize("cut", [250, 350])
def test_regime_is_truncation_invariant(cut: int):
    df = _bars()
    full = stock_regime(calculate_indicators(df, "US"))
    part = stock_regime(calculate_indicators(df.iloc[: cut + 1], "US"))
    assert int(full.iloc[cut]) == int(part.iloc[cut])


def test_generated_signals_are_a_prefix_under_truncation():
    """데이터를 자르면, 남은 구간의 시그널 목록은 **그대로**여야 한다.

    전체 데이터로 생성한 시그널 중 cut 이전 것들과, cut까지의 데이터로
    생성한 시그널이 일치해야 한다. 다르면 미래를 보고 시그널을 냈다는 뜻이다.
    """
    df = _bars(n=420)
    cut = 380
    cfg = ModelConfig(use_macro_gate=False)

    full = generate_signals_for_symbol("T", df, cfg)
    part = generate_signals_for_symbol("T", df.iloc[: cut + 1], cfg)

    cut_date = np.datetime64(df.index[cut].date(), "D")
    full_upto = [(s.symbol, s.signal_date, s.label) for s in full if s.signal_date <= cut_date]
    part_all = [(s.symbol, s.signal_date, s.label) for s in part]

    assert full_upto == part_all, (
        "절단 시 시그널 목록이 바뀐다 — 미래 데이터 누출"
    )


def test_break_high20_does_not_self_reference():
    """20일 신고가 돌파는 **전일까지의** 고가와 비교해야 한다.

    당일 고가가 포함된 20일 최고가와 종가를 비교하면, 종가가 당일 고가와
    같은 날(상승 마감 후 고가=종가)마다 참이 되는 자기참조가 된다.
    """
    n = 40
    close = np.full(n, 100.0)
    close[-1] = 110.0                      # 마지막 봉만 급등
    df = pd.DataFrame(
        {
            "open": close, "high": close, "low": close - 1.0,
            "close": close, "volume": np.full(n, 1e7),
        },
        index=pd.bdate_range("2022-01-03", periods=n),
    )
    ind = calculate_indicators(df, "US")
    # 평탄 구간에서는 신고가 돌파가 없어야 한다.
    assert not ind["break_high20"].iloc[25:-1].any()
    # 급등한 마지막 봉에서만 참.
    assert bool(ind["break_high20"].iloc[-1])


def test_squeeze_breakout_uses_prior_squeeze_state():
    """스퀴즈 '해제'는 전일까지 스퀴즈였다가 당일 풀린 것이어야 한다."""
    df = _bars(n=300)
    sc = apply_strategies(calculate_indicators(df, "US"))
    fired = sc.index[sc["SqueezeBreakout"]]
    for ts in fired:
        i = sc.index.get_loc(ts)
        assert not bool(sc["squeeze_on"].iloc[i]), "발동일에 스퀴즈가 여전히 켜져 있다"
        prior = sc["squeeze_on"].iloc[max(0, i - 5) : i].sum()
        assert prior >= 3, "직전 5봉 중 3봉 이상 스퀴즈가 아니었다"


def test_entry_is_after_signal_bar():
    """시그널 봉과 진입 봉이 같으면 그 봉의 종가를 미리 아는 셈이다."""
    from stock_auto.exit.config import ExitConfig
    from stock_auto.exit.engine import simulate_exit
    from stock_auto.model.generator import to_price_series

    df = _bars(n=420)
    cfg = ModelConfig(use_macro_gate=False)
    sigs = generate_signals_for_symbol("T", df, cfg)
    assert sigs, "시그널이 0건이면 이 테스트가 의미 없다"

    px = to_price_series({"T": df})["T"]
    for s in sigs[:40]:
        r = simulate_exit(s, px, ExitConfig.legacy())
        if r.entry_date is not None:
            assert r.entry_date > s.signal_date


def test_warmup_bars_are_never_scored():
    """지표가 덜 익은 구간에서 나온 시그널은 신호가 아니라 잡음이다."""
    df = _bars(n=420)
    cfg = ModelConfig(use_macro_gate=False)
    sigs = generate_signals_for_symbol("T", df, cfg)
    earliest_allowed = np.datetime64(df.index[WARMUP_BARS].date(), "D")
    assert all(s.signal_date >= earliest_allowed for s in sigs)


def test_short_history_yields_no_signals():
    """워밍업도 못 채우는 종목은 조용히 0건이 되어야 한다 (예외 아님)."""
    df = _bars(n=50)
    report_sigs = generate_signals_for_symbol("T", df, ModelConfig(use_macro_gate=False))
    assert report_sigs == []
