"""ExitConfig — 청산 규칙 파라미터.

TASK.md Phase 1의 요구를 dataclass 하나로 모은다. 지금까지 청산 파라미터는
코드 여기저기에 상수로 박혀 있었고, 그래서 "보유상한을 2일로 바꾸면 어떻게
되나"를 물어볼 방법이 없었다. 값을 전부 이 객체로 끌어내면 그 질문이
그리드 한 줄이 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from stock_auto.horizons import (
    ALL_LABELS,
    LEGACY_HOLD_DAYS,
    PROPOSED_HOLD_DAYS,
    normalize_label,
)

#: SL/TP 폭을 무엇으로 재는가.
#:   ``pct`` — 진입가 대비 고정 퍼센트 (현행)
#:   ``atr`` — 진입 전일 기준 ATR(14) 배수 (제안)
BarrierMode = Literal["pct", "atr"]


@dataclass(frozen=True, slots=True)
class LabelExit:
    """한 라벨(단타/중단기/스윙)의 청산 파라미터.

    라벨마다 보유상한뿐 아니라 SL/TP 폭까지 따로 잡을 수 있어야 한다.
    스윙에 단타용 손절폭을 걸면 노이즈에 털리고, 반대면 손실이 방치된다.
    """

    hold_days: int
    sl_mode: BarrierMode = "pct"
    sl_value: float = 3.0
    tp_mode: BarrierMode = "pct"
    tp_value: float = 3.0

    def __post_init__(self) -> None:
        if self.hold_days < 1:
            raise ValueError(f"hold_days는 1 이상이어야 한다: {self.hold_days}")
        if self.sl_value <= 0 or self.tp_value <= 0:
            raise ValueError("sl_value/tp_value는 양수여야 한다 (폭이지 부호가 아님)")

    def describe(self) -> str:
        """대시보드 상단 sub 라인용 한 줄 표기 (TASK.md Phase 4)."""
        sl = f"{self.sl_value:g}%" if self.sl_mode == "pct" else f"{self.sl_value:g}ATR"
        tp = f"{self.tp_value:g}%" if self.tp_mode == "pct" else f"{self.tp_value:g}ATR"
        return f"D+{self.hold_days} · SL {sl} · TP {tp}"


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """청산 엔진 전체 설정.

    라벨별 파라미터(:class:`LabelExit`)와, 라벨과 무관하게 전역으로 적용되는
    체결·판정 옵션으로 나뉜다.
    """

    by_label: dict[str, LabelExit]

    # --- 판정 방식 -------------------------------------------------------
    #: True면 배리어 도달을 **종가로만** 판정한다. 장중 위크에 털리는 것과
    #: 갭 리스크를 떠안는 것 사이의 맞교환이고, 어느 쪽이 나은지는 데이터가 답한다.
    close_based: bool = False

    #: 같은 날 SL·TP가 동시에 닿으면 어느 쪽으로 볼 것인가. 일봉만으로는
    #: 장중 선후를 알 수 없으므로 보수적으로 SL을 택한다(기존 라벨러와 동일).
    tie_breaker: Literal["sl", "tp"] = "sl"

    # --- 체결 현실화 -----------------------------------------------------
    #: 편도 슬리피지(bp). 왕복 기준이 아니라 진입·청산 각각에 적용된다.
    slippage_bps: float = 0.0

    #: True면 갭을 체결가에 반영한다 (TASK.md Phase 1).
    #:   SL 체결가 = min(손절가, 당일 시가)  — 갭다운이면 시가로 더 나쁘게
    #:   TP 체결가 = min(목표가, 당일 시가)  — 갭업이어도 목표가로만
    #: 즉 갭은 항상 불리하게만 반영한다. 이게 실제로 벌어지는 일이다.
    gap_aware_fill: bool = True

    # --- 선택 규칙 -------------------------------------------------------
    #: +N ATR 도달 시 손절을 본전(진입가)으로 끌어올린다. None이면 비활성.
    trail_be_atr: float | None = None

    #: 목표가 도달 시 이 비율만 익절하고 나머지는 보유상한까지 끌고 간다.
    #: 0.5면 절반 익절. None이면 전량 청산(현행).
    partial_tp: float | None = None

    def __post_init__(self) -> None:
        if not self.by_label:
            raise ValueError("by_label이 비었다")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps는 음수일 수 없다")
        if self.partial_tp is not None and not 0.0 < self.partial_tp < 1.0:
            raise ValueError("partial_tp는 0과 1 사이여야 한다 (1.0은 전량=None)")
        if self.trail_be_atr is not None and self.trail_be_atr <= 0:
            raise ValueError("trail_be_atr는 양수여야 한다")

    # --- 조회 -----------------------------------------------------------
    def for_label(self, label: str) -> LabelExit:
        """라벨의 청산 파라미터. 미분류는 가장 짧은 보유상한 쪽으로 붙인다."""
        norm = normalize_label(label)
        hit = self.by_label.get(norm)
        if hit is not None:
            return hit
        return min(self.by_label.values(), key=lambda le: le.hold_days)

    def needs_atr(self) -> bool:
        """ATR 컬럼이 있어야 돌아가는 설정인지. 데이터 준비 단계에서 쓴다."""
        if self.trail_be_atr is not None:
            return True
        return any(
            le.sl_mode == "atr" or le.tp_mode == "atr" for le in self.by_label.values()
        )

    def with_label(self, label: str, le: LabelExit) -> "ExitConfig":
        """라벨 하나만 갈아끼운 새 설정. 그리드가 라벨별로 훑을 때 쓴다."""
        merged = dict(self.by_label)
        merged[normalize_label(label)] = le
        return replace(self, by_label=merged)

    def describe(self) -> str:
        """대시보드 상단 한 줄 (TASK.md Phase 4 마지막 항목)."""
        parts = [
            f"{lab} {self.by_label[lab].describe()}"
            for lab in ALL_LABELS
            if lab in self.by_label
        ]
        flags = ["종가판정" if self.close_based else "장중판정"]
        if self.gap_aware_fill:
            flags.append("갭반영")
        if self.slippage_bps:
            flags.append(f"슬리피지 {self.slippage_bps:g}bp")
        if self.trail_be_atr:
            flags.append(f"BE트레일 {self.trail_be_atr:g}ATR")
        if self.partial_tp:
            flags.append(f"부분익절 {self.partial_tp:.0%}")
        return " | ".join(parts) + "  ·  " + " · ".join(flags)

    # --- 프리셋 ---------------------------------------------------------
    @classmethod
    def legacy(cls) -> "ExitConfig":
        """현행 프로덕션 동작 재현.

        보유상한 1일 · 고정 %TP/SL · 장중 판정 · 갭/슬리피지 미반영.
        TASK.md Phase 1의 회귀 테스트 기준선이다. 여기서 나온 숫자가
        기존 12건 결과와 어긋나면 엔진이 틀린 것이지 설정이 틀린 게 아니다.
        """
        return cls(
            by_label={
                lab: LabelExit(
                    hold_days=LEGACY_HOLD_DAYS[lab],
                    sl_mode="pct",
                    sl_value=3.0,
                    tp_mode="pct",
                    tp_value=3.0,
                )
                for lab in ALL_LABELS
            },
            close_based=False,
            gap_aware_fill=False,
            slippage_bps=0.0,
        )

    @classmethod
    def proposed(cls) -> "ExitConfig":
        """analysis_2026-09-18 §4 P0의 제안값.

        **기본값으로 쓰라는 뜻이 아니다.** 그리드의 출발점이자 legacy와
        나란히 놓고 볼 비교군이다. 확정은 사람이 그리드를 보고 한다.
        """
        return cls(
            by_label={
                lab: LabelExit(
                    hold_days=PROPOSED_HOLD_DAYS[lab],
                    sl_mode="atr",
                    sl_value=1.5,
                    tp_mode="atr",
                    tp_value=2.5,
                )
                for lab in ALL_LABELS
            },
            close_based=False,
            gap_aware_fill=True,
            slippage_bps=5.0,
        )

    @classmethod
    def uniform(
        cls,
        hold_days: int,
        sl_mode: BarrierMode,
        sl_value: float,
        tp_mode: BarrierMode,
        tp_value: float,
        **kwargs: object,
    ) -> "ExitConfig":
        """세 라벨에 같은 파라미터를 적용한 설정. 그리드 셀 하나를 만들 때 쓴다."""
        le = LabelExit(
            hold_days=hold_days,
            sl_mode=sl_mode,
            sl_value=sl_value,
            tp_mode=tp_mode,
            tp_value=tp_value,
        )
        return cls(by_label={lab: le for lab in ALL_LABELS}, **kwargs)  # type: ignore[arg-type]
