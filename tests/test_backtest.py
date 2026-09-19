"""백테스트 하네스 — 로더, walk-forward 분할, 그리드, 표본 부족 처리."""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.backtest.dataset import Dataset, load_signals, make_synthetic_dataset
from stock_auto.backtest.exit_grid import (
    GridSpec,
    best_per_label,
    run_grid,
    walk_forward_split,
)
from stock_auto.exit.engine import Signal
from stock_auto.horizons import ALL_LABELS


# --- walk-forward ---------------------------------------------------------
def _sigs(days: list[str]) -> list[Signal]:
    return [Signal("T", np.datetime64(d, "D"), "단타") for d in days]


def test_walk_forward_splits_on_date_not_count():
    """같은 날짜가 train/test로 쪼개지면 검증이 무의미해진다."""
    days = ["2026-01-05"] * 5 + ["2026-01-06"] * 5 + ["2026-01-07"] * 5
    cut, train, test = walk_forward_split(_sigs(days), train_frac=0.6)
    assert cut is not None
    assert all(s.signal_date < cut for s in train)
    assert all(s.signal_date >= cut for s in test)
    # 어떤 날짜도 양쪽에 동시에 있으면 안 된다.
    assert not ({s.signal_date for s in train} & {s.signal_date for s in test})


def test_walk_forward_is_time_ordered():
    days = [f"2026-01-{d:02d}" for d in range(5, 25)]
    cut, train, test = walk_forward_split(_sigs(days), train_frac=0.6)
    assert max(s.signal_date for s in train) < min(s.signal_date for s in test)


def test_walk_forward_single_date_yields_no_test():
    cut, train, test = walk_forward_split(_sigs(["2026-01-05"] * 3))
    assert cut is None and len(train) == 3 and not test


def test_walk_forward_empty():
    assert walk_forward_split([]) == (None, [], [])


# --- 그리드 ---------------------------------------------------------------
def test_grid_spec_size_matches_task_document():
    """TASK.md Phase 1: hold 4 × sl 5 × tp 5 × close_based 2 = 200."""
    assert GridSpec().size() == 200


def test_grid_runs_per_label_separately():
    ds = make_synthetic_dataset(n_symbols=6, n_days=200, signals_per_day=2.0, seed=3)
    spec = GridSpec(holds=(1, 3), sls=(("pct", 3.0),), tps=(("pct", 3.0),),
                    close_based=(False,))
    rows = run_grid(ds, spec, verbose=False)
    labels = {r.label for r in rows}
    assert labels <= set(ALL_LABELS)
    # 라벨마다 셀 전체가 돌아야 한다.
    for lab in labels:
        assert sum(1 for r in rows if r.label == lab) == spec.size()


def test_grid_rows_carry_train_and_test():
    """train만 좋은 셀을 걸러내려면 둘 다 있어야 한다."""
    ds = make_synthetic_dataset(n_symbols=6, n_days=200, signals_per_day=2.0, seed=4)
    spec = GridSpec(holds=(2,), sls=(("pct", 3.0),), tps=(("pct", 3.0),),
                    close_based=(False,))
    rows = run_grid(ds, spec, verbose=False)
    assert rows and all(r.train is not None and r.test is not None for r in rows)


def test_best_per_label_refuses_when_sample_too_small():
    """표본이 부족할 때 1등을 집어 주면 그게 과적합이다."""
    ds = make_synthetic_dataset(n_symbols=4, n_days=120, signals_per_day=0.3, seed=5)
    spec = GridSpec(holds=(1, 2), sls=(("pct", 3.0),), tps=(("pct", 3.0),),
                    close_based=(False,))
    rows = run_grid(ds, spec, verbose=False)
    best = best_per_label(rows, min_n=100_000)      # 절대 못 채우는 기준
    assert all(v is None for v in best.values())


def test_best_per_label_picks_when_sample_sufficient():
    ds = make_synthetic_dataset(n_symbols=10, n_days=300, signals_per_day=4.0, seed=6)
    spec = GridSpec(holds=(1, 3), sls=(("pct", 3.0),), tps=(("pct", 3.0),),
                    close_based=(False,))
    rows = run_grid(ds, spec, verbose=False)
    best = best_per_label(rows, min_n=10)
    assert any(v is not None for v in best.values())


def test_grid_score_prefers_test_expectancy():
    ds = make_synthetic_dataset(n_symbols=6, n_days=200, signals_per_day=2.0, seed=7)
    spec = GridSpec(holds=(2,), sls=(("pct", 3.0),), tps=(("pct", 3.0),),
                    close_based=(False,))
    rows = run_grid(ds, spec, verbose=False)
    r = rows[0]
    assert r.score == pytest.approx(r.test.expectancy)


# --- 데이터셋 -------------------------------------------------------------
def test_synthetic_dataset_is_labelled_across_all_three():
    ds = make_synthetic_dataset(n_symbols=8, n_days=200, seed=8)
    counts = ds.label_counts()
    assert all(counts.get(l, 0) > 0 for l in ALL_LABELS)


def test_dataset_filter_dates_keeps_prices():
    """시그널만 자르고 가격은 남겨야 한다 — 청산이 구간 밖으로 나가므로."""
    ds = make_synthetic_dataset(n_symbols=5, n_days=200, seed=9)
    lo, hi = ds.span
    mid = lo + (hi - lo) // 2
    sub = ds.filter_dates(mid, hi)
    assert len(sub.signals) < len(ds.signals)
    assert len(sub.prices) == len(ds.prices)


def test_load_signals_maps_aliased_columns(tmp_path):
    """컬럼명이 달라도 읽혀야 한다 — 이름 하나로 백테스트가 멈추면 안 된다."""
    csv = tmp_path / "s.csv"
    csv.write_text(
        "ticker,기준일,보유기간,conviction,섹터\n"
        "AAPL,2026-01-05,스윙,0.61,XLK\n"
        "MSFT,2026-01-06,day,0.48,XLK\n",
        encoding="utf-8",
    )
    sigs = load_signals(csv)
    assert len(sigs) == 2
    assert sigs[0].symbol == "AAPL" and sigs[0].label == "스윙"
    assert sigs[0].confidence == pytest.approx(0.61)
    assert sigs[1].label == "단타"


def test_load_signals_flags_defaulted_labels(tmp_path):
    """라벨이 없으면 기본값을 쓰되 그 사실을 남긴다."""
    csv = tmp_path / "s.csv"
    csv.write_text("symbol,date\nAAPL,2026-01-05\n", encoding="utf-8")
    sigs = load_signals(csv, default_label="중단기")
    assert sigs[0].label == "중단기"
    assert "label=default" in sigs[0].source


def test_load_signals_rejects_missing_key_columns(tmp_path):
    csv = tmp_path / "s.csv"
    csv.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="종목/날짜"):
        load_signals(csv)
