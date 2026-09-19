"""실행 필터 (TASK.md Phase 2).

세 필터 모두 **시그널을 지우지 않는다.** 추천 목록에는 남기고
`실행 대상 아님` 플래그만 붙인다. 지워 버리면 "그 필터가 옳았나"를
나중에 물어볼 수 없다 — 차단분도 라벨링해서 성과를 비교해야
게이트를 완화할지 조일지 판단할 수 있다(추천_읽는_법 §1-⑨과 같은 원칙).

필터는 **시간 순서대로** 평가된다. 쿨다운은 "이 시그널 이전에 일어난
SL 청산"만 볼 수 있어야 하므로 미래를 참조하지 않게 조심해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from stock_auto.exit.engine import EXIT_SL, ExitResult, Signal

#: 제외 사유 코드. 대시보드의 회색 pill 문구와 1:1 대응한다.
REASON_CONFIDENCE = "제외: 확신도"
REASON_COOLDOWN = "제외: 쿨다운"
REASON_SECTOR = "제외: 섹터한도"


@dataclass(frozen=True, slots=True)
class FilterConfig:
    """실행 필터 파라미터.

    기본값은 analysis_2026-09-18 §4 P1의 권고치다. `min_confidence`만은
    n=12에서 나온 관찰(0.50 미만 4건 전패)이라 근거가 약하지만, 통과분이
    손실이 아니었으므로 "지금 적용해도 잃을 게 없는" 쪽에 속한다.
    """

    min_confidence: float | None = 0.50
    cooldown_days: int = 3          # SL 청산 후 같은 종목 재추천 차단 (거래일)
    max_per_sector: int = 2         # 동시 보유 중 같은 섹터 상한
    enabled: bool = True

    def describe(self) -> str:
        if not self.enabled:
            return "실행 필터 없음"
        mc = "없음" if self.min_confidence is None else f"{self.min_confidence:.2f}"
        return (
            f"확신도≥{mc} · SL후 쿨다운 {self.cooldown_days}거래일 "
            f"· 섹터당 최대 {self.max_per_sector}"
        )


@dataclass(slots=True)
class FilterDecision:
    """시그널 한 건의 실행 여부 판정."""

    signal: Signal
    executable: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def pill(self) -> str:
        """대시보드 표기용. 통과면 빈 문자열."""
        return " / ".join(self.reasons)


def _trading_day_gap(dates: np.ndarray, a: np.datetime64, b: np.datetime64) -> int:
    """a → b 사이의 거래일 수. 달력일이 아니라 거래일로 세야 한다.

    ``dates``는 시장 거래일 캘린더(정렬된 datetime64[D]). 주말·휴장일이
    끼면 달력일 3일과 거래일 3일이 크게 달라진다.
    """
    ia = int(np.searchsorted(dates, a, side="left"))
    ib = int(np.searchsorted(dates, b, side="left"))
    return ib - ia


def apply_filters(
    signals: Sequence[Signal],
    cfg: FilterConfig,
    *,
    prior_exits: Sequence[ExitResult] = (),
    calendar: np.ndarray | None = None,
) -> list[FilterDecision]:
    """시그널 목록에 실행 필터를 적용한다.

    Args:
        signals: 평가할 시그널. 내부에서 시그널일자 순으로 정렬해 처리한다.
        cfg: 필터 파라미터.
        prior_exits: 쿨다운 판정에 쓸 **과거 청산 결과**. 같은 백테스트
            런의 결과를 넣으면 되지만, 각 시그널은 자기 시그널일자보다
            **이전에 청산된** 건만 참조한다(미래 참조 차단).
        calendar: 거래일 캘린더. 없으면 시그널 날짜들로 근사한다 —
            듬성듬성하면 쿨다운이 실제보다 길게 잡힐 수 있다.

    Returns:
        입력과 같은 순서의 판정 목록.
    """
    decisions: dict[int, FilterDecision] = {}
    order = sorted(range(len(signals)), key=lambda i: signals[i].signal_date)

    if calendar is None:
        calendar = np.unique(
            np.array([s.signal_date for s in signals], dtype="datetime64[D]")
        )

    # 종목별 SL 청산 이력: (청산일, ) 목록
    sl_exits: dict[str, list[np.datetime64]] = {}
    for r in prior_exits:
        if r.exit_type == EXIT_SL and r.exit_date is not None:
            sl_exits.setdefault(r.symbol, []).append(r.exit_date)
    for v in sl_exits.values():
        v.sort()

    # 동시 보유 추적: 청산일이 아직 안 지난 건들의 섹터를 센다.
    # (시그널, 예상청산일) 큐 — 예상청산일은 라벨 보유상한으로 근사한다.
    open_positions: list[tuple[np.datetime64, str]] = []  # (청산예정일, 섹터)
    exit_by_key: dict[tuple[str, np.datetime64], np.datetime64] = {}
    for r in prior_exits:
        if r.exit_date is not None:
            exit_by_key[(r.symbol, r.signal_date)] = r.exit_date

    for i in order:
        sig = signals[i]
        reasons: list[str] = []

        if not cfg.enabled:
            decisions[i] = FilterDecision(sig, True, [])
            continue

        # --- ① 확신도 -----------------------------------------------------
        if cfg.min_confidence is not None:
            conf = sig.confidence
            # 확신도가 아예 기록되지 않은 건은 차단하지 않는다. 과거 데이터에
            # 컬럼이 없던 구간을 통째로 날리면 표본이 사라진다.
            if np.isfinite(conf) and conf < cfg.min_confidence:
                reasons.append(REASON_CONFIDENCE)

        # --- ② SL 후 쿨다운 -------------------------------------------------
        if cfg.cooldown_days > 0:
            for exit_date in sl_exits.get(sig.symbol, ()):
                if exit_date >= sig.signal_date:
                    break   # 미래의 청산 — 참조 금지
                gap = _trading_day_gap(calendar, exit_date, sig.signal_date)
                if 0 <= gap < cfg.cooldown_days:
                    reasons.append(REASON_COOLDOWN)
                    break

        # --- ③ 섹터 동시보유 한도 --------------------------------------------
        open_positions = [
            (ed, sec) for ed, sec in open_positions if ed >= sig.signal_date
        ]
        if cfg.max_per_sector > 0 and sig.sector:
            same = sum(1 for _, sec in open_positions if sec == sig.sector)
            if same >= cfg.max_per_sector:
                reasons.append(REASON_SECTOR)

        executable = not reasons
        decisions[i] = FilterDecision(sig, executable, reasons)

        # 실행된 건만 보유로 잡는다. 차단분은 포지션을 차지하지 않는다.
        if executable and sig.sector:
            ed = exit_by_key.get((sig.symbol, sig.signal_date))
            if ed is not None:
                open_positions.append((ed, sig.sector))

    return [decisions[i] for i in range(len(signals))]


def split_by_filter(
    signals: Sequence[Signal],
    cfg: FilterConfig,
    *,
    prior_exits: Sequence[ExitResult] = (),
    calendar: np.ndarray | None = None,
) -> tuple[list[Signal], list[FilterDecision]]:
    """(실행 대상 시그널, 전체 판정) 쌍을 돌려준다.

    필터 적용 전/후 백테스트 지표를 나란히 비교하려면 둘 다 필요하다
    (TASK.md Phase 2 마지막 항목).
    """
    decisions = apply_filters(
        signals, cfg, prior_exits=prior_exits, calendar=calendar
    )
    passed = [d.signal for d in decisions if d.executable]
    return passed, decisions
