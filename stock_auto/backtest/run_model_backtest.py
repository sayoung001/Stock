"""모델 기반 N일 백테스트 — 기본 1000 거래일.

    python -m stock_auto.backtest.run_model_backtest --days 1000 \
        --price-dir data/cache

## 기존 러너와 무엇이 다른가

| | `run_exit_backtest` / `run_100d` | **`run_model_backtest`** |
|---|---|---|
| 시그널 출처 | `signals.csv`를 **읽는다** | 모델을 돌려 **생성한다** |
| 표본 상한 | 기록된 시그널 이력 (운영 시작 이후) | **가격 데이터 길이** |
| 2주 운영 시 | 24건 | 1000일 × 종목 수 만큼 |

`--window 1000`을 줘도 시그널이 24건뿐이던 이유가 이것이다. 시그널 파일이
2주치면 구간을 아무리 늘려도 그 안에 든 시그널이 전부다. 이 러너는 과거
일봉에 모델을 다시 돌려 "그때 시그널이 났을 것인가"를 재구성하므로,
표본이 가격 데이터가 있는 만큼 늘어난다.

## 반드시 알아야 할 한계

생성된 시그널은 **재현된 모델**의 산출이지 프로덕션이 실제로 낸 시그널이
아니다. 스코어링 원본이 이 저장소에 없어 문서 명세를 보고 다시 구현했고,
가정은 `docs/MODEL_REPRODUCTION.md`에 모아 두었다. 재현도는
`--validate-against signals.csv`로 실측할 수 있다.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from stock_auto.advisor.advisor import advise_overall, diagnose_label
from stock_auto.backtest.dataset import Dataset, load_signals
from stock_auto.backtest.exit_grid import GridSpec, best_per_label, run_grid, to_dataframe
from stock_auto.backtest.report import (
    counterfactual_table,
    filter_table,
    grid_top_table,
    metrics_table,
)
from stock_auto.exit.config import ExitConfig
from stock_auto.exit.engine import simulate_all
from stock_auto.exit.filters import FilterConfig, split_by_filter
from stock_auto.exit.metrics import by_confidence_bucket, by_label, by_sector, compute_metrics
from stock_auto.horizons import ALL_LABELS, SCORING_HORIZON_DAYS
from stock_auto.model.generator import (
    GenerationReport,
    ModelConfig,
    generate_signals,
    to_price_series,
)
from stock_auto.model.indicators import WARMUP_BARS
from stock_auto.model.universe import US_INDICES, load_universe

DEFAULT_DAYS = 1000

MODEL_CAVEAT = (
    "시그널은 **재현된 모델**이 생성한 것이다. 스코어링 원본이 이 저장소에 "
    "없어 문서 명세로 다시 구현했으므로 프로덕션 시그널과 완전히 같지 않다. "
    "가정은 `docs/MODEL_REPRODUCTION.md` 참고."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_model_backtest",
        description="모델로 과거 N일 시그널을 생성해 청산 백테스트 (단타/중단기/스윙 분리)",
    )
    d = p.add_argument_group("데이터")
    d.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"백테스트할 거래일 수 (기본 {DEFAULT_DAYS})")
    d.add_argument("--end", help="구간 종료일 YYYY-MM-DD (기본: 데이터 마지막 날)")
    d.add_argument("--price-dir", type=Path, help="일봉 CSV 캐시 디렉토리")
    d.add_argument("--universe", type=Path,
                   help="유니버스 CSV (symbol,sector). 없으면 내장 추정 목록")
    d.add_argument("--symbols", help="쉼표로 구분한 종목 목록 (유니버스 대신)")
    d.add_argument("--no-download", action="store_true")
    d.add_argument("--synthetic", action="store_true",
                   help="합성 일봉으로 실행 (배선·속도 확인)")
    d.add_argument("--syn-symbols", type=int, default=24)
    d.add_argument("--seed", type=int, default=20260918)

    m = p.add_argument_group("모델")
    m.add_argument("--eff-strong", type=float, default=4.0)
    m.add_argument("--eff-weak", type=float, default=2.5)
    m.add_argument("--min-regime", type=int, default=1)
    m.add_argument("--no-macro-gate", action="store_true")
    m.add_argument("--confidence-proxy", action="store_true",
                   help="Effective_Score에서 확신도를 유도한다 (대용값 — 기본 꺼짐)")
    m.add_argument("--validate-against", type=Path,
                   help="실제 signals.csv와 대조해 재현도를 측정한다")

    e = p.add_argument_group("청산")
    e.add_argument("--config", choices=("legacy", "proposed"), default="proposed")
    e.add_argument("--slippage-bps", type=float, default=None)

    g = p.add_argument_group("그리드")
    g.add_argument("--grid", action="store_true", default=True,
                   help="파라미터 그리드 실행 (기본 켜짐)")
    g.add_argument("--no-grid", dest="grid", action="store_false")
    g.add_argument("--train-frac", type=float, default=0.6)
    g.add_argument("--min-n", type=int, default=100)
    g.add_argument("--with-partial-tp", action="store_true")
    g.add_argument("--with-trail-be", action="store_true")

    f = p.add_argument_group("실행 필터")
    f.add_argument("--no-filters", action="store_true")
    f.add_argument("--min-confidence", type=float, default=0.50)
    f.add_argument("--cooldown-days", type=int, default=3)
    f.add_argument("--max-per-sector", type=int, default=2)

    o = p.add_argument_group("출력")
    o.add_argument("--out-dir", type=Path, default=Path("experiments/results"))
    o.add_argument("--tag", default="model")
    o.add_argument("--save-signals", action="store_true",
                   help="생성한 시그널을 CSV로 저장")
    o.add_argument("--quiet", action="store_true")
    return p


# --- 데이터 로딩 -----------------------------------------------------------
def _frames_from_cache_or_download(
    symbols: list[str], args: argparse.Namespace, need_bars: int
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """종목별 일봉 DataFrame. 캐시 우선, 없으면 다운로드."""
    from stock_auto.backtest.dataset import _from_frame, _download

    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    cache = Path(args.price_dir) if args.price_dir else None

    # 워밍업 + 구간 + 청산 여유를 덮을 만큼 넉넉히 받는다.
    approx_years = need_bars / 252.0 + 1.0
    start = (pd.Timestamp.today() - pd.DateOffset(days=int(approx_years * 400))).strftime("%Y-%m-%d")

    for sym in symbols:
        ps = None
        if cache is not None:
            for name in (f"{sym}.csv", f"{sym.upper()}.csv"):
                fp = cache / name
                if fp.exists():
                    try:
                        ps = _from_frame(sym, pd.read_csv(fp))
                    except Exception as e:                 # noqa: BLE001
                        if not args.quiet:
                            print(f"  [{sym}] 캐시 읽기 실패: {e}", file=sys.stderr)
                    break
        if ps is None and not args.no_download:
            ps = _download(sym, start, None, verbose=not args.quiet)
            if ps is not None and cache is not None:
                cache.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({
                    "date": ps.dates, "open": ps.open, "high": ps.high,
                    "low": ps.low, "close": ps.close,
                }).to_csv(cache / f"{sym}.csv", index=False)

        if ps is None:
            missing.append(sym)
            continue
        frames[sym] = pd.DataFrame(
            {"open": ps.open, "high": ps.high, "low": ps.low, "close": ps.close},
            index=pd.DatetimeIndex(ps.dates),
        )
    return frames, missing


def _ensure_volume(frames: dict[str, pd.DataFrame]) -> list[str]:
    """거래량 컬럼 확인. 없으면 그 종목은 채점할 수 없다.

    Money 전략 3종이 전부 거래량 기반이고, 문서가 "Money 신호 없이는 매수가
    성립하지 않는다"고 못박았다. 거래량 없는 종목을 0으로 채워 돌리면
    Money_Score가 항상 0이 되어 Money Cap에 걸린 반쪽 결과가 나온다.
    조용히 그러느니 명시적으로 빼는 편이 낫다.
    """
    dropped = []
    for sym in list(frames):
        if "volume" not in frames[sym].columns or frames[sym]["volume"].isna().all():
            dropped.append(sym)
            del frames[sym]
    return dropped


def make_synthetic_frames(
    n_symbols: int, n_days: int, seed: int
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """합성 일봉 — 거래량에 군집성을 준다.

    `dataset.make_synthetic_dataset`은 거래량이 iid라 Money 전략이 거의
    발동하지 않는다. 모델 경로를 시험하려면 거래량이 **뭉쳐서** 터져야
    하므로 AR(1) + 스파이크로 만든다. 성과 수치로 읽을 물건은 아니다.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp("2026-09-18"), periods=n_days)
    sectors = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLP", "XLC"]

    frames: dict[str, pd.DataFrame] = {}
    universe: dict[str, str] = {}
    for k in range(n_symbols):
        sym = f"SYN{k:03d}"
        universe[sym] = sectors[k % len(sectors)]
        vol_scale = float(rng.uniform(0.012, 0.030))

        # 수익률에 약한 군집 변동성
        shock = rng.normal(0.0, 1.0, n_days)
        sigma = np.empty(n_days)
        sigma[0] = vol_scale
        for i in range(1, n_days):
            sigma[i] = 0.92 * sigma[i - 1] + 0.08 * vol_scale * (1 + 0.6 * abs(shock[i - 1]))
        ret = shock * sigma + 0.0003
        close = 100.0 * np.exp(np.cumsum(ret))

        gap = rng.normal(0.0, sigma * 0.4)
        open_ = np.empty(n_days)
        open_[0] = close[0] * (1 - ret[0])
        open_[1:] = close[:-1] * np.exp(gap[1:])
        span = np.abs(rng.normal(0.0, sigma * 0.9))
        high = np.maximum(open_, close) * (1 + span)
        low = np.minimum(open_, close) * (1 - span)

        # 거래량: AR(1) 로그 + 가격 급변일 스파이크
        lv = np.empty(n_days)
        lv[0] = 0.0
        for i in range(1, n_days):
            lv[i] = 0.85 * lv[i - 1] + rng.normal(0, 0.35)
        spike = 1.0 + 2.5 * (np.abs(ret) > 2.0 * sigma)
        base = float(rng.uniform(2e6, 4e7))
        volume = base * np.exp(lv) * spike

        frames[sym] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=dates,
        )
    return frames, universe


