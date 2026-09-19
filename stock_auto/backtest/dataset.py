"""시그널·가격 데이터 로딩.

스코어링 계층의 시그널 저장소(`data/tracking/signals.csv`)와 일봉 캐시를
읽어 청산 엔진이 먹을 형태로 바꾼다. 컬럼명은 배포본마다 흔들리므로
별칭 표를 두고 유연하게 매핑한다 — 컬럼 하나 이름이 달라서 백테스트가
안 돌아가는 상황을 만들지 않는다.

가격 데이터 우선순위:
  1. 로컬 CSV 캐시 (`--price-dir`)
  2. FinanceDataReader
  3. yfinance
  4. 합성 일봉 (`make_synthetic_dataset`) — **오프라인 로직 검증 전용**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from stock_auto.exit.engine import PriceSeries, Signal
from stock_auto.horizons import ALL_LABELS, normalize_label

# --- 컬럼 별칭 ------------------------------------------------------------
_SYMBOL_COLS = ("symbol", "ticker", "code", "종목", "종목코드")
_DATE_COLS = (
    "signal_date", "date", "asof", "asof_date", "base_date", "bar_date",
    "기준일", "기준봉", "날짜",
)
_LABEL_COLS = ("label", "horizon", "hold_label", "보유기간", "라벨", "구분")
_CONF_COLS = ("confidence", "conviction", "conf", "확신도", "확신")
_SECTOR_COLS = ("sector", "sector_etf", "gics_sector", "섹터", "섹터ETF")
_SOURCE_COLS = ("source", "origin", "signal_source", "출처")

_OHLC_ALIASES = {
    "open": ("open", "Open", "OPEN", "시가"),
    "high": ("high", "High", "HIGH", "고가"),
    "low": ("low", "Low", "LOW", "저가"),
    "close": ("close", "Close", "CLOSE", "종가", "adj_close", "Adj Close"),
}


def _pick(df: pd.DataFrame, candidates: Sequence[str]) -> str | None:
    """후보 중 실제로 존재하는 컬럼명. 대소문자/공백 무시."""
    lowered = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        hit = lowered.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


@dataclass(slots=True)
class Dataset:
    """백테스트 한 판에 필요한 것 전부."""

    signals: list[Signal]
    prices: dict[str, PriceSeries]
    calendar: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[D]"))
    note: str = ""

    def __post_init__(self) -> None:
        if self.calendar.size == 0 and self.prices:
            # 전 종목 날짜의 합집합을 거래일 캘린더로 쓴다.
            alld = np.unique(np.concatenate([p.dates for p in self.prices.values()]))
            self.calendar = alld

    @property
    def span(self) -> tuple[np.datetime64, np.datetime64] | None:
        if not self.signals:
            return None
        ds = [s.signal_date for s in self.signals]
        return min(ds), max(ds)

    def label_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.signals:
            out[normalize_label(s.label)] = out.get(normalize_label(s.label), 0) + 1
        return out

    def filter_dates(
        self, start: np.datetime64 | None = None, end: np.datetime64 | None = None
    ) -> "Dataset":
        """시그널을 날짜로 자른다. 가격은 그대로 둔다(청산이 구간 밖으로 나갈 수 있으므로)."""
        sigs = [
            s for s in self.signals
            if (start is None or s.signal_date >= start)
            and (end is None or s.signal_date <= end)
        ]
        return Dataset(sigs, self.prices, self.calendar, self.note)

    def describe(self) -> str:
        sp = self.span
        span_txt = f"{sp[0]} ~ {sp[1]}" if sp else "없음"
        counts = self.label_counts()
        lab_txt = " · ".join(
            f"{k} {counts.get(k, 0)}" for k in ALL_LABELS if counts.get(k)
        ) or "라벨 없음"
        extra = " · ".join(
            f"{k} {v}" for k, v in counts.items() if k not in ALL_LABELS
        )
        if extra:
            lab_txt += " · " + extra
        return (
            f"시그널 {len(self.signals)}건 ({span_txt}) · 종목 {len(self.prices)} "
            f"· {lab_txt}"
        )


# --- 시그널 ---------------------------------------------------------------
def load_signals(
    path: str | Path,
    *,
    default_label: str = "중단기",
) -> list[Signal]:
    """시그널 CSV → :class:`Signal` 목록.

    라벨 컬럼이 없거나 비어 있으면 ``default_label``을 쓰되, 그 사실을
    호출자가 알 수 있도록 ``source``에 ``label=default`` 표시를 남긴다.
    """
    df = pd.read_csv(path)
    if df.empty:
        return []

    c_sym = _pick(df, _SYMBOL_COLS)
    c_date = _pick(df, _DATE_COLS)
    if c_sym is None or c_date is None:
        raise ValueError(
            f"{path}: 종목/날짜 컬럼을 못 찾았다. 있는 컬럼: {list(df.columns)}"
        )
    c_label = _pick(df, _LABEL_COLS)
    c_conf = _pick(df, _CONF_COLS)
    c_sector = _pick(df, _SECTOR_COLS)
    c_source = _pick(df, _SOURCE_COLS)

    dates = pd.to_datetime(df[c_date], errors="coerce")
    out: list[Signal] = []
    for i, row in df.iterrows():
        d = dates.iloc[i] if hasattr(dates, "iloc") else dates[i]
        if pd.isna(d):
            continue
        raw_label = row[c_label] if c_label else None
        norm = normalize_label(raw_label)
        src = str(row[c_source]) if c_source and not pd.isna(row[c_source]) else ""
        if norm == "미분류":
            norm = normalize_label(default_label)
            src = (src + " label=default").strip()
        conf = float("nan")
        if c_conf is not None and not pd.isna(row[c_conf]):
            try:
                conf = float(row[c_conf])
            except (TypeError, ValueError):
                conf = float("nan")
        out.append(
            Signal(
                symbol=str(row[c_sym]).strip().upper(),
                signal_date=np.datetime64(pd.Timestamp(d).date(), "D"),
                label=norm,
                confidence=conf,
                sector=(
                    str(row[c_sector]).strip()
                    if c_sector and not pd.isna(row[c_sector]) else ""
                ),
                source=src,
            )
        )
    out.sort(key=lambda s: (s.signal_date, s.symbol))
    return out


# --- 가격 -----------------------------------------------------------------
def _from_frame(symbol: str, df: pd.DataFrame) -> PriceSeries | None:
    """DataFrame → PriceSeries. OHLC 중 하나라도 없으면 None."""
    if df is None or df.empty:
        return None
    df = df.copy()

    # 인덱스가 날짜인 경우(FDR/yfinance 기본)와 컬럼인 경우 모두 지원.
    if not isinstance(df.index, pd.DatetimeIndex):
        c_date = _pick(df, _DATE_COLS + ("Date", "index"))
        if c_date is None:
            return None
        df.index = pd.to_datetime(df[c_date], errors="coerce")
    df = df[~df.index.isna()]
    if df.empty:
        return None

    cols: dict[str, np.ndarray] = {}
    for key, aliases in _OHLC_ALIASES.items():
        c = _pick(df, aliases)
        if c is None:
            return None
        cols[key] = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=np.float64)

    dates = df.index.normalize().to_numpy(dtype="datetime64[D]")
    # 중복일 제거(마지막 우선) + 정렬
    order = np.argsort(dates, kind="stable")
    dates = dates[order]
    for k in cols:
        cols[k] = cols[k][order]
    keep = np.ones(dates.size, dtype=bool)
    keep[:-1] = dates[1:] != dates[:-1]
    dates = dates[keep]
    for k in cols:
        cols[k] = cols[k][keep]

    # OHLC에 NaN이 낀 봉은 버린다. 배리어 판정이 조용히 틀어지는 것보다 낫다.
    good = np.isfinite(cols["open"]) & np.isfinite(cols["high"]) \
        & np.isfinite(cols["low"]) & np.isfinite(cols["close"])
    if not good.any():
        return None
    dates = dates[good]
    for k in cols:
        cols[k] = cols[k][good]

    return PriceSeries(
        symbol=symbol, dates=dates,
        open=cols["open"], high=cols["high"],
        low=cols["low"], close=cols["close"],
    )


def load_prices(
    symbols: Iterable[str],
    *,
    price_dir: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    allow_download: bool = True,
    verbose: bool = False,
) -> tuple[dict[str, PriceSeries], list[str]]:
    """종목별 일봉을 모은다.

    Returns:
        (가격 딕셔너리, 실패한 종목 목록)
    """
    prices: dict[str, PriceSeries] = {}
    missing: list[str] = []
    cache = Path(price_dir) if price_dir else None

    for sym in sorted(set(symbols)):
        ps: PriceSeries | None = None

        # 1) 로컬 CSV 캐시
        if cache is not None:
            for name in (f"{sym}.csv", f"{sym.upper()}.csv", f"{sym.lower()}.csv"):
                f = cache / name
                if f.exists():
                    try:
                        ps = _from_frame(sym, pd.read_csv(f))
                    except Exception as e:                      # noqa: BLE001
                        if verbose:
                            print(f"  [{sym}] 캐시 읽기 실패: {e}")
                    break

        # 2) FinanceDataReader → 3) yfinance
        if ps is None and allow_download:
            ps = _download(sym, start, end, verbose=verbose)
            if ps is not None and cache is not None:
                cache.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(
                    {
                        "date": ps.dates, "open": ps.open, "high": ps.high,
                        "low": ps.low, "close": ps.close,
                    }
                ).to_csv(cache / f"{sym}.csv", index=False)

        if ps is None:
            missing.append(sym)
        else:
            prices[sym] = ps

    return prices, missing


def _download(
    sym: str, start: str | None, end: str | None, *, verbose: bool = False
) -> PriceSeries | None:
    """FDR → yfinance 순서로 시도. 둘 다 없거나 실패하면 None."""
    try:
        import FinanceDataReader as fdr     # type: ignore

        df = fdr.DataReader(sym, start, end)
        ps = _from_frame(sym, df)
        if ps is not None:
            return ps
    except Exception as e:                   # noqa: BLE001
        if verbose:
            print(f"  [{sym}] FDR 실패: {e}")

    try:
        import yfinance as yf                # type: ignore

        df = yf.download(
            sym, start=start, end=end, progress=False, auto_adjust=False
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        ps = _from_frame(sym, df)
        if ps is not None:
            return ps
    except Exception as e:                   # noqa: BLE001
        if verbose:
            print(f"  [{sym}] yfinance 실패: {e}")

    return None


# --- 합성 데이터 (오프라인 검증 전용) ---------------------------------------
def make_synthetic_dataset(
    n_symbols: int = 20,
    n_days: int = 400,
    signals_per_day: float = 3.0,
    *,
    seed: int = 20260918,
    end_date: str = "2026-09-18",
    daily_vol: float = 0.022,
    drift: float = 0.0003,
    edge: float = 0.0,
) -> Dataset:
    """합성 일봉 + 합성 시그널.

    **성과 수치를 여기서 읽으면 안 된다.** 용도는 두 가지뿐이다 —
    (a) 엔진·그리드가 돌아가는지 검증, (b) 실행 시간 측정.
    ``edge``는 시그널 다음날 기대수익에 얹는 인위적 우위로, 0이면
    시그널에 알파가 전혀 없는 귀무가설 데이터가 된다.
    """
    rng = np.random.default_rng(seed)

    # 거래일 캘린더 (주말 제외)
    end = pd.Timestamp(end_date)
    cal = pd.bdate_range(end=end, periods=n_days)
    dates = cal.to_numpy(dtype="datetime64[D]")

    sectors = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI"]
    prices: dict[str, PriceSeries] = {}
    sym_sector: dict[str, str] = {}

    for k in range(n_symbols):
        sym = f"SYN{k:03d}"
        sym_sector[sym] = sectors[k % len(sectors)]
        vol = daily_vol * float(rng.uniform(0.6, 1.8))     # 종목별 변동성 차이
        ret = rng.normal(drift, vol, n_days)
        close = 100.0 * np.exp(np.cumsum(ret))
        # 시가는 전일 종가에 갭을 얹고, 고저는 장중 변동으로 만든다.
        gap = rng.normal(0.0, vol * 0.45, n_days)
        open_ = np.empty(n_days)
        open_[0] = close[0] * (1.0 - ret[0])
        open_[1:] = close[:-1] * np.exp(gap[1:])
        span = np.abs(rng.normal(0.0, vol * 0.9, n_days))
        high = np.maximum(open_, close) * (1.0 + span)
        low = np.minimum(open_, close) * (1.0 - span)
        prices[sym] = PriceSeries(sym, dates, open_, high, low, close)

    # 시그널: 하루 평균 signals_per_day건. ATR 워밍업과 청산 여유를 위해
    # 앞 30봉·뒤 25봉은 비워 둔다.
    syms = list(prices)
    sigs: list[Signal] = []
    for i in range(30, n_days - 25):
        k = rng.poisson(signals_per_day)
        for sym in rng.choice(syms, size=min(k, len(syms)), replace=False):
            lab = ALL_LABELS[int(rng.integers(0, len(ALL_LABELS)))]
            sigs.append(
                Signal(
                    symbol=str(sym),
                    signal_date=dates[i],
                    label=lab,
                    confidence=float(np.clip(rng.normal(0.53, 0.05), 0.3, 0.95)),
                    sector=sym_sector[str(sym)],
                    source="synthetic",
                )
            )

    if edge:
        # 시그널 다음날 종가/고가에 우위를 얹는다 — 그리드가 신호에 반응하는지
        # 확인하는 용도. 실측이 아님을 분명히 하려고 별도 분기로 둔다.
        for s in sigs:
            ps = prices[s.symbol]
            j = ps.next_index_after(s.signal_date)
            if 0 <= j < ps.dates.size:
                bump = 1.0 + edge
                ps.close[j] *= bump
                ps.high[j] = max(ps.high[j] * bump, ps.close[j])

    return Dataset(
        signals=sorted(sigs, key=lambda s: (s.signal_date, s.symbol)),
        prices=prices,
        calendar=dates,
        note=f"합성 (seed={seed}, edge={edge})",
    )
