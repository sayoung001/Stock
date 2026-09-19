"""백테스트 하네스 — 시그널·가격 로딩, 청산 그리드, 리포트."""

from stock_auto.backtest.dataset import (
    Dataset,
    load_prices,
    load_signals,
    make_synthetic_dataset,
)
from stock_auto.backtest.exit_grid import GridSpec, GridRow, run_grid, walk_forward_split

__all__ = [
    "Dataset",
    "load_signals",
    "load_prices",
    "make_synthetic_dataset",
    "GridSpec",
    "GridRow",
    "run_grid",
    "walk_forward_split",
]
