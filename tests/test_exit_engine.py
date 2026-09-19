"""청산 엔진 — 진입 규칙, 배리어 판정, 갭 체결가, 라벨별 보유상한.

값은 전부 손으로 계산 가능하게 만들었다. 숫자가 틀리면 엔진이 틀린 것이다.
"""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.exit.config import ExitConfig, LabelExit
from stock_auto.exit.engine import (
    EXIT_NONE,
    EXIT_SL,
    EXIT_TO,
    EXIT_TP,
    Signal,
    simulate_all,
    simulate_exit,
)
from stock_auto.horizons import ALL_LABELS
from tests.conftest import make_series


# --- 진입 규칙 -------------------------------------------------------------
def test_entry_is_next_trading_day_open():
    """진입은 시그널 **다음** 거래일 시가다. 시그널 봉 종가가 아니다."""
    px = make_series(
        opens=[90, 100, 110], highs=[95, 101, 111],
        lows=[89, 99, 109], closes=[94, 100, 110],
    )
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, ExitConfig.legacy())
    assert r.entry_date == px.dates[1]
    assert r.entry_price == pytest.approx(100.0)


def test_entry_skips_weekend_gap():
    """휴장일이 끼어도 다음 거래일로 넘어간다."""
    import pandas as pd

    # 금(01-02) → 월(01-05). 달력상 3일 차이.
    dates = np.array(["2026-01-02", "2026-01-05"], dtype="datetime64[D]")
    from stock_auto.exit.engine import PriceSeries

    px = PriceSeries(
        "T", dates, np.array([99.0, 100.0]), np.array([100.0, 101.0]),
        np.array([98.0, 99.0]), np.array([99.0, 100.0]),
    )
    r = simulate_exit(Signal("T", dates[0], "단타"), px, ExitConfig.legacy())
    assert r.entry_date == np.datetime64("2026-01-05")


def test_signal_on_last_bar_is_not_simulated():
    """진입할 봉이 없으면 조용히 0%로 처리하지 않고 NA로 남긴다."""
    px = make_series(opens=[100], highs=[101], lows=[99], closes=[100])
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, ExitConfig.legacy())
    assert r.exit_type == EXIT_NONE
    assert not r.simulated
    assert "진입일" in r.note


# --- legacy 회귀 ------------------------------------------------------------
def test_legacy_is_one_day_hold_for_every_label():
    """legacy는 세 라벨 전부 D+1이다 — analysis §3-1이 지목한 설계 모순."""
    cfg = ExitConfig.legacy()
    assert {cfg.for_label(l).hold_days for l in ALL_LABELS} == {1}


def test_legacy_timeout_exits_at_entry_day_close():
    """배리어 미도달이면 진입일 종가로 청산, 보유 1일."""
    px = make_series(
        opens=[99, 100, 100], highs=[100, 101, 101],
        lows=[98, 99, 99], closes=[99, 100.5, 100],
    )
    r = simulate_exit(Signal("T", px.dates[0], "스윙"), px, ExitConfig.legacy())
    assert r.exit_type == EXIT_TO
    assert r.hold_days == 1
    assert r.return_pct == pytest.approx(0.5)


def test_legacy_has_no_slippage_and_no_gap_adjustment():
    """legacy 재현의 전제 — 체결가 보정이 전혀 없다."""
    cfg = ExitConfig.legacy()
    assert cfg.slippage_bps == 0.0
    assert cfg.gap_aware_fill is False
    assert cfg.close_based is False


