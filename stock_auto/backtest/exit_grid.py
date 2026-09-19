"""청산 파라미터 그리드 + walk-forward 검증 (TASK.md Phase 1).

여기서 나오는 표가 `ExitConfig` 기본값을 결정하는 근거다. 12건에 맞춰
손으로 고르지 않기 위해 만든 도구이고, 그래서 두 가지를 강제한다:

1. **라벨별 분리** — 단타/중단기/스윙은 각자의 최적점이 다르다. 하나의
   보유상한을 셋에 같이 씌우는 게 애초의 설계 모순이었으므로, 그리드도
   라벨별로 따로 돈다.
2. **walk-forward** — 앞 60%에서 고른 파라미터를 뒤 40%에서 검증한다.
   전 구간 최적값은 반드시 좋아 보이기 때문에 그 숫자만으로는 아무것도
   알 수 없다. 과거 XGBoost 오버피팅 사건이 정확히 이것이었다.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

import numpy as np

from stock_auto.backtest.dataset import Dataset
from stock_auto.exit.config import BarrierMode, ExitConfig, LabelExit
from stock_auto.exit.engine import Signal, simulate_all
from stock_auto.exit.metrics import Metrics, compute_metrics
from stock_auto.horizons import ALL_LABELS, normalize_label

#: TASK.md Phase 1이 지정한 기본 축. (mode, value) 쌍으로 표현한다.
DEFAULT_HOLDS: tuple[int, ...] = (1, 2, 3, 5)
DEFAULT_SLS: tuple[tuple[BarrierMode, float], ...] = (
    ("atr", 1.0), ("atr", 1.5), ("atr", 2.0), ("pct", 2.0), ("pct", 3.0),
)
DEFAULT_TPS: tuple[tuple[BarrierMode, float], ...] = (
    ("atr", 2.0), ("atr", 2.5), ("atr", 3.0), ("pct", 3.0), ("pct", 5.0),
)
DEFAULT_CLOSE_BASED: tuple[bool, ...] = (False, True)


@dataclass(frozen=True, slots=True)
class GridSpec:
    """탐색 축. 기본값이 TASK.md Phase 1의 그리드다."""

    holds: tuple[int, ...] = DEFAULT_HOLDS
    sls: tuple[tuple[BarrierMode, float], ...] = DEFAULT_SLS
    tps: tuple[tuple[BarrierMode, float], ...] = DEFAULT_TPS
    close_based: tuple[bool, ...] = DEFAULT_CLOSE_BASED
    trail_be_atr: tuple[float | None, ...] = (None,)
    partial_tp: tuple[float | None, ...] = (None,)
    slippage_bps: float = 5.0
    gap_aware_fill: bool = True

    def size(self) -> int:
        """셀 개수. 실행 시간 예측의 분모다."""
        return (
            len(self.holds) * len(self.sls) * len(self.tps)
            * len(self.close_based) * len(self.trail_be_atr) * len(self.partial_tp)
        )

    def cells(self) -> Iterable[tuple]:
        return itertools.product(
            self.holds, self.sls, self.tps, self.close_based,
            self.trail_be_atr, self.partial_tp,
        )

    def config_for(self, cell: tuple) -> ExitConfig:
        """그리드 셀 하나 → ExitConfig (세 라벨에 동일 적용)."""
        hold, (slm, slv), (tpm, tpv), cb, trail, ptp = cell
        return ExitConfig.uniform(
            hold_days=hold, sl_mode=slm, sl_value=slv,
            tp_mode=tpm, tp_value=tpv,
            close_based=cb, trail_be_atr=trail, partial_tp=ptp,
            slippage_bps=self.slippage_bps, gap_aware_fill=self.gap_aware_fill,
        )


@dataclass(slots=True)
class GridRow:
    """그리드 한 셀의 결과. train/test를 나란히 들고 있어야 과적합이 보인다."""

    label: str
    hold_days: int
    sl: str
    tp: str
    close_based: bool
    trail_be_atr: float | None
    partial_tp: float | None
    full: Metrics
    train: Metrics | None = None
    test: Metrics | None = None

    def as_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "label": self.label,
            "hold_days": self.hold_days,
            "sl": self.sl,
            "tp": self.tp,
            "close_based": self.close_based,
            "trail_be_atr": self.trail_be_atr,
            "partial_tp": self.partial_tp,
        }
        for prefix, m in (("", self.full), ("tr_", self.train), ("te_", self.test)):
            if m is None:
                continue
            for k, v in m.as_row().items():
                row[f"{prefix}{k}"] = v
        return row

    @property
    def score(self) -> float:
        """정렬 기준.

        **test 구간의 건당 기대값**을 쓴다. PF는 표본이 적을 때 분모가
        작아져 쉽게 폭발하고, 전 구간(full) 값은 과적합을 감추기 때문이다.
        test가 없으면(walk-forward 미실시) full로 떨어진다.
        """
        m = self.test or self.full
        if m.n == 0 or not np.isfinite(m.expectancy):
            return float("-inf")
        return m.expectancy


def walk_forward_split(
    signals: Sequence[Signal], train_frac: float = 0.6
) -> tuple[np.datetime64 | None, list[Signal], list[Signal]]:
    """시간 순 60/40 분할.

    건수가 아니라 **날짜**로 자른다. 같은 날짜의 시그널이 train과 test로
    쪼개지면 사실상 같은 시장 상황을 양쪽에서 보게 되어 검증이 무의미해진다.
    """
    if not signals:
        return None, [], []
    dates = np.array(sorted({s.signal_date for s in signals}))
    if dates.size < 2:
        return None, list(signals), []
    cut_i = max(1, min(dates.size - 1, int(round(dates.size * train_frac))))
    cut = dates[cut_i]
    train = [s for s in signals if s.signal_date < cut]
    test = [s for s in signals if s.signal_date >= cut]
    return cut, train, test


def run_grid(
    ds: Dataset,
    spec: GridSpec | None = None,
    *,
    labels: Sequence[str] = ALL_LABELS,
    train_frac: float = 0.6,
    walk_forward: bool = True,
    verbose: bool = True,
) -> list[GridRow]:
    """라벨별로 그리드를 돌린다.

    Returns:
        모든 (라벨 × 셀) 결과. 정렬은 호출자가 한다.
    """
    spec = spec or GridSpec()
    rows: list[GridRow] = []

    # 라벨별로 시그널을 미리 갈라 둔다. 각 셀에서 다시 필터링하면
    # 셀 수만큼 반복 비용이 붙는다.
    by_label: dict[str, list[Signal]] = {normalize_label(l): [] for l in labels}
    for s in ds.signals:
        key = normalize_label(s.label)
        if key in by_label:
            by_label[key].append(s)

    total = spec.size() * sum(1 for v in by_label.values() if v)
    done = 0
    t0 = time.time()

    for label in labels:
        key = normalize_label(label)
        sigs = by_label.get(key, [])
        if not sigs:
            if verbose:
                print(f"  [{label}] 시그널 0건 — 건너뜀")
            continue

        cut, train_sigs, test_sigs = (
            walk_forward_split(sigs, train_frac) if walk_forward else (None, [], [])
        )
        if verbose:
            wf = (
                f" · walk-forward 분기 {cut} (train {len(train_sigs)} / test {len(test_sigs)})"
                if cut is not None else " · walk-forward 없음"
            )
            print(f"  [{label}] 시그널 {len(sigs)}건 · 셀 {spec.size()}개{wf}")

        for cell in spec.cells():
            cfg = spec.config_for(cell)
            hold, (slm, slv), (tpm, tpv), cb, trail, ptp = cell

            full = compute_metrics(simulate_all(sigs, ds.prices, cfg))
            tr = compute_metrics(simulate_all(train_sigs, ds.prices, cfg)) if train_sigs else None
            te = compute_metrics(simulate_all(test_sigs, ds.prices, cfg)) if test_sigs else None

            rows.append(
                GridRow(
                    label=key, hold_days=hold,
                    sl=f"{slv:g}{'%' if slm == 'pct' else 'ATR'}",
                    tp=f"{tpv:g}{'%' if tpm == 'pct' else 'ATR'}",
                    close_based=cb, trail_be_atr=trail, partial_tp=ptp,
                    full=full, train=tr, test=te,
                )
            )
            done += 1
            if verbose and total and done % max(1, total // 10) == 0:
                el = time.time() - t0
                print(f"    {done}/{total} ({done / total:.0%}) · {el:.1f}s")

    if verbose:
        print(f"  그리드 완료: {done}셀 · {time.time() - t0:.1f}s")
    return rows


def best_per_label(
    rows: Sequence[GridRow], *, min_n: int = 100
) -> dict[str, GridRow | None]:
    """라벨별 최적 셀.

    ``min_n``은 TASK.md 완료 기준의 "n ≥ 100"이다. 이를 못 채우면
    **최적값을 고르지 않고 None**을 돌려준다 — 표본이 부족할 때 1등을
    집어 주면 그게 곧 과적합이다. 판단은 리포트가 문장으로 남긴다.
    """
    out: dict[str, GridRow | None] = {}
    for label in {r.label for r in rows}:
        cand = [r for r in rows if r.label == label]
        eligible = [
            r for r in cand
            if (r.test.n if r.test else r.full.n) >= min_n
        ]
        out[label] = max(eligible, key=lambda r: r.score) if eligible else None
    return out


def to_dataframe(rows: Sequence[GridRow]):
    """그리드 결과 → pandas DataFrame (CSV 저장용)."""
    import pandas as pd

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r.as_row() for r in rows])
    sort_key = "te_expectancy" if "te_expectancy" in df.columns else "expectancy"
    return df.sort_values(["label", sort_key], ascending=[True, False])
