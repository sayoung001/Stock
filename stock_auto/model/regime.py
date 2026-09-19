"""레짐 — 종목 5단계 + 매크로 지수 게이트.

종목 레짐은 이동평균 배열로 0~4를 매긴다(문서 §2.2 "5단계 + 전환 방향").
매크로 게이트는 지수 레짐이 0이면 **전 종목 매수를 막는다**. 개별 종목이
정배열이어도 약세장에서는 사지 않겠다는 뜻이고, 문서가 "보수적 설계"라고
적은 그 장치다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 지수 그룹. S&P 계열과 나스닥 계열을 나누고, 그룹 간에는 **낮은 쪽**을
#: 채택한다(보수적). 나스닥 계열끼리는 상관이 높아 **나은 쪽**으로 대표시킨다.
#: 근거: `추천_읽는_법과_확인사항.md` §1-②.
INDEX_GROUPS: dict[str, tuple[str, ...]] = {
    "sp500": ("US500", "^GSPC", "SPY"),
    "nasdaq": ("IXIC", "^IXIC", "QQQ"),
}


def stock_regime(ind: pd.DataFrame) -> pd.Series:
    """종목 레짐 0~4.

    | 값 | 상태 | 조건 |
    |---|---|---|
    | 4 | 강세 정배열 | MA5>MA10>MA20>MA60 |
    | 3 | 상승 | 종가>MA20 이고 MA20>MA60 |
    | 2 | 중립 | 종가>MA60 |
    | 1 | 하락초기 | MA20>MA60 이지만 종가<MA60 |
    | 0 | 하락위험 | 그 외 (역배열) |

    0이면 매수 차단, 1(하락초기)은 통과한다 — 통과하지만 감점 요인이라는
    것이 문서의 설명이다.
    """
    c = ind["close"]
    ma5, ma10, ma20, ma60 = (ind[f"MA{p}"] for p in (5, 10, 20, 60))

    r = pd.Series(0, index=ind.index, dtype="int64")
    r = r.mask((c > ma60), 2)
    r = r.mask((c > ma20) & (ma20 > ma60), 3)
    r = r.mask((ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60), 4)
    r = r.mask((ma20 > ma60) & (c <= ma60), 1)
    # 워밍업 구간은 판정 불가 → 0(차단)으로 둔다. 모르면 사지 않는다.
    r = r.mask(ma60.isna(), 0)
    return r


def index_regime(index_df: pd.DataFrame) -> pd.Series:
    """지수 일봉 → 레짐 시계열. 종목과 같은 규칙을 쓴다."""
    from stock_auto.model.indicators import calculate_indicators

    return stock_regime(calculate_indicators(index_df, market="US"))


def macro_gate(
    index_frames: dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
    *,
    block_at: int = 0,
) -> tuple[pd.Series, pd.Series]:
    """매크로 게이트.

    Args:
        index_frames: {지수심볼: 일봉 DataFrame}
        calendar: 판정할 날짜들
        block_at: 이 값 이하이면 차단 (기본 0)

    Returns:
        (통과 여부 bool 시계열, 종합 레짐 값 시계열)

    지수 데이터가 하나도 없으면 **게이트를 열어 둔다**(전부 통과).
    닫아 버리면 시그널이 0건이 되어 백테스트 자체가 불가능해지고, 그건
    "지수 데이터를 못 구했다"는 사실을 결과로 위장하는 셈이다. 대신
    호출자가 이 사실을 알 수 있게 `run_model_backtest`가 경고를 찍는다.
    """
    if not index_frames:
        return (
            pd.Series(True, index=calendar),
            pd.Series(np.nan, index=calendar),
        )

    group_vals: list[pd.Series] = []
    for symbols in INDEX_GROUPS.values():
        members = [
            index_regime(index_frames[s]).reindex(calendar).ffill()
            for s in symbols
            if s in index_frames
        ]
        if members:
            # 그룹 내부는 나은 쪽(max)으로 대표.
            group_vals.append(pd.concat(members, axis=1).max(axis=1))

    # 매칭 안 된 지수는 자기 혼자 그룹으로 친다.
    known = {s for syms in INDEX_GROUPS.values() for s in syms}
    for sym, df in index_frames.items():
        if sym not in known:
            group_vals.append(index_regime(df).reindex(calendar).ffill())

    if not group_vals:
        return pd.Series(True, index=calendar), pd.Series(np.nan, index=calendar)

    # 그룹 간에는 낮은 쪽(min) — 보수적.
    combined = pd.concat(group_vals, axis=1).min(axis=1)
    ok = (combined > block_at).fillna(True)
    return ok, combined


def synthetic_index(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """유니버스 동일가중 합성지수.

    실제 지수 일봉을 못 구했을 때의 **폴백**이다. 유니버스가 대형주
    중심이면 지수와 상관이 높아 게이트 역할을 어느 정도 한다. 다만
    유니버스 자체가 편향돼 있으면(예: 전부 반도체) 지수 대용이 못 되므로
    `docs/MODEL_REPRODUCTION.md` A6에 한계를 적어 두었다.
    """
    if not prices:
        raise ValueError("합성지수를 만들 가격 데이터가 없다")

    # 각 종목을 첫 종가 100으로 정규화한 뒤 평균낸다.
    cols = {}
    for sym, df in prices.items():
        c = df["close"]
        if c.notna().sum() < 2:
            continue
        cols[sym] = c / c.dropna().iloc[0] * 100.0
    if not cols:
        raise ValueError("유효한 종가 계열이 없다")

    close = pd.concat(cols, axis=1).mean(axis=1).dropna()
    # OHLC는 종가 기반으로 근사한다. 레짐은 이동평균만 보므로 충분하다.
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close),
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
        },
        index=close.index,
    )