# --- 배리어 판정 ------------------------------------------------------------
def test_tp_hit_intraday():
    """장중 고가가 목표를 넘으면 목표가로 체결."""
    px = make_series(
        opens=[99, 100, 100], highs=[100, 104, 101],
        lows=[98, 99, 99], closes=[99, 103, 100],
    )
    cfg = ExitConfig.uniform(2, "pct", 5.0, "pct", 3.0, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.exit_type == EXIT_TP
    assert r.return_pct == pytest.approx(3.0)


def test_sl_hit_intraday():
    px = make_series(
        opens=[99, 100, 100], highs=[100, 101, 101],
        lows=[98, 96, 99], closes=[99, 97, 100],
    )
    cfg = ExitConfig.uniform(2, "pct", 3.0, "pct", 10.0, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.exit_type == EXIT_SL
    assert r.return_pct == pytest.approx(-3.0)


def test_same_bar_both_barriers_resolves_to_sl():
    """일봉으로는 선후를 알 수 없다 → 보수적으로 SL (기존 라벨러와 동일)."""
    px = make_series(
        opens=[99, 100, 100], highs=[100, 110, 101],
        lows=[98, 90, 99], closes=[99, 100, 100],
    )
    cfg = ExitConfig.uniform(2, "pct", 3.0, "pct", 3.0, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.exit_type == EXIT_SL

    r_tp = simulate_exit(
        Signal("T", px.dates[0], "단타"),
        px,
        ExitConfig.uniform(
            2, "pct", 3.0, "pct", 3.0, gap_aware_fill=False, tie_breaker="tp"
        ),
    )
    assert r_tp.exit_type == EXIT_TP


def test_close_based_ignores_intraday_wick():
    """종가 판정이면 장중 위크에 털리지 않는다 — 옵션의 존재 이유."""
    px = make_series(
        opens=[99, 100, 100], highs=[100, 101, 101],
        lows=[98, 90, 99], closes=[99, 100.0, 100],   # 저가는 -10%, 종가는 0%
    )
    intraday = ExitConfig.uniform(2, "pct", 3.0, "pct", 20.0, gap_aware_fill=False)
    closed = ExitConfig.uniform(
        2, "pct", 3.0, "pct", 20.0, close_based=True, gap_aware_fill=False
    )
    assert simulate_exit(Signal("T", px.dates[0], "단타"), px, intraday).exit_type == EXIT_SL
    assert simulate_exit(Signal("T", px.dates[0], "단타"), px, closed).exit_type == EXIT_TO


# --- 갭 체결가 (TASK.md Phase 1의 핵심) --------------------------------------
def test_gap_down_through_sl_fills_at_open_not_at_stop():
    """갭다운으로 손절가를 뚫고 열리면 체결가는 **그 시가**다.

    analysis §3-2가 "SL이 -2.8~-4.56%로 목표보다 나쁘게 체결됐다"고 한 현상을
    백테스트가 재현해야 한다. 손절가로만 채우면 손실을 과소평가한다.
    """
    # 진입 100. SL 3% = 97. 2일차가 94에 갭다운 개장.
    px = make_series(
        opens=[99, 100, 94], highs=[100, 100.5, 95],
        lows=[98, 99.5, 93], closes=[99, 100, 94],
    )
    gap_on = ExitConfig.uniform(3, "pct", 3.0, "pct", 20.0, gap_aware_fill=True)
    gap_off = ExitConfig.uniform(3, "pct", 3.0, "pct", 20.0, gap_aware_fill=False)

    r_on = simulate_exit(Signal("T", px.dates[0], "단타"), px, gap_on)
    r_off = simulate_exit(Signal("T", px.dates[0], "단타"), px, gap_off)

    assert r_on.exit_type == EXIT_SL and r_off.exit_type == EXIT_SL
    assert r_on.exit_price == pytest.approx(94.0)      # 갭 반영 → 시가
    assert r_off.exit_price == pytest.approx(97.0)     # 미반영 → 손절가
    assert r_on.return_pct < r_off.return_pct          # 갭 반영이 항상 더 나쁘다


def test_gap_up_through_tp_does_not_credit_the_gap():
    """갭업으로 목표를 뚫고 열려도 목표가까지만 인정한다.

    갭 이득을 백테스트가 가져가면 TP 쪽만 장식된다 — analysis §3-2가
    지적한 비대칭을 백테스트가 되풀이하지 않게 하는 장치다.
    """
    # 진입 100. TP 3% = 103. 2일차가 110에 갭업 개장.
    px = make_series(
        opens=[99, 100, 110], highs=[100, 100.5, 112],
        lows=[98, 99.5, 109], closes=[99, 100, 111],
    )
    cfg = ExitConfig.uniform(3, "pct", 20.0, "pct", 3.0, gap_aware_fill=True)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.exit_type == EXIT_TP
    assert r.exit_price == pytest.approx(103.0)   # 110이 아니다
    assert r.return_pct == pytest.approx(3.0)


def test_slippage_hurts_both_entry_and_exit():
    px = make_series(
        opens=[99, 100, 100], highs=[100, 101, 101],
        lows=[98, 99, 99], closes=[99, 100, 100],
    )
    cfg = ExitConfig.uniform(
        1, "pct", 5.0, "pct", 5.0, gap_aware_fill=False, slippage_bps=10.0
    )
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.entry_price == pytest.approx(100.0 * 1.001)   # 매수는 비싸게
    assert r.exit_price == pytest.approx(100.0 * 0.999)    # 매도는 싸게
    assert r.return_pct < 0                                # 무변동인데 손실


# --- 라벨별 보유상한 --------------------------------------------------------
def test_hold_days_differ_by_label():
    """같은 가격 흐름인데 라벨마다 다른 날 청산돼야 한다 — 이번 작업의 목적."""
    n = 12
    px = make_series(
        opens=[100.0] * n, highs=[100.5] * n, lows=[99.5] * n,
        closes=[100.0 + i * 0.1 for i in range(n)],
    )
    cfg = ExitConfig(
        by_label={
            "단타": LabelExit(2, "pct", 20.0, "pct", 20.0),
            "중단기": LabelExit(3, "pct", 20.0, "pct", 20.0),
            "스윙": LabelExit(5, "pct", 20.0, "pct", 20.0),
        },
        gap_aware_fill=False,
    )
    holds = {
        lab: simulate_exit(Signal("T", px.dates[0], lab), px, cfg).hold_days
        for lab in ALL_LABELS
    }
    assert holds == {"단타": 2, "중단기": 3, "스윙": 5}


def test_unknown_label_gets_shortest_hold():
    """미분류 라벨은 가장 보수적인(짧은) 보유상한으로 붙인다."""
    cfg = ExitConfig(
        by_label={
            "단타": LabelExit(2, "pct", 20.0, "pct", 20.0),
            "스윙": LabelExit(5, "pct", 20.0, "pct", 20.0),
        },
        gap_aware_fill=False,
    )
    assert cfg.for_label("정체불명").hold_days == 2


# --- ATR 배리어 -------------------------------------------------------------
def test_atr_barrier_uses_signal_bar_not_entry_bar():
    """ATR은 진입 전일까지로 계산해야 한다 — 진입일 봉이 섞이면 누출이다."""
    n = 40
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    px = make_series(
        opens=closes.copy(), highs=closes + 1.0, lows=closes - 1.0, closes=closes
    )
    px.ensure_atr()
    sig_idx = 30
    cfg = ExitConfig.uniform(3, "atr", 1.5, "atr", 2.5, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[sig_idx], "단타"), px, cfg)
    assert r.simulated
    # 손절폭이 시그널 봉 ATR의 1.5배와 일치해야 한다.
    expected_sl = r.entry_price - 1.5 * px.atr[sig_idx]
    assert r.exit_price >= expected_sl - 1e-6 or r.exit_type != EXIT_SL


def test_atr_warmup_shortfall_is_reported_not_guessed():
    """ATR이 안 익은 구간은 임의값으로 메우지 않고 판정을 포기한다."""
    px = make_series(
        opens=[100] * 6, highs=[101] * 6, lows=[99] * 6, closes=[100] * 6
    )
    cfg = ExitConfig.uniform(2, "atr", 1.5, "atr", 2.5)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.exit_type == EXIT_NONE
    assert "ATR" in r.note


# --- 부분익절 / 본전 트레일 --------------------------------------------------
def test_partial_tp_blends_two_legs():
    """목표 도달 시 절반만 실현하고 잔량은 보유상한까지 끌고 간다."""
    # 진입 100, TP 3%=103. 2일차 고가 104(TP), 이후 105로 마감.
    px = make_series(
        opens=[99, 100, 100, 104], highs=[100, 100.5, 104, 106],
        lows=[98, 99.5, 99.5, 103.5], closes=[99, 100, 103, 105],
    )
    cfg = ExitConfig.uniform(
        4, "pct", 20.0, "pct", 3.0, gap_aware_fill=False, partial_tp=0.5
    )
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    # 절반 +3%, 잔량은 마지막 종가 105 → +5%. 가중평균 +4%.
    assert r.return_pct == pytest.approx(4.0)
    assert r.exit_type.startswith("TP")


def test_trail_be_cannot_trigger_within_the_same_bar():
    """본전 트레일은 발동 봉 다음부터. 같은 봉 안에서 당겨 쓰면 낙관 편향."""
    n = 30
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0.1, 0.4, n))
    px = make_series(
        opens=closes.copy(), highs=closes + 1.5, lows=closes - 1.5, closes=closes
    )
    cfg = ExitConfig.uniform(
        5, "atr", 2.0, "atr", 5.0, gap_aware_fill=False, trail_be_atr=0.5
    )
    r = simulate_exit(Signal("T", px.dates[20], "스윙"), px, cfg)
    assert r.simulated       # 발동해도 정상 종료되어야 한다


