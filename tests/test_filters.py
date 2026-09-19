"""실행 필터 — 확신도, SL 후 쿨다운, 섹터 한도.

필터는 시그널을 **지우지 않고 플래그만** 붙인다는 성질도 함께 검증한다.
"""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.exit.engine import EXIT_SL, EXIT_TP, ExitResult, Signal
from stock_auto.exit.filters import (
    REASON_CONFIDENCE,
    REASON_COOLDOWN,
    REASON_SECTOR,
    FilterConfig,
    apply_filters,
    split_by_filter,
)


def _sig(sym: str, day: str, *, conf: float = 0.6, sector: str = "XLK") -> Signal:
    return Signal(sym, np.datetime64(day, "D"), "단타", confidence=conf, sector=sector)


def _exit(sym: str, sig_day: str, exit_day: str, kind: str) -> ExitResult:
    return ExitResult(
        symbol=sym, label="단타", confidence=0.6, sector="XLK", source="",
        signal_date=np.datetime64(sig_day, "D"),
        entry_date=np.datetime64(sig_day, "D"), entry_price=100.0,
        exit_type=kind, exit_date=np.datetime64(exit_day, "D"),
        exit_price=97.0, return_pct=-3.0, hold_days=1,
        mfe_pct=1.0, mae_pct=-3.0,
    )


CAL = np.array(
    [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)],
    dtype="datetime64[D]",
)


# --- 확신도 ---------------------------------------------------------------
def test_low_confidence_is_flagged_not_dropped():
    """차단분도 목록에 남아야 한다 — 나중에 "그 필터가 옳았나"를 물어야 하므로."""
    sigs = [_sig("A", "2026-01-05", conf=0.45), _sig("B", "2026-01-05", conf=0.60)]
    ds = apply_filters(sigs, FilterConfig(), calendar=CAL)
    assert len(ds) == 2                       # 지워지지 않았다
    assert ds[0].executable is False
    assert REASON_CONFIDENCE in ds[0].reasons
    assert ds[1].executable is True


def test_missing_confidence_is_not_blocked():
    """확신도 컬럼이 없던 과거 구간을 통째로 날리면 표본이 사라진다."""
    sigs = [_sig("A", "2026-01-05", conf=float("nan"))]
    ds = apply_filters(sigs, FilterConfig(), calendar=CAL)
    assert ds[0].executable is True


def test_confidence_threshold_is_configurable_and_disableable():
    sigs = [_sig("A", "2026-01-05", conf=0.45)]
    assert apply_filters(sigs, FilterConfig(min_confidence=None), calendar=CAL)[0].executable
    assert apply_filters(sigs, FilterConfig(min_confidence=0.40), calendar=CAL)[0].executable


# --- 쿨다운 ---------------------------------------------------------------
def test_cooldown_blocks_reentry_after_sl():
    """SWKS가 손절 직후 재추천되던 패턴(analysis §3-4)을 막는다."""
    prior = [_exit("SWKS", "2026-01-05", "2026-01-06", EXIT_SL)]
    # 01-07은 청산 다음 거래일 → 쿨다운 3거래일 안
    blocked = apply_filters(
        [_sig("SWKS", "2026-01-07")], FilterConfig(cooldown_days=3),
        prior_exits=prior, calendar=CAL,
    )
    assert REASON_COOLDOWN in blocked[0].reasons

    # 01-12는 거래일 기준 4일 뒤 → 통과
    allowed = apply_filters(
        [_sig("SWKS", "2026-01-12")], FilterConfig(cooldown_days=3),
        prior_exits=prior, calendar=CAL,
    )
    assert allowed[0].executable


def test_cooldown_counts_trading_days_not_calendar_days():
    """주말이 끼면 달력 3일과 거래일 3일이 다르다."""
    prior = [_exit("A", "2026-01-08", "2026-01-09", EXIT_SL)]
    # 01-12는 달력상 3일 뒤(주말 포함)지만 거래일로는 1일 뒤 → 차단
    ds = apply_filters(
        [_sig("A", "2026-01-12")], FilterConfig(cooldown_days=3),
        prior_exits=prior, calendar=CAL,
    )
    assert REASON_COOLDOWN in ds[0].reasons


