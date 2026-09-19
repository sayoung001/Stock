"""100일 백테스트 — 최근 100 거래일 구간 집중 분석.

`run_exit_backtest.py`가 전 구간 그리드라면, 이쪽은 **최근 100 거래일**에
초점을 맞춘 별도 러너다. 용도가 다르다:

| | run_exit_backtest | run_100d |
|---|---|---|
| 구간 | 시그널 전 구간 | 최근 100 거래일 |
| 목적 | 파라미터 **결정** (그리드 + walk-forward) | 결정한 파라미터의 **최근 성적 확인** |
| 표본 | 많을수록 좋음 | 100일치 — 대개 n < 100 |

**이 러너의 결과로 파라미터를 고르면 안 된다.** 100 거래일은 시장 국면
하나를 겨우 덮는 길이라, 여기서 1등인 설정은 그 국면에 맞춘 것일 뿐이다.
파라미터는 전 구간 walk-forward가 정하고, 이 러너는 "그 설정이 최근에도
살아 있나"를 본다.

세 라벨(단타/중단기/스윙)을 **반드시 분리**해 출력한다. 100일 구간에서는
라벨별 표본이 특히 작아지므로, 합쳐 놓으면 어느 지평이 무너졌는지 완전히
가려진다.

    python -m stock_auto.backtest.run_100d --synthetic
    python -m stock_auto.backtest.run_100d \
        --signals data/tracking/signals.csv --price-dir data/cache
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from stock_auto.advisor.advisor import advise_overall, diagnose_label
from stock_auto.backtest.dataset import (
    Dataset,
    load_prices,
    load_signals,
    make_synthetic_dataset,
)
from stock_auto.backtest.exit_grid import GridSpec, run_grid, to_dataframe
from stock_auto.backtest.report import (
    counterfactual_table,
    filter_table,
    grid_top_table,
    metrics_table,
)
from stock_auto.exit.config import ExitConfig
from stock_auto.exit.engine import simulate_all
from stock_auto.exit.filters import FilterConfig, split_by_filter
from stock_auto.exit.metrics import (
    by_confidence_bucket,
    by_label,
    by_sector,
    compute_metrics,
)
from stock_auto.horizons import ALL_LABELS, SCORING_HORIZON_DAYS

DEFAULT_WINDOW = 100

SYNTHETIC_WARNING = (
    "합성 데이터다. **성과 수치로 읽으면 안 된다.** 배선·실행시간 확인 전용."
)

WINDOW_CAVEAT = (
    "100 거래일은 시장 국면 하나를 겨우 덮는 길이다. 여기서 1등인 설정은 "
    "그 국면에 맞춘 것일 수 있으므로, **파라미터 결정에는 쓰지 않는다.** "
    "결정은 `run_exit_backtest --grid`의 walk-forward가 한다."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_100d",
        description="최근 100 거래일 청산 백테스트 (단타/중단기/스윙 분리)",
    )
    p.add_argument("--signals", type=Path, help="시그널 CSV")
    p.add_argument("--price-dir", type=Path, help="일봉 CSV 캐시 디렉토리")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--synthetic", action="store_true", help="합성 데이터로 실행")
    p.add_argument("--syn-symbols", type=int, default=20)
    p.add_argument("--syn-per-day", type=float, default=3.0)
    p.add_argument("--seed", type=int, default=20260918)

    p.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                   help=f"거래일 수 (기본 {DEFAULT_WINDOW})")
    p.add_argument("--end", help="구간 종료일 YYYY-MM-DD (기본: 데이터 마지막 날)")

    p.add_argument("--config", choices=("legacy", "proposed"), default="proposed")
    p.add_argument("--slippage-bps", type=float, default=None)
    p.add_argument("--compare-legacy", action="store_true", default=True,
                   help="legacy와 나란히 비교 (기본 켜짐)")

    p.add_argument("--grid", action="store_true",
                   help="이 구간에서도 그리드를 돌린다 (참고용 — 결정 근거 아님)")
    p.add_argument("--min-n", type=int, default=100)

    p.add_argument("--no-filters", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.50)
    p.add_argument("--cooldown-days", type=int, default=3)
    p.add_argument("--max-per-sector", type=int, default=2)

    p.add_argument("--out-dir", type=Path, default=Path("experiments/results"))
    p.add_argument("--tag", default="100d")
    p.add_argument("--quiet", action="store_true")
    return p


def _load(args: argparse.Namespace) -> Dataset:
    if args.synthetic or not args.signals:
        if not args.synthetic:
            print(
                "⚠️  --signals가 없어 합성 데이터로 돌린다.", file=sys.stderr
            )
        # 100일 구간 + ATR 워밍업 + 청산 여유를 위해 넉넉히 만든다.
        return make_synthetic_dataset(
            n_symbols=args.syn_symbols,
            n_days=args.window + 120,
            signals_per_day=args.syn_per_day,
            seed=args.seed,
        )

    sigs = load_signals(args.signals)
    if not sigs:
        raise SystemExit(f"{args.signals}: 시그널 0건")
    symbols = {s.symbol for s in sigs}
    if not args.quiet:
        print(f"시그널 {len(sigs)}건 · 종목 {len(symbols)}개 — 일봉 로딩...")
    dl_start = (min(s.signal_date for s in sigs) - np.timedelta64(60, "D")).astype(str)
    dl_end = (max(s.signal_date for s in sigs) + np.timedelta64(45, "D")).astype(str)
    prices, missing = load_prices(
        symbols, price_dir=args.price_dir, start=dl_start, end=dl_end,
        allow_download=not args.no_download, verbose=not args.quiet,
    )
    if missing and not args.quiet:
        print(f"⚠️  일봉 없음 {len(missing)}종목: {', '.join(missing[:10])}",
              file=sys.stderr)
    if not prices:
        raise SystemExit("일봉을 하나도 못 받았다. --price-dir 또는 네트워크 확인.")
    return Dataset(sigs, prices, note="실데이터")


def slice_window(
    ds: Dataset, window: int, end: str | None
) -> tuple[Dataset, np.datetime64 | None, np.datetime64 | None]:
    """최근 `window` 거래일로 시그널을 자른다.

    거래일 캘린더 기준이다. 달력 100일로 자르면 주말·휴장이 들어가
    실제로는 70일 남짓만 보게 된다.
    """
    cal = ds.calendar
    if cal.size == 0:
        return ds, None, None
    anchor = np.datetime64(end, "D") if end else cal[-1]
    i = int(np.searchsorted(cal, anchor, side="right")) - 1
    if i < 0:
        raise SystemExit(f"--end {end} 이전의 거래일이 없다.")
    j = max(0, i - window + 1)
    lo, hi = cal[j], cal[i]
    return ds.filter_dates(lo, hi), lo, hi


def build_100d_report(
    *,
    ds: Dataset,
    lo,
    hi,
    window: int,
    cfg: ExitConfig,
    results,
    legacy_results,
    fcfg: FilterConfig,
    decisions,
    results_filtered,
    grid_rows,
    min_n: int,
    synthetic: bool,
    elapsed: float,
) -> str:
    lines: list[str] = []
    a = lines.append
    overall = compute_metrics(results)
    labelled = by_label(results)

    a(f"# 최근 {window} 거래일 청산 백테스트\n")
    if synthetic:
        a(f"> ⚠️ {SYNTHETIC_WARNING}\n")
    a(f"> ⚠️ {WINDOW_CAVEAT}\n")
    a(f"- 구간: **{lo} ~ {hi}** ({window} 거래일)")
    a(f"- 데이터: {ds.describe()} · {ds.note}")
    a(f"- 청산 설정: `{cfg.describe()}`")
    a(f"- 실행 필터: `{fcfg.describe()}`")
    a(f"- 실행 시간: {elapsed:.1f}초")
    a("")

    # --- 0. 판정 ---------------------------------------------------------
    a("## 0. 판정\n")
    a(advise_overall(overall, labelled, min_n=min_n))
    a("")
    if overall.n < min_n:
        a(
            f"> 위 판정은 {window}일 구간 표본({overall.n}건) 기준이다. "
            f"이 구간에서 n ≥ {min_n}을 못 채우는 것은 **정상**이며, "
            f"파라미터 결정은 전 구간 walk-forward가 한다."
        )
        a("")

    # --- 1. 전체 -----------------------------------------------------------
    a("## 1. 전체\n")
    a(metrics_table({f"최근 {window}일": overall}))
    a("")

    # --- 2. 라벨별 (핵심) ---------------------------------------------------
    a("## 2. 라벨별 — 단타 / 중단기 / 스윙\n")
    a("| 라벨 | 채점 지평 | 보유상한 | SL | TP | 이 구간 건수 |")
    a("|---|---:|---:|---|---|---:|")
    for lab in ALL_LABELS:
        le = cfg.by_label.get(lab)
        if le is None:
            continue
        sl = f"{le.sl_value:g}{'%' if le.sl_mode == 'pct' else 'ATR'}"
        tp = f"{le.tp_value:g}{'%' if le.tp_mode == 'pct' else 'ATR'}"
        n = labelled[lab].n if lab in labelled else 0
        a(f"| {lab} | {SCORING_HORIZON_DAYS[lab]}일 | D+{le.hold_days} "
          f"| {sl} | {tp} | {n} |")
    a("")
    a(metrics_table(labelled, first_col="라벨"))
    a("")

    for lab in list(ALL_LABELS) + [k for k in labelled if k not in ALL_LABELS]:
        m = labelled.get(lab)
        if m is None:
            continue
        a(f"### {lab}\n")
        a(diagnose_label(lab, m, min_n=min_n).render())
        a("")
        a("**배리어 청산 vs 단순보유**\n")
        a(counterfactual_table([r for r in results if r.label == lab]))
        a("")

    # --- 3. legacy 대비 -----------------------------------------------------
    if legacy_results:
        a("## 3. legacy 대비 (같은 구간·같은 시그널)\n")
        lm = compute_metrics(legacy_results)
        a(metrics_table({"legacy (D+1 고정%)": lm, "현 설정": overall},
                        first_col="설정"))
        a("")
        ll = by_label(legacy_results)
        merged = {}
        for lab in ALL_LABELS:
            if lab in ll:
                merged[f"{lab} legacy"] = ll[lab]
            if lab in labelled:
                merged[f"{lab} 현설정"] = labelled[lab]
        if merged:
            a("라벨별:\n")
            a(metrics_table(merged, first_col="라벨 × 설정"))
            a("")
        delta = overall.expectancy - lm.expectancy
        if np.isfinite(delta):
            a(
                f"건당 기대값 차이: **{delta:+.2f}%p** "
                f"(legacy {lm.expectancy:+.2f}% → 현 설정 {overall.expectancy:+.2f}%)"
            )
            a("")
            a(
                "> 이 차이는 **이 구간에서만** 관찰된 것이다. 채택 근거로 쓰려면 "
                "전 구간 walk-forward에서 같은 방향이 나와야 한다."
            )
            a("")

    # --- 4. 분해 -----------------------------------------------------------
    a("## 4. 분해\n")
    conf = by_confidence_bucket(results)
    a("### 확신도 구간별\n")
    a(metrics_table(conf, first_col="확신도") if conf else "_확신도 기록 없음._\n")
    a("")
    sec = by_sector(results)
    a("### 섹터별\n")
    a(metrics_table(sec, first_col="섹터") if sec else "_섹터 기록 없음._\n")
    a("")

    # --- 5. 필터 -----------------------------------------------------------
    if decisions:
        a("## 5. 실행 필터\n")
        a(filter_table(decisions))
        a("")
        if results_filtered:
            a(metrics_table(
                {"필터 전": overall, "필터 후": compute_metrics(results_filtered)},
                first_col="구간",
            ))
            a("")

    # --- 6. 그리드 (참고) ----------------------------------------------------
    if grid_rows:
        a("## 6. 이 구간 그리드 (참고용)\n")
        a("> ⚠️ **파라미터 결정 근거로 쓰지 말 것.** 100일 구간 최적값은")
        a("> 그 국면에 맞춘 값이다. 여기서는 전 구간 최적값이 이 구간에서도")
        a("> 상위권에 있는지만 확인한다.\n")
        for lab in ALL_LABELS:
            sub = [r for r in grid_rows if r.label == lab]
            if not sub:
                continue
            a(f"### {lab} — 상위 10셀\n")
            a(grid_top_table(sub, 10))
            a("")

    a("---\n")
    a("※ 분석 보조 자료이며 투자 권유가 아닙니다.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t0 = time.time()

    ds_full = _load(args)
    ds, lo, hi = slice_window(ds_full, args.window, args.end)
    if not args.quiet:
        print(f"구간: {lo} ~ {hi} ({args.window} 거래일)")
        print(f"데이터: {ds.describe()}")
    if not ds.signals:
        raise SystemExit(
            f"{lo} ~ {hi} 구간에 시그널이 0건이다. --window 또는 --end를 확인한다."
        )

    cfg = ExitConfig.legacy() if args.config == "legacy" else ExitConfig.proposed()
    if args.slippage_bps is not None:
        cfg = replace(cfg, slippage_bps=args.slippage_bps)

    results = simulate_all(ds.signals, ds.prices, cfg)
    legacy_results = (
        simulate_all(ds.signals, ds.prices, ExitConfig.legacy())
        if args.compare_legacy and args.config != "legacy" else []
    )

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

    grid_rows = []
    if args.grid:
        spec = GridSpec()
        if not args.quiet:
            print(f"그리드(참고용): 라벨당 {spec.size()}셀")
        grid_rows = run_grid(ds, spec, verbose=not args.quiet)

    elapsed = time.time() - t0
    synthetic = "합성" in ds.note

    md = build_100d_report(
        ds=ds, lo=lo, hi=hi, window=args.window, cfg=cfg, results=results,
        legacy_results=legacy_results, fcfg=fcfg, decisions=decisions,
        results_filtered=results_filtered, grid_rows=grid_rows,
        min_n=args.min_n, synthetic=synthetic, elapsed=elapsed,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    md_path = args.out_dir / f"backtest{tag}.md"
    md_path.write_text(md, encoding="utf-8")

    import pandas as pd

    pd.DataFrame(
        [
            {
                "symbol": r.symbol, "label": r.label, "confidence": r.confidence,
                "sector": r.sector, "signal_date": r.signal_date,
                "entry_date": r.entry_date, "entry_price": r.entry_price,
                "exit_type": r.exit_type, "exit_date": r.exit_date,
                "exit_price": r.exit_price, "return_pct": r.return_pct,
                "hold_days": r.hold_days, "mfe_pct": r.mfe_pct,
                "mae_pct": r.mae_pct, "mfe_hold_pct": r.mfe_hold_pct,
                "capture_ratio": r.capture_ratio,
                "hold_capture_ratio": r.hold_capture_ratio,
                **{f"dplus{k}": v for k, v in sorted(r.dplus.items())},
                "note": r.note,
            }
            for r in results
        ]
    ).to_csv(args.out_dir / f"trades{tag}.csv", index=False)

    if grid_rows:
        to_dataframe(grid_rows).to_csv(args.out_dir / f"grid{tag}.csv", index=False)

    if not args.quiet:
        overall = compute_metrics(results)
        print(f"\n{'=' * 64}")
        print(f"구간      {lo} ~ {hi} ({args.window} 거래일)")
        print(f"시뮬레이션 {overall.n}건 (판정불가 {overall.n_skipped}건)")
        for lab, m in by_label(results).items():
            print(
                f"  {lab:4s} n={m.n:4d}  승률 {m.win_rate:5.1f}%  "
                f"PF {m.profit_factor:5.2f}  건당 {m.expectancy:+6.2f}%"
            )
        print(f"리포트    {md_path}")
        print(f"소요      {elapsed:.1f}s")
        if synthetic:
            print(f"\n⚠️  {SYNTHETIC_WARNING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