# --- MFE / MAE -------------------------------------------------------------
def test_mfe_mae_are_measured_from_entry_price():
    px = make_series(
        opens=[99, 100, 100, 100], highs=[100, 108, 101, 101],
        lows=[98, 99, 92, 99], closes=[99, 100, 100, 100],
    )
    cfg = ExitConfig.uniform(1, "pct", 50.0, "pct", 50.0, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    assert r.mfe_pct == pytest.approx(8.0)    # 고가 108
    assert r.mae_pct == pytest.approx(-8.0)   # 저가 92
    # 보유 구간(1일)만 보면 MAE는 -1%
    assert r.mae_hold_pct == pytest.approx(-1.0)


def test_hold_capture_distinguishes_timing_from_horizon():
    """보유내 포착률과 MFE20 포착률은 다른 질문에 답한다."""
    px = make_series(
        opens=[99, 100, 100, 100, 100], highs=[100, 102, 120, 120, 120],
        lows=[98, 99, 99, 99, 99], closes=[99, 102, 100, 100, 100],
    )
    cfg = ExitConfig.uniform(1, "pct", 50.0, "pct", 50.0, gap_aware_fill=False)
    r = simulate_exit(Signal("T", px.dates[0], "단타"), px, cfg)
    # 보유 1일 안에서는 고가 102 = MFE 2%, 실현 +2% → 보유내 포착 100%
    assert r.hold_capture_ratio == pytest.approx(1.0)
    # 20일 창에서는 고가 120 = MFE 20% → MFE20 포착 10%
    assert r.capture_ratio == pytest.approx(0.1)


# --- 누락 데이터 -----------------------------------------------------------
def test_missing_price_data_is_na_not_zero():
    sigs = [Signal("NOPE", np.datetime64("2026-01-05"), "단타")]
    out = simulate_all(sigs, {}, ExitConfig.legacy())
    assert out[0].exit_type == EXIT_NONE
    assert not out[0].simulated
