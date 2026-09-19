"""시그널 생성기 — 과거 전 구간에서 "그때 시그널이 났을 것인가"를 재구성한다.

이 모듈이 있어야 백테스트 표본이 **기록된 시그널 이력**이 아니라
**가격 데이터 길이**만큼 커진다. 1000일 백테스트가 가능해지는 지점이다.

흐름:

```
일봉 (종목별)
   ↓ indicators.calculate_indicators
지표 51종
   ↓ strategies.apply_strategies
Money 3 + Price 7 발동 여부 → Money/Price/Effective Score
   ↓ regime.stock_regime + regime.macro_gate
레짐 게이트 AND 매크로 게이트
   ↓ strategies.final_buy
매수 시그널
   ↓ strategies.assign_label
단타 / 중단기 / 스윙 라벨
   ↓
Signal 목록 → 기존 청산 엔진으로
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from stock_auto.exit.engine import PriceSeries, Signal
from stock_auto.model.indicators import WARMUP_BARS, calculate_indicators
from stock_auto.model.regime import macro_gate, stock_regime, synthetic_index
from stock_auto.model.strategies import (
    ALL_STRATEGIES,
    active_strategies,
    apply_strategies,
    assign_label,
    final_buy,
)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """스코어링 모델 파라미터.

    기본값은 `현재_적용_로직_전수.md` §2.7의 프로덕션 임계값이다.
    완화해 보고 싶으면 `eff_strong`/`eff_weak`를 낮추면 되는데, 그건
    별개의 실험이므로 리포트에 설정을 같이 찍는다.
    """

    market: str = "US"
    eff_strong: float = 4.0
    eff_weak: float = 2.5
    money_min: float = 1.5
    liq_min: float = 0.7
    min_regime: int = 1
    use_macro_gate: bool = True

    #: 확신도(conviction)를 Effective_Score에서 유도할지.
    #: 기본 False — 프로덕션 확신도는 LLM이 낸 값이고 소급 재현이 불가능하다.
    #: 없는 값을 지어내면 확신도 필터가 **가짜 숫자로** 작동하게 된다.
    confidence_proxy: bool = False

    def describe(self) -> str:
        parts = [
            f"Eff≥{self.eff_strong:g} 또는 (Eff≥{self.eff_weak:g} "
            f"& Money≥{self.money_min:g} & 유동성≥{self.liq_min:g})",
            f"레짐≥{self.min_regime}",
            "매크로게이트 ON" if self.use_macro_gate else "매크로게이트 OFF",
        ]
        if self.confidence_proxy:
            parts.append("확신도=점수유도(대용)")
        return " · ".join(parts)


@dataclass(slots=True)
class GenerationReport:
    """생성 과정 요약. 시그널이 왜 이 정도 나왔는지 설명하는 데 쓴다."""

    n_symbols: int = 0
    n_bars_scored: int = 0
    n_signals: int = 0
    n_blocked_macro: int = 0
    n_blocked_regime: int = 0
    n_blocked_score: int = 0
    strategy_hits: dict[str, int] = field(default_factory=dict)
    label_counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    macro_note: str = ""

    def describe(self) -> str:
        rate = (
            f"{self.n_signals / self.n_bars_scored * 100:.2f}%"
            if self.n_bars_scored else "—"
        )
        return (
            f"{self.n_symbols}종목 · 채점 {self.n_bars_scored:,}봉 → "
            f"시그널 {self.n_signals:,}건 (발동률 {rate})"
        )


def _confidence_from_score(eff: float) -> float:
    """Effective_Score → 0~1 대용 확신도.

    **프로덕션 conviction과 다른 값이다.** 프로덕션은 LLM이 뉴스·수급·밸류를
    보고 낸 숫자이고, 이건 기술 점수만의 단조 변환이다. 실제 관측 범위
    (0.45~0.58)와 비슷한 대역에 오도록 Eff 2.5~7.0을 0.45~0.75에 선형 대응시켰다.
    """
    if not np.isfinite(eff):
        return float("nan")
    return float(np.clip(0.45 + (eff - 2.5) * (0.30 / 4.5), 0.30, 0.95))


def generate_signals_for_symbol(
    symbol: str,
    df: pd.DataFrame,
    cfg: ModelConfig,
    *,
    sector: str = "",
    macro_ok: pd.Series | None = None,
    report: GenerationReport | None = None,
) -> list[Signal]:
    """종목 하나의 일봉에서 시그널을 뽑는다."""
    if len(df) < WARMUP_BARS:
        if report is not None:
            report.skipped.append(f"{symbol}(봉 {len(df)} < 워밍업 {WARMUP_BARS})")
        return []

    ind = calculate_indicators(df, market=cfg.market)
    scored = apply_strategies(ind)
    regime = stock_regime(ind)

    buy = final_buy(
        scored,
        regime=regime,
        macro_ok=macro_ok if cfg.use_macro_gate else None,
        eff_strong=cfg.eff_strong,
        eff_weak=cfg.eff_weak,
        money_min=cfg.money_min,
        liq_min=cfg.liq_min,
        min_regime=cfg.min_regime,
    )

    # 워밍업 구간은 채점 대상에서 제외한다. 지표가 덜 익은 채로 나온
    # 시그널은 신호가 아니라 잡음이다.
    valid = np.zeros(len(scored), dtype=bool)
    valid[WARMUP_BARS:] = True
    buy = buy & valid

    if report is not None:
        report.n_bars_scored += int(valid.sum())
        # 차단 사유별 집계 — 점수는 통과했는데 게이트에서 막힌 건을 센다.
        eff = scored["Effective_Score"]
        score_ok = (eff >= cfg.eff_strong) | (
            (eff >= cfg.eff_weak)
            & (scored["Money_Score"] >= cfg.money_min)
            & (scored["liquidity_score"] >= cfg.liq_min)
        )
        score_ok = (score_ok & valid).fillna(False)
        report.n_blocked_score += int((~score_ok & valid).sum())
        report.n_blocked_regime += int((score_ok & (regime < cfg.min_regime)).sum())
        if macro_ok is not None and cfg.use_macro_gate:
            m = macro_ok.reindex(scored.index).fillna(True)
            report.n_blocked_regime -= 0
            report.n_blocked_macro += int(
                (score_ok & (regime >= cfg.min_regime) & ~m).sum()
            )

    hits = scored.loc[buy.to_numpy()]
    out: list[Signal] = []
    for ts, row in hits.iterrows():
        label = assign_label(row)
        conf = (
            _confidence_from_score(float(row["Effective_Score"]))
            if cfg.confidence_proxy else float("nan")
        )
        strat = active_strategies(row)
        out.append(
            Signal(
                symbol=symbol,
                signal_date=np.datetime64(pd.Timestamp(ts).date(), "D"),
                label=label,
                confidence=conf,
                sector=sector,
                source="model:" + ",".join(strat) if strat else "model",
            )
        )
        if report is not None:
            report.label_counts[label] = report.label_counts.get(label, 0) + 1
            for s in strat:
                report.strategy_hits[s] = report.strategy_hits.get(s, 0) + 1

    return out


def generate_signals(
    prices: dict[str, pd.DataFrame],
    universe: dict[str, str],
    cfg: ModelConfig | None = None,
    *,
    index_frames: dict[str, pd.DataFrame] | None = None,
    verbose: bool = True,
) -> tuple[list[Signal], GenerationReport]:
    """유니버스 전체에서 시그널을 생성한다.

    Args:
        prices: {종목: 일봉 DataFrame(open/high/low/close/volume)}
        universe: {종목: 섹터}
        index_frames: 매크로 게이트용 지수 일봉. 없으면 합성지수로 폴백.

    Returns:
        (시그널 목록, 생성 리포트)
    """
    cfg = cfg or ModelConfig()
    report = GenerationReport(n_symbols=len(prices))

    # --- 매크로 게이트 -----------------------------------------------------
    calendar = pd.DatetimeIndex(
        sorted({ts for df in prices.values() for ts in df.index})
    )
    macro_ok: pd.Series | None = None
    if cfg.use_macro_gate:
        frames = dict(index_frames or {})
        if frames:
            report.macro_note = f"지수 {', '.join(sorted(frames))}"
        else:
            try:
                frames = {"SYNTHETIC": synthetic_index(prices)}
                report.macro_note = (
                    "⚠️ 실제 지수 일봉이 없어 **유니버스 동일가중 합성지수**로 "
                    "대체했다. 유니버스가 편향돼 있으면 게이트가 지수 대용이 못 된다"
                )
            except ValueError:
                report.macro_note = "⚠️ 매크로 게이트 비활성 (지수·합성 둘 다 실패)"
        if frames:
            macro_ok, _ = macro_gate(frames, calendar)

    # --- 종목별 생성 -------------------------------------------------------
    all_sigs: list[Signal] = []
    for i, (sym, df) in enumerate(sorted(prices.items()), 1):
        sigs = generate_signals_for_symbol(
            sym, df, cfg,
            sector=universe.get(sym, ""),
            macro_ok=macro_ok,
            report=report,
        )
        all_sigs.extend(sigs)
        if verbose and (i % 10 == 0 or i == len(prices)):
            print(f"    {i}/{len(prices)}종목 · 누적 시그널 {len(all_sigs):,}건")

    all_sigs.sort(key=lambda s: (s.signal_date, s.symbol))
    report.n_signals = len(all_sigs)
    return all_sigs, report


def to_price_series(prices: dict[str, pd.DataFrame]) -> dict[str, PriceSeries]:
    """청산 엔진이 쓰는 PriceSeries로 변환."""
    out: dict[str, PriceSeries] = {}
    for sym, df in prices.items():
        d = df.dropna(subset=["open", "high", "low", "close"])
        if d.empty:
            continue
        out[sym] = PriceSeries(
            symbol=sym,
            dates=d.index.normalize().to_numpy(dtype="datetime64[D]"),
            open=d["open"].to_numpy(dtype=np.float64),
            high=d["high"].to_numpy(dtype=np.float64),
            low=d["low"].to_numpy(dtype=np.float64),
            close=d["close"].to_numpy(dtype=np.float64),
        )
    return out
