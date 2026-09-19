"""유니버스 정의.

문서(`현재_적용_로직_전수.md` §2.2)는 `config/universe.py`의 `US_SAMPLE`이
**20종목 하드코딩**이라고만 적고 종목명을 나열하지 않는다. 원본 파일이
이 저장소에 없으므로 아래 목록은 **추정치**다.

- `analysis_2026-09-18.md`에 실제로 등장한 종목(SWKS, HPQ, HPE, DELL, META,
  KHC, VLO, KR, AMD, SMCI, ARM, INTC)을 전부 포함시켰다. 이들은 실제
  유니버스에 있는 것이 확실하다.
- 나머지는 섹터 분산을 맞추기 위해 채운 대형주다.

**실제 유니버스를 쓰려면** `--universe universe.csv`로 덮어써야 한다
(컬럼 `symbol,sector`). 유니버스가 다르면 시그널도 달라지므로, 재현도를
따질 때 가장 먼저 맞춰야 할 항목이다.
"""

from __future__ import annotations

from pathlib import Path

#: {종목: 섹터 ETF}. 섹터는 동시보유 한도(FilterConfig.max_per_sector)에 쓰인다.
US_SAMPLE: dict[str, str] = {
    # analysis_2026-09-18에 실제 등장 — 유니버스 포함 확실
    "SWKS": "XLK", "HPQ": "XLK", "HPE": "XLK", "DELL": "XLK",
    "AMD": "XLK", "SMCI": "XLK", "ARM": "XLK", "INTC": "XLK",
    "META": "XLC", "KHC": "XLP", "VLO": "XLE", "KR": "XLP",
    # 섹터 분산용 대형주 (추정)
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK",
    "JPM": "XLF", "BAC": "XLF",
    "XOM": "XLE", "JNJ": "XLV", "UNH": "XLV",
    "AMZN": "XLY", "TSLA": "XLY",
    "CAT": "XLI", "GE": "XLI",
}

#: 매크로 게이트용 지수. 못 받으면 합성지수로 폴백한다.
US_INDICES: tuple[str, ...] = ("^GSPC", "^IXIC")


def load_universe(path: str | Path | None) -> dict[str, str]:
    """유니버스 CSV(`symbol,sector`) 로딩. 경로가 없으면 기본 추정 목록."""
    if path is None:
        return dict(US_SAMPLE)

    import pandas as pd

    df = pd.read_csv(path)
    lowered = {str(c).strip().lower(): c for c in df.columns}
    c_sym = next(
        (lowered[k] for k in ("symbol", "ticker", "code", "종목") if k in lowered),
        None,
    )
    if c_sym is None:
        raise ValueError(
            f"{path}: 종목 컬럼을 못 찾았다. 있는 컬럼: {list(df.columns)}"
        )
    c_sec = next(
        (lowered[k] for k in ("sector", "sector_etf", "섹터") if k in lowered), None
    )
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        sym = str(row[c_sym]).strip().upper()
        if sym:
            out[sym] = str(row[c_sec]).strip() if c_sec else ""
    return out
