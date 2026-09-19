"""성과 지표 — PF, 기대값, 손익비, MDD, 포착률, 라벨별 분해."""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.exit.engine import EXIT_NONE, EXIT_SL, EXIT_TO, EXIT_TP, ExitResult
from stock_auto.exit.metrics import by_confidence_bucket, by_label, compute_metrics


def _r(ret: float, *, label: str = "단타", kind: str = EXIT_TP, day: int = 5,
       conf: float = 0.6, mfe: float = 10.0, mfe_hold: float = 5.0) -> ExitResult:
    d = np.datetime64(f"2026-01-{day:02d}", "D")
    return ExitResult(
        symbol="T", label=label, confidence=conf, sector="XLK", source="",
        signal_date=d, entry_date=d, entry_price=100.0, exit_type=kind,
        exit_date=d, exit_price=100.0 + ret, return_pct=ret, hold_days=1,
        mfe_pct=mfe, mae_pct=-2.0, mfe_hold_pct=mfe_hold,
    )


def test_profit_factor_and_payoff():
    res = [_r(3.0), _r(3.0), _r(-2.0, kind=EXIT_SL), _r(-2.0, kind=EXIT_SL)]
    m = compute_metrics(res)
    assert m.n == 4
    assert m.win_rate == pytest.approx(50.0)
    assert m.profit_factor == pytest.approx(6.0 / 4.0)
    assert m.avg_win == pytest.approx(3.0)
    assert m.avg_loss == pytest.approx(-2.0)
    assert m.payoff_ratio == pytest.approx(1.5)
    assert m.expectancy == pytest.approx(0.5)


def test_losing_set_reproduces_the_reported_shape():
    """analysis §2의 구조 — 승률 42%, 평균익절 < 평균손실 → PF < 1."""
    res = [_r(2.7) for _ in range(5)] + [_r(-3.48, kind=EXIT_SL) for _ in range(5)] \
        + [_r(-0.87, kind=EXIT_TO) for _ in range(2)]
    m = compute_metrics(res)
    assert m.win_rate == pytest.approx(5 / 12 * 100, abs=0.1)
    assert m.profit_factor < 1.0
    assert m.expectancy < 0
    assert m.payoff_ratio < 1.0
    assert not any(m.meets_targets().values())


def test_skipped_results_are_excluded_from_denominator():
    """판정 못 한 건을 0%로 세면 기대값이 0쪽으로 희석된다."""
    good = _r(4.0)
    bad = ExitResult(
        symbol="X", label="단타", confidence=0.6, sector="", source="",
        signal_date=np.datetime64("2026-01-05"), entry_date=None,
        entry_price=float("nan"), exit_type=EXIT_NONE, exit_date=None,
        exit_price=float("nan"), return_pct=float("nan"), hold_days=0,
        mfe_pct=float("nan"), mae_pct=float("nan"),
    )
    m = compute_metrics([good, bad])
    assert m.n == 1 and m.n_skipped == 1
    assert m.expectancy == pytest.approx(4.0)


def test_max_drawdown_follows_time_order():
    """MDD는 순서에 의존한다 — 시간순 정렬이 안 되면 값이 틀린다."""
    res = [_r(5.0, day=5), _r(-8.0, kind=EXIT_SL, day=6), _r(5.0, day=7)]
    m = compute_metrics(res)
    assert m.max_drawdown == pytest.approx(-8.0)

    # 입력 순서를 섞어도 같은 값이 나와야 한다.
    m2 = compute_metrics([res[2], res[0], res[1]])
    assert m2.max_drawdown == pytest.approx(-8.0)


def test_capture_ratio_uses_ratio_of_means():
    """건별 비율의 평균을 쓰면 MFE≈0인 건에서 값이 폭발한다."""
    res = [_r(2.0, mfe=10.0), _r(1.0, mfe=0.001)]
    m = compute_metrics(res)
    # 평균의 비율 = 1.5 / 5.0005 ≈ 0.3. 건별 평균이면 500에 가까워진다.
    assert 0.2 < m.capture_ratio < 0.4


def test_hold_capture_is_separate_from_mfe20_capture():
    res = [_r(4.0, mfe=20.0, mfe_hold=5.0)]
    m = compute_metrics(res)
    assert m.capture_ratio == pytest.approx(0.2)
    assert m.hold_capture_ratio == pytest.approx(0.8)


def test_zero_loss_set_gives_inf_not_crash():
    m = compute_metrics([_r(1.0), _r(2.0)])
    assert np.isinf(m.profit_factor)


def test_empty_input_is_safe():
    m = compute_metrics([])
    assert m.n == 0 and not np.isfinite(m.expectancy)


def test_by_label_keeps_standard_order():
    """리포트는 항상 단타 → 중단기 → 스윙 순서로 나와야 한다."""
    res = [_r(1.0, label="스윙"), _r(1.0, label="단타"), _r(1.0, label="중단기")]
    assert list(by_label(res)) == ["단타", "중단기", "스윙"]


def test_by_label_separates_metrics():
    res = [_r(5.0, label="스윙"), _r(-5.0, kind=EXIT_SL, label="단타")]
    out = by_label(res)
    assert out["스윙"].expectancy == pytest.approx(5.0)
    assert out["단타"].expectancy == pytest.approx(-5.0)


def test_confidence_buckets_split_at_050():
    """analysis §3-3의 "0.50 미만" 경계가 정확히 잡혀야 한다."""
    res = [_r(1.0, conf=0.49), _r(1.0, conf=0.50), _r(1.0, conf=0.62)]
    out = by_confidence_bucket(res)
    assert out["<0.50"].n == 1
    assert out["0.50~0.55"].n == 1
    assert out["0.60+"].n == 1


def test_targets_match_the_documented_thresholds():
    res = [_r(3.0)] * 7 + [_r(-1.0, kind=EXIT_SL)] * 3
    m = compute_metrics(res)
    t = m.meets_targets()
    assert set(t) == {"PF≥1.3", "손익비≥1.5", "기대값>+0.5%", "MFE포착≥50%"}
    assert t["PF≥1.3"] and t["손익비≥1.5"] and t["기대값>+0.5%"]