def test_cooldown_only_triggers_on_sl_not_tp():
    prior = [_exit("A", "2026-01-05", "2026-01-06", EXIT_TP)]
    ds = apply_filters(
        [_sig("A", "2026-01-07")], FilterConfig(cooldown_days=3),
        prior_exits=prior, calendar=CAL,
    )
    assert ds[0].executable


def test_cooldown_never_looks_at_future_exits():
    """미래 청산을 보고 차단하면 그건 누출이다."""
    prior = [_exit("A", "2026-01-13", "2026-01-14", EXIT_SL)]
    ds = apply_filters(
        [_sig("A", "2026-01-06")], FilterConfig(cooldown_days=3),
        prior_exits=prior, calendar=CAL,
    )
    assert ds[0].executable


# --- 섹터 한도 -------------------------------------------------------------
def test_sector_cap_blocks_third_concurrent_position():
    """09-17 대기 6건 중 4건이 반도체였던 상황(analysis §3-4)을 막는다."""
    sigs = [
        _sig("A", "2026-01-05", sector="XLK"),
        _sig("B", "2026-01-05", sector="XLK"),
        _sig("C", "2026-01-05", sector="XLK"),
        _sig("D", "2026-01-05", sector="XLE"),
    ]
    # 세 건 모두 01-09까지 보유 중이라고 알려 준다.
    prior = [
        ExitResult(
            symbol=s.symbol, label="단타", confidence=s.confidence, sector=s.sector,
            source="", signal_date=s.signal_date,
            entry_date=s.signal_date, entry_price=100.0, exit_type=EXIT_TP,
            exit_date=np.datetime64("2026-01-09", "D"), exit_price=103.0,
            return_pct=3.0, hold_days=3, mfe_pct=3.0, mae_pct=-1.0,
        )
        for s in sigs
    ]
    ds = apply_filters(sigs, FilterConfig(max_per_sector=2), prior_exits=prior, calendar=CAL)
    assert ds[0].executable and ds[1].executable
    assert REASON_SECTOR in ds[2].reasons        # 세 번째 XLK
    assert ds[3].executable                      # 다른 섹터는 통과


def test_blocked_signals_do_not_occupy_a_sector_slot():
    """차단된 건이 포지션을 차지하면 뒤 건이 억울하게 막힌다."""
    sigs = [
        _sig("A", "2026-01-05", conf=0.30, sector="XLK"),   # 확신도로 차단
        _sig("B", "2026-01-05", sector="XLK"),
        _sig("C", "2026-01-05", sector="XLK"),
    ]
    prior = [
        ExitResult(
            symbol=s.symbol, label="단타", confidence=s.confidence, sector=s.sector,
            source="", signal_date=s.signal_date, entry_date=s.signal_date,
            entry_price=100.0, exit_type=EXIT_TP,
            exit_date=np.datetime64("2026-01-09", "D"), exit_price=103.0,
            return_pct=3.0, hold_days=3, mfe_pct=3.0, mae_pct=-1.0,
        )
        for s in sigs
    ]
    ds = apply_filters(sigs, FilterConfig(max_per_sector=2), prior_exits=prior, calendar=CAL)
    assert not ds[0].executable
    assert ds[1].executable and ds[2].executable     # A가 자리를 안 먹었다


def test_disabled_filters_pass_everything():
    sigs = [_sig("A", "2026-01-05", conf=0.1), _sig("A", "2026-01-06", conf=0.1)]
    ds = apply_filters(sigs, FilterConfig(enabled=False), calendar=CAL)
    assert all(d.executable for d in ds)


def test_split_by_filter_returns_both_sides():
    sigs = [_sig("A", "2026-01-05", conf=0.45), _sig("B", "2026-01-05", conf=0.60)]
    passed, decisions = split_by_filter(sigs, FilterConfig(), calendar=CAL)
    assert len(passed) == 1 and len(decisions) == 2


def test_multiple_reasons_accumulate():
    prior = [_exit("A", "2026-01-05", "2026-01-06", EXIT_SL)]
    ds = apply_filters(
        [_sig("A", "2026-01-07", conf=0.30)], FilterConfig(),
        prior_exits=prior, calendar=CAL,
    )
    assert REASON_CONFIDENCE in ds[0].reasons
    assert REASON_COOLDOWN in ds[0].reasons
    assert "제외" in ds[0].pill