def slice_frames(
    frames: dict[str, pd.DataFrame], days: int, end: str | None
) -> tuple[pd.Timestamp, pd.Timestamp, pd.DatetimeIndex]:
    """채점 구간(거래일 기준)을 정한다. 가격은 자르지 않는다 —
    워밍업은 구간 **앞쪽** 데이터가, 청산은 **뒤쪽** 데이터가 필요하다."""
    cal = pd.DatetimeIndex(sorted({ts for df in frames.values() for ts in df.index}))
    if cal.empty:
        raise SystemExit("가격 데이터가 비어 있다.")
    anchor = pd.Timestamp(end) if end else cal[-1]
    i = int(cal.searchsorted(anchor, side="right")) - 1
    if i < 0:
        raise SystemExit(f"--end {end} 이전 거래일이 없다.")
    j = max(0, i - days + 1)
    return cal[j], cal[i], cal


def validate_reproduction(
    generated: list, actual_path: Path, lo: pd.Timestamp, hi: pd.Timestamp
) -> str:
    """생성 시그널 vs 실제 기록 시그널 대조.

    재현도를 숫자로 보는 유일한 방법이다. 겹치는 구간에서 (종목, 날짜)
    쌍이 얼마나 일치하는지 센다.
    """
    try:
        actual = load_signals(actual_path)
    except Exception as e:                                # noqa: BLE001
        return f"_대조 실패: {e}_\n"
    if not actual:
        return "_실제 시그널 0건 — 대조 불가._\n"

    a_lo = min(s.signal_date for s in actual)
    a_hi = max(s.signal_date for s in actual)
    lo64, hi64 = np.datetime64(lo.date(), "D"), np.datetime64(hi.date(), "D")
    ov_lo, ov_hi = max(lo64, a_lo), min(hi64, a_hi)
    if ov_lo > ov_hi:
        return (
            f"_겹치는 구간이 없다. 생성 {lo64}~{hi64} vs 실제 {a_lo}~{a_hi}._\n"
        )

    gen_set = {
        (s.symbol, s.signal_date) for s in generated
        if ov_lo <= s.signal_date <= ov_hi
    }
    act_set = {
        (s.symbol, s.signal_date) for s in actual
        if ov_lo <= s.signal_date <= ov_hi
    }
    if not act_set:
        return "_겹치는 구간에 실제 시그널이 없다._\n"

    hit = gen_set & act_set
    recall = len(hit) / len(act_set) * 100.0
    prec = len(hit) / len(gen_set) * 100.0 if gen_set else 0.0
    out = (
        f"대조 구간 **{ov_lo} ~ {ov_hi}**\n\n"
        f"| 항목 | 값 |\n|---|---:|\n"
        f"| 실제 시그널 | {len(act_set)} |\n"
        f"| 생성 시그널 | {len(gen_set)} |\n"
        f"| 일치 (종목·날짜) | {len(hit)} |\n"
        f"| 재현율 (실제 중 잡아낸 비율) | {recall:.1f}% |\n"
        f"| 정밀도 (생성 중 실제인 비율) | {prec:.1f}% |\n\n"
    )
    if recall < 50:
        out += (
            "> ⚠️ 재현율이 낮다. 유니버스·임계값·지표 정의 중 하나가 원본과 "
            "다르다는 뜻이다. `docs/MODEL_REPRODUCTION.md`의 가정을 먼저 점검한다. "
            "이 상태의 백테스트 결과는 **프로덕션 성과의 대용이 아니다.**\n"
        )
    else:
        out += (
            "> 재현율이 이 정도면 구조적 결론(어느 지평이 불리한가, 보유상한을 "
            "늘려야 하는가)은 끌어낼 만하다. 다만 절대 수치는 여전히 대용값이다.\n"
        )
    missed = sorted(act_set - gen_set)[:8]
    if missed:
        out += "\n놓친 실제 시그널 (일부): " + ", ".join(
            f"{sym} {d}" for sym, d in missed
        ) + "\n"
    return out


