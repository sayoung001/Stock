"""백테스트 리포트 — 라벨별로 분리된 markdown 출력.

TASK.md Phase 1의 "결과 CSV + 상위 10개 요약 markdown"과 Phase 4의
분해 표를 한 파일에 담는다. 라벨(단타/중단기/스윙)을 **항상 섹션으로
분리**하는 것이 이 리포트의 핵심 규칙이다. 세 라벨을 합쳐서 보면
"평균적으로 괜찮다"는 답만 나오고, 정작 필요한 "어느 지평이 망가졌나"는
안 보인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from stock_auto.advisor.advisor import advise_label, advise_overall
from stock_auto.backtest.exit_grid import GridRow, best_per_label
from stock_auto.exit.config import ExitConfig
from stock_auto.exit.engine import ExitResult
from stock_auto.exit.filters import FilterConfig, FilterDecision
from stock_auto.exit.metrics import (
    Metrics,
    by_confidence_bucket,
    by_label,
    by_sector,
    compute_metrics,
)
from stock_auto.horizons import ALL_LABELS, SCORING_HORIZON_DAYS


def _f(v: float, digits: int = 2, suffix: str = "") -> str:
    """NaN/inf를 표에서 티 나게 보여 준다. 0.00으로 감추면 안 된다."""
    if v is None or not np.isfinite(v):
        return "—"
    return f"{v:.{digits}f}{suffix}"


def _sign(v: float, digits: int = 2) -> str:
    if v is None or not np.isfinite(v):
        return "—"
    return f"{v:+.{digits}f}%"


def metrics_table(rows: dict[str, Metrics], first_col: str = "구분") -> str:
    """지표 표 한 장. 라벨별·구간별·섹터별에 모두 쓴다."""
    head = (
        f"| {first_col} | 건수 | 승률 | PF | 기대값 | 평균익절 | 평균손실 "
        f"| 손익비 | MDD | 평균보유 | MFE20포착 | 보유내포착 | TP/SL/TO |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    body = ""
    for name, m in rows.items():
        body += (
            f"| {name} | {m.n} | {_f(m.win_rate, 1, '%')} | {_f(m.profit_factor)} "
            f"| {_sign(m.expectancy)} | {_sign(m.avg_win)} | {_sign(m.avg_loss)} "
            f"| {_f(m.payoff_ratio)} | {_f(m.max_drawdown, 1, '%p')} "
            f"| {_f(m.avg_hold_days, 1)}일 | {_f(m.capture_ratio * 100, 0, '%')} "
            f"| {_f(m.hold_capture_ratio * 100, 0, '%')} "
            f"| {m.n_tp}/{m.n_sl}/{m.n_to} |\n"
        )
    return head + body


def grid_top_table(rows: Sequence[GridRow], n: int = 10) -> str:
    """상위 n개 셀. train/test를 나란히 보여 과적합을 눈으로 잡게 한다."""
    ranked = sorted(rows, key=lambda r: r.score, reverse=True)[:n]
    head = (
        "| # | 보유 | SL | TP | 판정 | train n/기대값/PF | test n/기대값/PF "
        "| test 승률 | test 손익비 | test MFE포착 |\n"
        "|---:|---:|---|---|---|---|---|---:|---:|---:|\n"
    )
    body = ""
    for i, r in enumerate(ranked, 1):
        tr = r.train
        te = r.test or r.full
        tr_txt = (
            f"{tr.n} / {_sign(tr.expectancy)} / {_f(tr.profit_factor)}"
            if tr else "—"
        )
        te_txt = f"{te.n} / {_sign(te.expectancy)} / {_f(te.profit_factor)}"
        body += (
            f"| {i} | D+{r.hold_days} | {r.sl} | {r.tp} "
            f"| {'종가' if r.close_based else '장중'} | {tr_txt} | {te_txt} "
            f"| {_f(te.win_rate, 1, '%')} | {_f(te.payoff_ratio)} "
            f"| {_f(te.capture_ratio * 100, 0, '%')} |\n"
        )
    return head + body


def counterfactual_table(results: Sequence[ExitResult]) -> str:
    """배리어 청산 vs D+N 단순보유.

    analysis_2026-09-18 §3-1의 반사실 계산을 자동화한 것이다. 배리어가
    단순보유보다 못하면 청산 규칙이 수익을 깎고 있다는 직접 증거다.
    """
    ok = [r for r in results if r.simulated and np.isfinite(r.return_pct)]
    if not ok:
        return "_비교 가능한 건이 없다._\n"
    m = compute_metrics(ok)
    head = (
        "| 청산 방식 | 합계 | 건당 | 승 | 패 |\n|---|---:|---:|---:|---:|\n"
    )
    wins = sum(1 for r in ok if r.return_pct > 0)
    body = (
        f"| **배리어 청산 (현 설정)** | {_sign(m.total_return)} "
        f"| {_sign(m.expectancy)} | {wins} | {len(ok) - wins} |\n"
    )
    for n in sorted({k for r in ok for k in r.dplus}):
        vals = [r.dplus[n] for r in ok if n in r.dplus and np.isfinite(r.dplus[n])]
        if not vals:
            continue
        arr = np.array(vals)
        body += (
            f"| D+{n} 단순보유 | {_sign(arr.sum())} | {_sign(arr.mean())} "
            f"| {int((arr > 0).sum())} | {int((arr <= 0).sum())} |\n"
        )
    return head + body


def filter_table(decisions: Sequence[FilterDecision]) -> str:
    """실행 필터가 무엇을 얼마나 걸렀는지."""
    if not decisions:
        return "_필터 미적용._\n"
    total = len(decisions)
    passed = sum(1 for d in decisions if d.executable)
    counts: dict[str, int] = {}
    for d in decisions:
        for r in d.reasons:
            counts[r] = counts.get(r, 0) + 1
    out = f"전체 {total}건 → 실행 대상 {passed}건 (차단 {total - passed}건)\n\n"
    if counts:
        out += "| 차단 사유 | 건수 |\n|---|---:|\n"
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            out += f"| {k} | {v} |\n"
    return out


def build_report(
    *,
    title: str,
    dataset_desc: str,
    results: Sequence[ExitResult],
    cfg: ExitConfig,
    grid_rows: Sequence[GridRow] = (),
    legacy_results: Sequence[ExitResult] = (),
    decisions: Sequence[FilterDecision] = (),
    filter_cfg: FilterConfig | None = None,
    results_filtered: Sequence[ExitResult] = (),
    min_n: int = 100,
    data_note: str = "",
) -> str:
    """리포트 markdown 전문을 만든다."""
    lines: list[str] = []
    a = lines.append

    a(f"# {title}\n")
    if data_note:
        a(f"> ⚠️ {data_note}\n")
    a(f"- 데이터: {dataset_desc}")
    a(f"- 청산 설정: `{cfg.describe()}`")
    if filter_cfg is not None:
        a(f"- 실행 필터: `{filter_cfg.describe()}`")
    a("")

    overall = compute_metrics(results)
    labelled = by_label(results)

    # --- 0. 한 줄 판정 ----------------------------------------------------
    a("## 0. 판정\n")
    a(advise_overall(overall, labelled, min_n=min_n))
    a("")

    # --- 1. 전체 성과 ------------------------------------------------------
    a("## 1. 전체 성과\n")
    a(metrics_table({"전체": overall}))
    a("")
    a("목표 대비 (analysis_2026-09-18 §5):\n")
    for k, ok in overall.meets_targets().items():
        a(f"- {'✅' if ok else '❌'} {k}")
    a("")

    # --- 2. 라벨별 (이 리포트의 핵심) ---------------------------------------
    a("## 2. 라벨별 — 단타 / 중단기 / 스윙\n")
    a("세 라벨은 채점 지평도 보유상한도 다르다. 합산 지표만 보면 어느 지평이")
    a("망가졌는지 알 수 없으므로 항상 분리해서 본다.\n")
    a("| 라벨 | 채점 지평 | 현 보유상한 | SL | TP |")
    a("|---|---:|---:|---|---|")
    for lab in ALL_LABELS:
        le = cfg.by_label.get(lab)
        if le is None:
            continue
        sl = f"{le.sl_value:g}{'%' if le.sl_mode == 'pct' else 'ATR'}"
        tp = f"{le.tp_value:g}{'%' if le.tp_mode == 'pct' else 'ATR'}"
        a(f"| {lab} | {SCORING_HORIZON_DAYS[lab]}일 | D+{le.hold_days} | {sl} | {tp} |")
    a("")
    a(metrics_table(labelled, first_col="라벨"))
    a("")
    for lab in list(ALL_LABELS) + [k for k in labelled if k not in ALL_LABELS]:
        m = labelled.get(lab)
        if m is None:
            continue
        a(f"### {lab}\n")
        a(advise_label(lab, m, min_n=min_n))
        sub = [r for r in results if r.label == lab]
        a("")
        a("**반사실 비교 — 배리어 청산 vs 단순보유**\n")
        a(counterfactual_table(sub))
        a("")

    # --- 3. 그리드 --------------------------------------------------------
    if grid_rows:
        a("## 3. 파라미터 그리드 (walk-forward)\n")
        a("정렬 기준은 **test 구간 건당 기대값**이다. train에서만 좋고 test에서")
        a("무너지는 셀은 과적합이므로 그 대비를 바로 옆에 붙여 둔다.\n")
        best = best_per_label(grid_rows, min_n=min_n)
        for lab in ALL_LABELS:
            sub = [r for r in grid_rows if r.label == lab]
            if not sub:
                continue
            a(f"### {lab} — 상위 10셀\n")
            a(grid_top_table(sub, 10))
            b = best.get(lab)
            if b is None:
                n_have = max((r.test.n if r.test else r.full.n) for r in sub)
                a("")
                a(
                    f"> ⚠️ **표본 부족 — 최적값을 고르지 않는다.** 검증 구간 최대 "
                    f"{n_have}건으로 기준 n ≥ {min_n}에 미달. {min_n - n_have}건 "
                    f"더 필요. 이 구간에서는 legacy 설정을 유지한다."
                )
            else:
                te = b.test or b.full
                a("")
                a(
                    f"> **후보**: D+{b.hold_days} · SL {b.sl} · TP {b.tp} · "
                    f"{'종가' if b.close_based else '장중'} 판정 "
                    f"→ test n={te.n}, 기대값 {_sign(te.expectancy)}, "
                    f"PF {_f(te.profit_factor)}, 손익비 {_f(te.payoff_ratio)}"
                )
            a("")

    # --- 4. 분해 ----------------------------------------------------------
    a("## 4. 분해\n")
    a("### 확신도 구간별\n")
    conf = by_confidence_bucket(results)
    a(metrics_table(conf, first_col="확신도") if conf else "_확신도 기록 없음._\n")
    a("")
    a("### 섹터별\n")
    sec = by_sector(results)
    a(metrics_table(sec, first_col="섹터") if sec else "_섹터 기록 없음._\n")
    a("")

    # --- 5. 필터 전/후 -----------------------------------------------------
    if decisions:
        a("## 5. 실행 필터 전 / 후\n")
        a(filter_table(decisions))
        a("")
        if results_filtered:
            a(metrics_table(
                {"필터 전": overall, "필터 후": compute_metrics(results_filtered)},
                first_col="구간",
            ))
            a("")

    # --- 6. legacy 대비 ----------------------------------------------------
    if legacy_results:
        a("## 6. legacy 대비\n")
        a("**주의**: 이 표는 설정 변경의 방향을 보는 용도이고, 근거는 3장의")
        a("walk-forward 결과다. 여기 숫자가 좋아졌다는 것만으로 채택하면 안 된다.\n")
        lm = compute_metrics(legacy_results)
        a(metrics_table({"legacy (D+1 고정%)": lm, "현 설정": overall}, first_col="설정"))
        a("")
        a("라벨별:\n")
        ll = by_label(legacy_results)
        merged = {}
        for lab in ALL_LABELS:
            if lab in ll:
                merged[f"{lab} legacy"] = ll[lab]
            if lab in labelled:
                merged[f"{lab} 현설정"] = labelled[lab]
        a(metrics_table(merged, first_col="라벨 × 설정"))
        a("")

    a("---\n")
    a("※ 분석 보조 자료이며 투자 권유가 아닙니다.")
    return "\n".join(lines) + "\n"
