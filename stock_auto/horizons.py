"""보유기간 라벨(단타 / 중단기 / 스윙) 단일 정의.

이 시스템의 라벨은 세 군데에서 서로 다른 의미로 쓰여 왔다. 섞이면 백테스트
결과 해석이 통째로 틀어지므로 여기서 한 번에 구분해 둔다.

1. **채점 지평(scoring horizon)** — `추천_읽는_법과_확인사항.md` §4의
   단타 3일 / 중단기 7일 / 스윙 20일. 라벨러가 "이 추천이 맞았나"를
   판정할 때 쓰는 관찰 창이다. 청산 규칙이 아니다.
2. **실제 보유상한(legacy)** — 프로덕션에서 세 라벨 전부 **D+1**.
   `docs/analysis_2026-09-18.md` §3-1이 지목한 설계 모순이 이것이다.
   라벨을 붙여놓고 청산은 셋 다 하루 룰로 돌았다.
3. **제안 보유상한** — 같은 문서 §4 P0-1의 단타 D+2 / 중단기 D+3 / 스윙 D+5.
   어디까지나 **초기값**이고, 확정은 백테스트 그리드가 한다.

`ExitConfig.legacy()`는 2번을, `ExitConfig.proposed()`는 3번을 쓴다.
1번은 MFE/MAE 관찰 창(`SCORING_HORIZON_DAYS`)으로만 남겨 둔다.
"""

from __future__ import annotations

from typing import Final

DAYTRADE: Final = "단타"
SWING_SHORT: Final = "중단기"
SWING: Final = "스윙"

#: 정렬·출력 순서. 리포트는 항상 이 순서를 따른다.
ALL_LABELS: Final[tuple[str, ...]] = (DAYTRADE, SWING_SHORT, SWING)

#: 라벨을 못 붙인(또는 스키마에 없는) 시그널을 몰아넣는 버킷.
UNKNOWN: Final = "미분류"

#: 1. 채점 지평 — MFE/MAE 관찰 창. 청산과 무관.
SCORING_HORIZON_DAYS: Final[dict[str, int]] = {
    DAYTRADE: 3,
    SWING_SHORT: 7,
    SWING: 20,
}

#: 2. 프로덕션 현행 보유상한 — 전 라벨 D+1.
LEGACY_HOLD_DAYS: Final[dict[str, int]] = {
    DAYTRADE: 1,
    SWING_SHORT: 1,
    SWING: 1,
}

#: 3. analysis_2026-09-18 §4 P0-1 제안 보유상한. 그리드가 확정하기 전 초기값.
PROPOSED_HOLD_DAYS: Final[dict[str, int]] = {
    DAYTRADE: 2,
    SWING_SHORT: 3,
    SWING: 5,
}

#: MFE/MAE를 재는 고정 창. 문서 전반에서 "MFE20 / MAE20"으로 부르는 그 20일.
MFE_MAE_WINDOW: Final = 20

#: 라벨 표기 흔들림 흡수용. 영문/축약/공백 표기가 섞여 들어온다.
_ALIASES: Final[dict[str, str]] = {
    "단타": DAYTRADE,
    "day": DAYTRADE,
    "daytrade": DAYTRADE,
    "day_trade": DAYTRADE,
    "scalp": DAYTRADE,
    "short": DAYTRADE,
    "중단기": SWING_SHORT,
    "중기": SWING_SHORT,
    "mid": SWING_SHORT,
    "midterm": SWING_SHORT,
    "mid_term": SWING_SHORT,
    "swing_short": SWING_SHORT,
    "스윙": SWING,
    "장기": SWING,
    "swing": SWING,
    "long": SWING,
    "longterm": SWING,
    "long_term": SWING,
}


def normalize_label(raw: object) -> str:
    """자유 표기 라벨을 세 표준 라벨 중 하나로 정규화한다.

    매칭에 실패하면 조용히 단타로 떨어뜨리지 않고 :data:`UNKNOWN`을 돌려준다.
    보유상한이 라벨마다 다른 이상, 잘못 찍힌 라벨은 "모른다"로 남기고
    리포트에서 눈에 띄게 하는 편이 안전하다.
    """
    if raw is None:
        return UNKNOWN
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "")
    if not key or key in {"nan", "none", "null"}:
        return UNKNOWN
    return _ALIASES.get(key, UNKNOWN)


def hold_days(label: str, table: dict[str, int], default: int = 1) -> int:
    """라벨별 보유상한 조회. 미분류는 가장 보수적인(= 가장 짧은) 값을 쓴다."""
    norm = normalize_label(label)
    if norm is UNKNOWN or norm not in table:
        return min(table.values()) if table else default
    return table[norm]