def build_model_report(
    *, ds, lo, hi, days, mcfg, gen_report, cfg, results, legacy_results,
    fcfg, decisions, results_filtered, grid_rows, min_n, synthetic,
    elapsed, validation, n_symbols, missing,
) -> str:
    lines: list[str] = []
    a = lines.append
    overall = compute_metrics(results)
    labelled = by_label(results)

    a(f"# 모델 기반 {days} 거래일 백테스트\n")
    if synthetic:
        a("> ⚠️ 합성 일봉이다. **성과 수치로 읽으면 안 된다.** 배선·속도 확인 전용.\n")
    a(f"> ⚠️ {MODEL_CAVEAT}\n")
    a(f"- 채점 구간: **{lo.date()} ~ {hi.date()}** ({days} 거래일)")
    a(f"- 유니버스: {n_symbols}종목" + (f" (일봉 없음 {len(missing)}종목)" if missing else ""))
    a(f"- 모델 설정: `{mcfg.describe()}`")
    a(f"- 청산 설정: `{cfg.describe()}`")
    a(f"- 실행 필터: `{fcfg.describe()}`")
    a(f"- 실행 시간: {elapsed:.1f}초")
    a("")

    # --- 0. 시그널 생성 결과 ------------------------------------------------
    a("## 0. 시그널 생성\n")
    a(f"**{gen_report.describe()}**\n")
    if gen_report.macro_note:
        a(f"- 매크로 게이트: {gen_report.macro_note}")
    a(f"- 점수 미달로 제외: {gen_report.n_blocked_score:,}봉")
    a(f"- 레짐 게이트 차단: {gen_report.n_blocked_regime:,}봉")
    a(f"- 매크로 게이트 차단: {gen_report.n_blocked_macro:,}봉")
    if gen_report.skipped:
        a(f"- 워밍업 부족으로 제외한 종목: {', '.join(gen_report.skipped[:8])}")
    a("")
    if gen_report.strategy_hits:
        a("전략별 발동 (시그널 기준, 중복 포함):\n")
        a("| 전략 | 스타일 | 가중치 | 발동 | 비중 |")
        a("|---|---|---:|---:|---:|")
        from stock_auto.model.strategies import STRATEGY_STYLE, WEIGHTS
        tot = max(1, gen_report.n_signals)
        for name, cnt in sorted(
            gen_report.strategy_hits.items(), key=lambda kv: -kv[1]
        ):
            a(f"| {name} | {STRATEGY_STYLE[name]} | {WEIGHTS[name]:g} "
              f"| {cnt:,} | {cnt / tot * 100:.1f}% |")
        a("")

    if validation:
        a("### 재현도 검증 — 실제 시그널과 대조\n")
        a(validation)
        a("")

    # --- 1. 판정 ----------------------------------------------------------
    a("## 1. 판정\n")
    a(advise_overall(overall, labelled, min_n=min_n))
    a("")

    a("## 2. 전체 성과\n")
    a(metrics_table({f"{days}일 전체": overall}))
    a("")
    a("목표 대비 (analysis_2026-09-18 §5):\n")
    for k, ok in overall.meets_targets().items():
        a(f"- {'✅' if ok else '❌'} {k}")
    a("")

    # --- 3. 라벨별 ---------------------------------------------------------
    a("## 3. 라벨별 — 단타 / 중단기 / 스윙\n")
    a("| 라벨 | 채점 지평 | 보유상한 | SL | TP | 건수 |")
    a("|---|---:|---:|---|---|---:|")
    for lab in ALL_LABELS:
        le = cfg.by_label.get(lab)
        if le is None:
            continue
        sl = f"{le.sl_value:g}{'%' if le.sl_mode == 'pct' else 'ATR'}"
        tp = f"{le.tp_value:g}{'%' if le.tp_mode == 'pct' else 'ATR'}"
        n = labelled[lab].n if lab in labelled else 0
        a(f"| {lab} | {SCORING_HORIZON_DAYS[lab]}일 | D+{le.hold_days} | {sl} | {tp} | {n} |")
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

    # --- 4. 그리드 ---------------------------------------------------------
    if grid_rows:
        a("## 4. 파라미터 그리드 (walk-forward)\n")
        a("정렬 기준은 **test 구간 건당 기대값**. train 옆 test가 무너지면 과적합이다.\n")
        best = best_per_label(grid_rows, min_n=min_n)
        for lab in ALL_LABELS:
            sub = [r for r in grid_rows if r.label == lab]
            if not sub:
                continue
            a(f"### {lab} — 상위 10셀\n")
            a(grid_top_table(sub, 10))
            b = best.get(lab)
            if b is None:
                have = max((r.test.n if r.test else r.full.n) for r in sub)
                a("")
                a(f"> ⚠️ **표본 부족** — 검증 구간 최대 {have}건, 기준 n ≥ {min_n} 미달. "
                  f"{min_n - have}건 더 필요. legacy 유지.")
            else:
                te = b.test or b.full
                a("")
                a(f"> **후보**: D+{b.hold_days} · SL {b.sl} · TP {b.tp} · "
                  f"{'종가' if b.close_based else '장중'} → test n={te.n}, "
                  f"기대값 {te.expectancy:+.2f}%, PF {te.profit_factor:.2f}, "
                  f"손익비 {te.payoff_ratio:.2f}")
            a("")

    # --- 5. 분해 -----------------------------------------------------------
    a("## 5. 분해\n")
    conf = by_confidence_bucket(results)
    a("### 확신도 구간별\n")
    if conf and not (len(conf) == 1 and "미기록" in conf):
        a(metrics_table(conf, first_col="확신도"))
    else:
        a("_확신도 미기록 — 프로덕션 conviction은 LLM 산출이라 소급 재현이 안 된다._\n")
        a("_`--confidence-proxy`로 점수 유도 대용값을 쓸 수 있지만 같은 값이 아니다._\n")
    a("")
    a("### 섹터별\n")
    sec = by_sector(results)
    a(metrics_table(sec, first_col="섹터") if sec else "_섹터 기록 없음._\n")
    a("")

    if decisions:
        a("## 6. 실행 필터\n")
        a(filter_table(decisions))
        a("")
        if results_filtered:
            a(metrics_table(
                {"필터 전": overall, "필터 후": compute_metrics(results_filtered)},
                first_col="구간"))
            a("")

    if legacy_results:
        a("## 7. legacy 대비\n")
        lm = compute_metrics(legacy_results)
        a(metrics_table({"legacy (D+1 고정%)": lm, "현 설정": overall}, first_col="설정"))
        a("")
        ll = by_label(legacy_results)
        merged = {}
        for lab in ALL_LABELS:
            if lab in ll:
                merged[f"{lab} legacy"] = ll[lab]
            if lab in labelled:
                merged[f"{lab} 현설정"] = labelled[lab]
        if merged:
            a(metrics_table(merged, first_col="라벨 × 설정"))
            a("")

    a("---\n")
    a("※ 분석 보조 자료이며 투자 권유가 아닙니다.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t0 = time.time()
    need_bars = args.days + WARMUP_BARS + 30

    # --- 1. 일봉 확보 -------------------------------------------------------
    if args.synthetic:
        frames, universe = make_synthetic_frames(
            args.syn_symbols, need_bars + 40, args.seed
        )
        missing: list[str] = []
        index_frames: dict[str, pd.DataFrame] = {}
    else:
        universe = load_universe(args.universe)
        if args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            universe = {s: universe.get(s, "") for s in syms}
        if not args.quiet:
            print(f"유니버스 {len(universe)}종목 · 일봉 {need_bars}봉 필요 — 로딩...")
        frames, missing = _frames_from_cache_or_download(
            list(universe), args, need_bars
        )
        dropped = _ensure_volume(frames)
        if dropped and not args.quiet:
            print(
                f"⚠️  거래량 없음 {len(dropped)}종목 제외: {', '.join(dropped[:10])}\n"
                f"    Money 전략 3종이 전부 거래량 기반이라 채점이 불가능하다.",
                file=sys.stderr,
            )
        index_frames, _ = _frames_from_cache_or_download(
            list(US_INDICES), args, need_bars
        )
        index_frames = {k: v for k, v in index_frames.items() if "volume" in v}

    if not frames:
        raise SystemExit(
            "채점 가능한 일봉이 없다.\n"
            "  · --price-dir 캐시를 지정했는지 확인 (CSV에 volume 컬럼 필요)\n"
            "  · 네트워크가 막힌 환경이면 --synthetic으로 배선만 확인할 수 있다"
        )

    lo, hi, cal = slice_frames(frames, args.days, args.end)
    if not args.quiet:
        print(f"채점 구간: {lo.date()} ~ {hi.date()} ({args.days} 거래일)")

    # --- 2. 시그널 생성 -----------------------------------------------------
    mcfg = ModelConfig(
        eff_strong=args.eff_strong, eff_weak=args.eff_weak,
        min_regime=args.min_regime, use_macro_gate=not args.no_macro_gate,
        confidence_proxy=args.confidence_proxy,
    )
    if not args.quiet:
        print(f"시그널 생성 중... ({mcfg.describe()})")
    sigs, gen_report = generate_signals(
        frames, universe, mcfg,
        index_frames=index_frames or None, verbose=not args.quiet,
    )

    # 채점 구간으로 자른다.
    lo64, hi64 = np.datetime64(lo.date(), "D"), np.datetime64(hi.date(), "D")
    sigs = [s for s in sigs if lo64 <= s.signal_date <= hi64]
    gen_report.n_signals = len(sigs)

    if not sigs:
        raise SystemExit(
            f"{lo.date()} ~ {hi.date()} 구간에 시그널이 0건이다.\n"
            "  · 매크로 게이트가 계속 닫혀 있었을 수 있다 → --no-macro-gate로 확인\n"
            "  · 임계값이 높을 수 있다 → --eff-weak 2.0 등으로 완화해 본다"
        )
    if not args.quiet:
        print(f"  {gen_report.describe()}")

    prices = to_price_series(frames)
    ds = Dataset(sigs, prices, calendar=cal.to_numpy(dtype="datetime64[D]"),
                 note="합성" if args.synthetic else "실데이터")

    # --- 3. 청산 백테스트 ---------------------------------------------------
    cfg = ExitConfig.legacy() if args.config == "legacy" else ExitConfig.proposed()
    if args.slippage_bps is not None:
        cfg = replace(cfg, slippage_bps=args.slippage_bps)

    results = simulate_all(sigs, prices, cfg)
    legacy_results = (
        simulate_all(sigs, prices, ExitConfig.legacy())
        if args.config != "legacy" else []
    )

    fcfg = FilterConfig(
        min_confidence=None if args.no_filters else args.min_confidence,
        cooldown_days=0 if args.no_filters else args.cooldown_days,
        max_per_sector=0 if args.no_filters else args.max_per_sector,
        enabled=not args.no_filters,
    )
    passed, decisions = split_by_filter(
        sigs, fcfg, prior_exits=results, calendar=ds.calendar
    )
    results_filtered = simulate_all(passed, prices, cfg) if passed else []

    # --- 4. 그리드 ---------------------------------------------------------
    grid_rows = []
    if args.grid:
        spec = GridSpec(
            partial_tp=(None, 0.5, 0.6) if args.with_partial_tp else (None,),
            trail_be_atr=(None, 1.0, 1.5) if args.with_trail_be else (None,),
        )
        if not args.quiet:
            print(f"그리드: 라벨당 {spec.size()}셀 · 시그널 {len(sigs):,}건")
        grid_rows = run_grid(
            ds, spec, train_frac=args.train_frac, verbose=not args.quiet
        )

    # --- 5. 재현도 검증 ------------------------------------------------------
    validation = ""
    if args.validate_against:
        validation = validate_reproduction(sigs, args.validate_against, lo, hi)

    # --- 6. 출력 -----------------------------------------------------------
    elapsed = time.time() - t0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""

    md = build_model_report(
        ds=ds, lo=lo, hi=hi, days=args.days, mcfg=mcfg, gen_report=gen_report,
        cfg=cfg, results=results, legacy_results=legacy_results, fcfg=fcfg,
        decisions=decisions, results_filtered=results_filtered,
        grid_rows=grid_rows, min_n=args.min_n, synthetic=args.synthetic,
        elapsed=elapsed, validation=validation, n_symbols=len(frames),
        missing=missing,
    )
    md_path = args.out_dir / f"backtest{tag}.md"
    md_path.write_text(md, encoding="utf-8")

    pd.DataFrame([
        {
            "symbol": r.symbol, "label": r.label, "confidence": r.confidence,
            "sector": r.sector, "source": r.source, "signal_date": r.signal_date,
            "entry_date": r.entry_date, "entry_price": r.entry_price,
            "exit_type": r.exit_type, "exit_date": r.exit_date,
            "exit_price": r.exit_price, "return_pct": r.return_pct,
            "hold_days": r.hold_days, "mfe_pct": r.mfe_pct, "mae_pct": r.mae_pct,
            "mfe_hold_pct": r.mfe_hold_pct, "capture_ratio": r.capture_ratio,
            "hold_capture_ratio": r.hold_capture_ratio,
            **{f"dplus{k}": v for k, v in sorted(r.dplus.items())},
            "note": r.note,
        }
        for r in results
    ]).to_csv(args.out_dir / f"trades{tag}.csv", index=False)

    if args.save_signals:
        pd.DataFrame([
            {"symbol": s.symbol, "signal_date": s.signal_date, "label": s.label,
             "confidence": s.confidence, "sector": s.sector, "source": s.source}
            for s in sigs
        ]).to_csv(args.out_dir / f"signals{tag}.csv", index=False)

    if grid_rows:
        to_dataframe(grid_rows).to_csv(args.out_dir / f"grid{tag}.csv", index=False)

    if not args.quiet:
        overall = compute_metrics(results)
        print(f"\n{'=' * 68}")
        print(f"구간      {lo.date()} ~ {hi.date()} ({args.days} 거래일)")
        print(f"시그널    {len(sigs):,}건 · 시뮬레이션 {overall.n:,}건 "
              f"(판정불가 {overall.n_skipped})")
        for lab, m in by_label(results).items():
            print(f"  {lab:4s} n={m.n:5d}  승률 {m.win_rate:5.1f}%  "
                  f"PF {m.profit_factor:5.2f}  건당 {m.expectancy:+6.2f}%")
        print(f"리포트    {md_path}")
        print(f"소요      {elapsed:.1f}s")
        if args.synthetic:
            print("\n⚠️  합성 일봉 — 성과 수치로 읽으면 안 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
