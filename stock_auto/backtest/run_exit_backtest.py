"""청산 백테스트 실행기.

    python -m stock_auto.backtest.run_exit_backtest --help

TASK.md Phase 1의 산출물(결과 CSV + 상위 10개 요약 markdown)을 만든다.
실데이터가 없는 환경에서도 `--synthetic`으로 파이프라인 전체를 검증할 수
있게 해 두었다 — 단, 그 결과는 성과가 아니라 배선 확인용이다.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from stock_auto.backtest.dataset import (
    Dataset,
    load_prices,
    load_signals,
    make_synthetic_dataset,
)
from stock_auto.backtest.exit_grid import GridSpec, run_grid, to_dataframe
from stock_auto.backtest.report import build_report
from stock_auto.exit.config import ExitConfig
from stock_auto.exit.engine import simulate_all
from stock_auto.exit.filters import FilterConfig, split_by_filter

SYNTHETIC_WARNING = (
    "합성 데이터 결과다. **성과 수치로 읽으면 안 된다.** 배선·실행시간 확인 전용."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_exit_backtest",
        description="청산 규칙 백테스트 + 파라미터 그리드 (라벨별 분리)",
    )
    src = p.add_argument_group("데이터")
    src.add_argument("--signals", type=Path, help="시그널 CSV (예: data/tracking/signals.csv)")
    src.add_argument("--price-dir", type=Path, help="일봉 CSV 캐시 디렉토리")
    src.add_argument("--no-download", action="store_true", help="FDR/yfinance 조회 금지 (캐시만)")
    src.add_argument("--synthetic", action="store_true", help="합성 데이터로 실행 (검증/벤치마크)")
    src.add_argument("--syn-symbols", type=int, default=20)
    src.add_argument("--syn-days", type=int, default=400)
    src.add_argument("--syn-per-day", type=float, default=3.0)
    src.add_argument("--seed", type=int, default=20260918)
    src.add_argument("--start", help="시그널 시작일 (YYYY-MM-DD)")
    src.add_argument("--end", help="시그널 종료일 (YYYY-MM-DD)")
    src.add_argument("--lookback-days", type=int,
                     help="종료일로부터 거슬러 N 거래일만 (예: 100)")

    cfgg = p.add_argument_group("청산 설정")
    cfgg.add_argument("--config", choices=("legacy", "proposed"), default="proposed",
                      help="본 실행에 쓸 설정 (기본 proposed)")
    cfgg.add_argument("--slippage-bps", type=float, default=None,
                      help="편도 슬리피지(bp). 지정 시 프리셋 값을 덮어쓴다")

    gridg = p.add_argument_group("그리드")
    gridg.add_argument("--grid", action="store_true", help="파라미터 그리드 실행")
    gridg.add_argument("--no-walk-forward", action="store_true")
    gridg.add_argument("--train-frac", type=float, default=0.6)
    gridg.add_argument("--min-n", type=int, default=100,
                       help="최적값 채택 최소 표본 (TASK.md 완료 기준)")
    gridg.add_argument("--with-partial-tp", action="store_true",
                       help="부분익절 축 추가 (셀 수 3배)")
    gridg.add_argument("--with-trail-be", action="store_true",
                       help="본전 트레일 축 추가 (셀 수 3배)")

    filt = p.add_argument_group("실행 필터")
    filt.add_argument("--no-filters", action="store_true")
    filt.add_argument("--min-confidence", type=float, default=0.50)
    filt.add_argument("--cooldown-days", type=int, default=3)
    filt.add_argument("--max-per-sector", type=int, default=2)

    out = p.add_argument_group("출력")
    out.add_argument("--out-dir", type=Path, default=Path("experiments/results"))
    out.add_argument("--tag", default="", help="출력 파일명 접미사")
    out.add_argument("--quiet", action="store_true")
    return p


def load_dataset(args: argparse.Namespace) -> Dataset:
    """인자에 따라 실데이터 또는 합성 데이터를 준비한다."""
    if args.synthetic or not args.signals:
        if not args.synthetic:
            print(
                "⚠️  --signals가 없어 합성 데이터로 돌린다. 실데이터를 쓰려면\n"
                "    --signals data/tracking/signals.csv --price-dir data/cache 를 지정한다.",
                file=sys.stderr,
            )
        return make_synthetic_dataset(
            n_symbols=args.syn_symbols,
            n_days=args.syn_days,
            signals_per_day=args.syn_per_day,
            seed=args.seed,
        )

    sigs = load_signals(args.signals)
    if not sigs:
        raise SystemExit(f"{args.signals}: 시그널 0건")
    symbols = {s.symbol for s in sigs}
    if not args.quiet:
        print(f"시그널 {len(sigs)}건 · 종목 {len(symbols)}개 — 일봉 로딩...")

    # 청산이 마지막 시그널 이후로 넘어가므로 뒤쪽 여유를 둔다.
    dl_start = (min(s.signal_date for s in sigs) - np.timedelta64(60, "D")).astype(str)
    dl_end = (max(s.signal_date for s in sigs) + np.timedelta64(45, "D")).astype(str)
    prices, missing = load_prices(
        symbols, price_dir=args.price_dir, start=dl_start, end=dl_end,
        allow_download=not args.no_download, verbose=not args.quiet,
    )
    if missing:
        print(
            f"⚠️  일봉 없음 {len(missing)}종목: {', '.join(missing[:10])}"
            + (" ..." if len(missing) > 10 else ""),
            file=sys.stderr,
        )
    if not prices:
        raise SystemExit(
            "일봉을 하나도 못 받았다. --price-dir 캐시를 지정하거나 "
            "네트워크/라이브러리(FinanceDataReader, yfinance)를 확인한다."
        )
    return Dataset(sigs, prices, note="실데이터")


def apply_date_window(ds: Dataset, args: argparse.Namespace) -> Dataset:
    """--start/--end/--lookback-days 적용."""
    start = np.datetime64(args.start, "D") if args.start else None
    end = np.datetime64(args.end, "D") if args.end else None

    if args.lookback_days:
        cal = ds.calendar
        anchor = end if end is not None else (cal[-1] if cal.size else None)
        if anchor is not None and cal.size:
            i = int(np.searchsorted(cal, anchor, side="right")) - 1
            j = max(0, i - args.lookback_days + 1)
            start, end = cal[j], cal[i]
    return ds.filter_dates(start, end)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t0 = time.time()

    ds = apply_date_window(load_dataset(args), args)
    if not args.quiet:
        print(f"데이터: {ds.describe()}")
    if not ds.signals:
        raise SystemExit("해당 구간에 시그널이 0건이다. --start/--end를 확인한다.")

    cfg = ExitConfig.legacy() if args.config == "legacy" else ExitConfig.proposed()
    if args.slippage_bps is not None:
        from dataclasses import replace
        cfg = replace(cfg, slippage_bps=args.slippage_bps)

    # --- 본 실행 ---------------------------------------------------------
    results = simulate_all(ds.signals, ds.prices, cfg)
    legacy_results = (
        simulate_all(ds.signals, ds.prices, ExitConfig.legacy())
        if args.config != "legacy" else []
    )

    # --- 실행 필터 --------------------------------------------------------
    fcfg = FilterConfig(
        min_confidence=None if args.no_filters else args.min_confidence,
        cooldown_days=0 if args.no_filters else args.cooldown_days,
        max_per_sector=0 if args.no_filters else args.max_per_sector,
        enabled=not args.no_filters,
    )
    passed, decisions = split_by_filter(
        ds.signals, fcfg, prior_exits=results, calendar=ds.calendar
    )
    results_filtered = simulate_all(passed, ds.prices, cfg) if passed else []

    # --- 그리드 ----------------------------------------------------------
    grid_rows = []
    if args.grid:
        spec = GridSpec(
            partial_tp=(None, 0.5, 0.6) if args.with_partial_tp else (None,),
            trail_be_atr=(None, 1.0, 1.5) if args.with_trail_be else (None,),
        )
        if not args.quiet:
            print(f"그리드: 라벨당 {spec.size()}셀")
        grid_rows = run_grid(
            ds, spec, train_frac=args.train_frac,
            walk_forward=not args.no_walk_forward, verbose=not args.quiet,
        )

    # --- 출력 ------------------------------------------------------------
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    synthetic = "합성" in ds.note

    md = build_report(
        title=f"청산 백테스트 리포트 ({args.config})",
        dataset_desc=ds.describe() + f" · {ds.note}",
        results=results, cfg=cfg, grid_rows=grid_rows,
        legacy_results=legacy_results, decisions=decisions,
        filter_cfg=fcfg, results_filtered=results_filtered,
        min_n=args.min_n,
        data_note=SYNTHETIC_WARNING if synthetic else "",
    )
    md_path = args.out_dir / f"exit_backtest{tag}.md"
    md_path.write_text(md, encoding="utf-8")

    import pandas as pd

    trades = pd.DataFrame(
        [
            {
                "symbol": r.symbol, "label": r.label, "confidence": r.confidence,
                "sector": r.sector, "source": r.source,
                "signal_date": r.signal_date, "entry_date": r.entry_date,
                "entry_price": r.entry_price, "exit_type": r.exit_type,
                "exit_date": r.exit_date, "exit_price": r.exit_price,
                "return_pct": r.return_pct, "hold_days": r.hold_days,
                "mfe_pct": r.mfe_pct, "mae_pct": r.mae_pct,
                "capture_ratio": r.capture_ratio,
                **{f"dplus{k}": v for k, v in sorted(r.dplus.items())},
                "note": r.note,
            }
            for r in results
        ]
    )
    trades_path = args.out_dir / f"exit_trades{tag}.csv"
    trades.to_csv(trades_path, index=False)

    grid_path = None
    if grid_rows:
        grid_path = args.out_dir / f"exit_grid{tag}.csv"
        to_dataframe(grid_rows).to_csv(grid_path, index=False)

    el = time.time() - t0
    if not args.quiet:
        print(f"\n{'=' * 64}")
        print(f"리포트   {md_path}")
        print(f"체결내역 {trades_path}")
        if grid_path:
            print(f"그리드   {grid_path}")
        print(f"소요     {el:.1f}s")
        if synthetic:
            print(f"\n⚠️  {SYNTHETIC_WARNING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
