"""100일 러너 — 구간 자르기와 리포트 생성."""

from __future__ import annotations

import numpy as np
import pytest

from stock_auto.backtest.dataset import make_synthetic_dataset
from stock_auto.backtest.run_100d import DEFAULT_WINDOW, main, slice_window


def test_window_is_counted_in_trading_days():
    """달력 100일로 자르면 주말이 들어가 실제로는 70일 남짓만 본다."""
    ds = make_synthetic_dataset(n_symbols=6, n_days=300, seed=11)
    sub, lo, hi = slice_window(ds, 100, None)
    cal = ds.calendar
    in_window = cal[(cal >= lo) & (cal <= hi)]
    assert in_window.size == 100
    # 거래일 100일은 달력으로 130일을 훌쩍 넘는다.
    assert (hi - lo).astype(int) > 130


def test_window_anchors_to_last_trading_day_by_default():
    ds = make_synthetic_dataset(n_symbols=5, n_days=200, seed=12)
    _, _, hi = slice_window(ds, 50, None)
    assert hi == ds.calendar[-1]


def test_explicit_end_date_is_respected():
    ds = make_synthetic_dataset(n_symbols=5, n_days=200, seed=13)
    target = ds.calendar[-30]
    _, _, hi = slice_window(ds, 50, str(target))
    assert hi == target


def test_window_longer_than_history_clamps_to_start():
    ds = make_synthetic_dataset(n_symbols=4, n_days=60, seed=14)
    _, lo, _ = slice_window(ds, 500, None)
    assert lo == ds.calendar[0]


def test_slice_keeps_all_price_data():
    """청산이 구간 밖으로 넘어가므로 가격은 자르면 안 된다."""
    ds = make_synthetic_dataset(n_symbols=7, n_days=250, seed=15)
    sub, _, _ = slice_window(ds, 100, None)
    assert len(sub.prices) == len(ds.prices)
    assert len(sub.signals) < len(ds.signals)


def test_default_window_is_100():
    assert DEFAULT_WINDOW == 100


def test_end_to_end_writes_report_and_csv(tmp_path):
    rc = main([
        "--synthetic", "--syn-symbols", "6", "--syn-per-day", "2",
        "--out-dir", str(tmp_path), "--quiet",
    ])
    assert rc == 0
    md = tmp_path / "backtest_100d.md"
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    # 세 라벨이 반드시 분리돼 나와야 한다.
    for lab in ("단타", "중단기", "스윙"):
        assert f"### {lab}" in text
    assert "라벨별" in text
    assert (tmp_path / "trades_100d.csv").exists()


def test_report_carries_the_window_caveat(tmp_path):
    """100일 구간으로 파라미터를 고르지 말라는 경고가 빠지면 안 된다."""
    main(["--synthetic", "--syn-symbols", "5", "--out-dir", str(tmp_path), "--quiet"])
    text = (tmp_path / "backtest_100d.md").read_text(encoding="utf-8")
    assert "파라미터 결정에는 쓰지 않는다" in text
    assert "walk-forward" in text


def test_synthetic_report_is_labelled_as_synthetic(tmp_path):
    main(["--synthetic", "--syn-symbols", "5", "--out-dir", str(tmp_path), "--quiet"])
    text = (tmp_path / "backtest_100d.md").read_text(encoding="utf-8")
    assert "성과 수치로 읽으면 안 된다" in text
