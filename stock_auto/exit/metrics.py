"""성과 지표 — 청산 결과 묶음을 숫자 한 줄로 요약한다.

analysis_2026-09-18 §5의 성공 기준(PF ≥ 1.3, 실효 손익비 ≥ 1.5,
건당 기대값 > +0.5%, MFE 포착률 ≥ 50%)을 그대로 계산 대상으로 삼는다.
그리드가 고를 기준이자 완료 판정의 근거다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np

from stock_auto.exit.engine import (
    DPLUS_HORIZONS,
    EXIT_NONE,
    EXIT_SL,
    EXIT_TO,
    EXIT_TP,
    ExitResult,
)


@dataclass(slots=True)
class Metrics:
    """청산 결과 묶음의 요약. 모든 수익률 단위는 %."""

    n: int                      # 시뮬레이션 성공 건수
    n_skipped: int              # 데이터 부족 등으로 판정 못 한 건수
    win_rate: float
    total_return: float         # 단순 합 (종목당 균등 $1,000 가정과 정합)
    expectancy: float           # 건당 기대값
    profit_factor: float
    avg_win: float
    avg_loss: float             # 음수로 돌려준다
    payoff_ratio: float         # 실효 손익비 = |avg_win / avg_loss|
    max_drawdown: float         # 시간순 누적곡선의 최대낙폭 (%p)
    avg_hold_days: float
    capture_ratio: float        # MFE 포착률 = mean(실현) / mean(MFE20)
    hold_capture_ratio: float   # 보유구간 내 포착률 = mean(실현) / mean(보유구간 MFE)
    n_tp: int
    n_sl: int
    n_to: int
    dplus_total: dict[int, float]   # D+N 단순보유 반사실 합계

    def as_row(self) -> dict[str, object]:
        """CSV 한 줄로. dplus는 컬럼으로 펼친다."""
        row = asdict(self)
        dp = row.pop("dplus_total")
        for n, v in sorted(dp.items()):
            row[f"dplus{n}_total"] = v
        return row

    def meets_targets(self) -> dict[str, bool]:
        """analysis_2026-09-18 §5 목표 대비 통과 여부."""
        return {
            "PF≥1.3": self.profit_factor >= 1.3,
            "손익비≥1.5": self.payoff_ratio >= 1.5,
            "기대값>+0.5%": self.expectancy > 0.5,
            "MFE포착≥50%": self.capture_ratio >= 0.5,
        }


def _max_drawdown(returns_in_time_order: np.ndarray) -> float:
    """누적 수익률 곡선의 최대낙폭(%p). 산술 누적 — 균등 사이징 가정과 맞다."""
    if returns_in_time_order.size == 0:
        return 0.0
    equity = np.cumsum(returns_in_time_order)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    return float(dd.min())


def compute_metrics(results: Sequence[ExitResult]) -> Metrics:
    """청산 결과 묶음 → 지표. 판정 실패 건은 분모에서 빼고 따로 센다."""
    ok = [r for r in results if r.simulated and np.isfinite(r.return_pct)]
    n_skipped = len(results) - len(ok)

    empty_dplus = {n: 0.0 for n in DPLUS_HORIZONS}
    if not ok:
        return Metrics(
            n=0, n_skipped=n_skipped, win_rate=float("nan"), total_return=0.0,
            expectancy=float("nan"), profit_factor=float("nan"),
            avg_win=float("nan"), avg_loss=float("nan"),
            payoff_ratio=float("nan"), max_drawdown=0.0,
            avg_hold_days=float("nan"), capture_ratio=float("nan"),
            hold_capture_ratio=float("nan"),
            n_tp=0, n_sl=0, n_to=0, dplus_total=empty_dplus,
        )

    # 시간순 정렬 — MDD는 순서에 의존한다.
    ok_sorted = sorted(ok, key=lambda r: (r.entry_date or r.signal_date))
    rets = np.array([r.return_pct for r in ok_sorted], dtype=np.float64)

    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())

    avg_win = float(wins.mean()) if wins.size else float("nan")
    avg_loss = float(losses.mean()) if losses.size else float("nan")
    payoff = (
        abs(avg_win / avg_loss)
        if wins.size and losses.size and avg_loss != 0
        else float("inf") if wins.size and not losses.size
        else float("nan")
    )
    pf = (
        gross_win / gross_loss if gross_loss > 0
        else float("inf") if gross_win > 0
        else float("nan")
    )

    # MFE 포착률은 건별 비율의 평균이 아니라 **평균의 비율**로 낸다.
    # 건별 비율은 MFE가 0에 가까운 건에서 폭발해 평균이 망가진다.
    mfes = np.array(
        [r.mfe_pct for r in ok if np.isfinite(r.mfe_pct) and r.mfe_pct > 0],
        dtype=np.float64,
    )
    mfe_rets = np.array(
        [r.return_pct for r in ok if np.isfinite(r.mfe_pct) and r.mfe_pct > 0],
        dtype=np.float64,
    )
    capture = (
        float(mfe_rets.mean() / mfes.mean()) if mfes.size and mfes.mean() > 0
        else float("nan")
    )

    # 보유 구간 기준 포착률 — 같은 방식(평균의 비율)으로 낸다.
    hmfes = np.array(
        [r.mfe_hold_pct for r in ok
         if np.isfinite(r.mfe_hold_pct) and r.mfe_hold_pct > 0],
        dtype=np.float64,
    )
    hmfe_rets = np.array(
        [r.return_pct for r in ok
         if np.isfinite(r.mfe_hold_pct) and r.mfe_hold_pct > 0],
        dtype=np.float64,
    )
    hold_capture = (
        float(hmfe_rets.mean() / hmfes.mean()) if hmfes.size and hmfes.mean() > 0
        else float("nan")
    )

    dplus_total = {}
    for n in DPLUS_HORIZONS:
        vals = [r.dplus[n] for r in ok if n in r.dplus and np.isfinite(r.dplus[n])]
        dplus_total[n] = float(np.sum(vals)) if vals else 0.0

    return Metrics(
        n=len(ok),
        n_skipped=n_skipped,
        win_rate=float(wins.size / len(ok) * 100.0),
        total_return=float(rets.sum()),
        expectancy=float(rets.mean()),
        profit_factor=pf,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff,
        max_drawdown=_max_drawdown(rets),
        avg_hold_days=float(np.mean([r.hold_days for r in ok])),
        capture_ratio=capture,
        hold_capture_ratio=hold_capture,
        n_tp=sum(1 for r in ok if r.exit_type.startswith(EXIT_TP)),
        n_sl=sum(1 for r in ok if r.exit_type == EXIT_SL),
        n_to=sum(1 for r in ok if r.exit_type.endswith(EXIT_TO)),
        dplus_total=dplus_total,
    )


def by_label(results: Sequence[ExitResult]) -> dict[str, Metrics]:
    """라벨(단타/중단기/스윙)별 분해. TASK.md Phase 1 마지막 항목."""
    from stock_auto.horizons import ALL_LABELS, UNKNOWN

    groups: dict[str, list[ExitResult]] = {lab: [] for lab in ALL_LABELS}
    for r in results:
        groups.setdefault(r.label, []).append(r)
    # 값이 있는 버킷만, 표준 순서대로.
    order = list(ALL_LABELS) + [k for k in groups if k not in ALL_LABELS]
    return {k: compute_metrics(groups[k]) for k in order if groups.get(k)}


def by_confidence_bucket(results: Sequence[ExitResult]) -> dict[str, Metrics]:
    """확신도 구간별 분해 (0.45 / 0.50 / 0.55 / 0.60+).

    analysis_2026-09-18 §3-3이 "구간 간 승률 차이 ≥ 15%p"를 목표로 잡은
    그 표다. 지금은 차이가 없다는 것이 발견 사항이었다.
    """
    edges = [(-np.inf, 0.50, "<0.50"), (0.50, 0.55, "0.50~0.55"),
             (0.55, 0.60, "0.55~0.60"), (0.60, np.inf, "0.60+")]
    groups: dict[str, list[ExitResult]] = {lab: [] for _, _, lab in edges}
    unknown: list[ExitResult] = []
    for r in results:
        if not np.isfinite(r.confidence):
            unknown.append(r)
            continue
        for lo, hi, lab in edges:
            if lo <= r.confidence < hi:
                groups[lab].append(r)
                break
    out = {lab: compute_metrics(groups[lab]) for _, _, lab in edges if groups[lab]}
    if unknown:
        out["미기록"] = compute_metrics(unknown)
    return out


def by_sector(results: Sequence[ExitResult]) -> dict[str, Metrics]:
    """섹터별 분해. 쏠림이 성과에 어떻게 나타나는지 본다."""
    groups: dict[str, list[ExitResult]] = {}
    for r in results:
        groups.setdefault(r.sector or "미기록", []).append(r)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}
