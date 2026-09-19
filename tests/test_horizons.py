"""라벨 정규화와 보유상한 표 — 세 지평이 섞이지 않는지."""

from __future__ import annotations

import pytest

from stock_auto.horizons import (
    ALL_LABELS,
    LEGACY_HOLD_DAYS,
    PROPOSED_HOLD_DAYS,
    SCORING_HORIZON_DAYS,
    UNKNOWN,
    hold_days,
    normalize_label,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("단타", "단타"), ("day", "단타"), ("DayTrade", "단타"), ("  단타 ", "단타"),
        ("중단기", "중단기"), ("mid", "중단기"), ("mid_term", "중단기"),
        ("스윙", "스윙"), ("SWING", "스윙"), ("long-term", "스윙"),
    ],
)
def test_aliases_normalize(raw, expected):
    assert normalize_label(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "nan", "정체불명", 123])
def test_unrecognized_falls_to_unknown_not_daytrade(raw):
    """모르는 라벨을 단타로 떨어뜨리면 보유상한이 조용히 바뀐다."""
    assert normalize_label(raw) == UNKNOWN


def test_three_tables_are_distinct_concepts():
    """채점 지평 · legacy 보유상한 · 제안 보유상한은 서로 다른 표다."""
    assert SCORING_HORIZON_DAYS != LEGACY_HOLD_DAYS
    assert LEGACY_HOLD_DAYS != PROPOSED_HOLD_DAYS
    assert set(SCORING_HORIZON_DAYS) == set(LEGACY_HOLD_DAYS) == set(PROPOSED_HOLD_DAYS)


def test_legacy_is_one_day_everywhere():
    assert set(LEGACY_HOLD_DAYS.values()) == {1}


def test_proposed_increases_with_horizon():
    assert (
        PROPOSED_HOLD_DAYS["단타"]
        < PROPOSED_HOLD_DAYS["중단기"]
        < PROPOSED_HOLD_DAYS["스윙"]
    )


def test_scoring_horizon_matches_documented_values():
    """추천_읽는_법 §1-④의 3 / 7 / 20일."""
    assert SCORING_HORIZON_DAYS == {"단타": 3, "중단기": 7, "스윙": 20}


def test_hold_days_unknown_uses_most_conservative():
    assert hold_days("정체불명", PROPOSED_HOLD_DAYS) == min(PROPOSED_HOLD_DAYS.values())


def test_label_order_is_stable():
    assert ALL_LABELS == ("단타", "중단기", "스윙")
