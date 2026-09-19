"""청산 시뮬레이션 엔진.

입력은 시그널 한 건 + 그 종목의 일봉이고, 출력은 "언제 어떤 가격에 빠져
나왔는가"다. 기존 라벨러와 같은 삼중 배리어를 쓰되, TASK.md Phase 1이
요구한 세 가지를 더한다: 라벨별 보유상한, ATR 기반 배리어, 갭을 반영한
체결가.

진입 규칙은 기존과 동일하게 **시그널 다음 거래일 시가**다. 여기를 바꾸면
과거 라벨과 비교가 불가능해지므로 건드리지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from stock_auto.exit.atr import DEFAULT_PERIOD, atr_wilder
from stock_auto.horizons import MFE_MAE_WINDOW, normalize_label

#: 반사실 비교용 단순보유 지평. analysis_2026-09-18 §3-1의 "D+2 단순보유"가 여기 들어 있다.
DPLUS_HORIZONS: tuple[int, ...] = (1, 2, 3, 5)

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_TO = "TO"          # timeout — 보유상한 도달
EXIT_NONE = "NA"        # 시뮬레이션 불가 (데이터 부족 등)


@dataclass(slots=True)
class PriceSeries:
    """한 종목의 일봉. 반복 조회가 많아 numpy 배열로 눕혀 둔다."""

    symbol: str
    dates: np.ndarray        # datetime64[D], 오름차순
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray | None = None

    def __post_init__(self) -> None:
        n = self.dates.size
        for name in ("open", "high", "low", "close"):
            if getattr(self, name).size != n:
                raise ValueError(f"{self.symbol}: {name} 길이가 dates와 다르다")
        if n > 1 and not np.all(self.dates[1:] > self.dates[:-1]):
            raise ValueError(f"{self.symbol}: dates가 오름차순이 아니거나 중복이 있다")

    def ensure_atr(self, period: int = DEFAULT_PERIOD) -> None:
        """ATR을 아직 안 붙였으면 계산해 둔다. 이미 있으면 그대로."""
        if self.atr is None:
            self.atr = atr_wilder(self.high, self.low, self.close, period)

    def index_of(self, date: np.datetime64) -> int:
        """해당 날짜 봉의 위치. 없으면 -1."""
        i = int(np.searchsorted(self.dates, date, side="left"))
        if i < self.dates.size and self.dates[i] == date:
            return i
        return -1

    def next_index_after(self, date: np.datetime64) -> int:
        """해당 날짜 **다음** 거래일 봉의 위치. 없으면 -1.

        휴장일이 끼어도 searchsorted가 알아서 다음 거래일로 보낸다.
        """
        i = int(np.searchsorted(self.dates, date, side="right"))
        return i if i < self.dates.size else -1


@dataclass(slots=True)
class Signal:
    """청산 시뮬레이션에 필요한 최소 시그널 정보.

    스코어링 계층이 뱉는 컬럼은 훨씬 많지만, 청산은 이것만 보면 된다.
    의존을 좁게 유지해야 선정 로직이 바뀌어도 이 계층이 안 깨진다.
    """

    symbol: str
    signal_date: np.datetime64   # 판단 근거가 된 봉의 날짜
    label: str                   # 단타 / 중단기 / 스윙
    confidence: float = float("nan")
    sector: str = ""
    source: str = ""             # batch / monitor 등


@dataclass(slots=True)
class ExitResult:
    """시그널 한 건의 청산 결과."""

    symbol: str
    label: str
    confidence: float
    sector: str
    source: str
    signal_date: np.datetime64
    entry_date: np.datetime64 | None
    entry_price: float
    exit_type: str
    exit_date: np.datetime64 | None
    exit_price: float
    return_pct: float
    hold_days: int
    mfe_pct: float          # 진입 후 20일 최대 유리 변동
    mae_pct: float          # 진입 후 20일 최대 불리 변동
    mfe_hold_pct: float = float("nan")   # **보유 구간 안에서의** 최대 유리 변동
    mae_hold_pct: float = float("nan")   # 보유 구간 안에서의 최대 불리 변동
    dplus: dict[int, float] = field(default_factory=dict)  # 단순보유 반사실
    note: str = ""

    @property
    def is_win(self) -> bool:
        return self.return_pct > 0

    @property
    def simulated(self) -> bool:
        return self.exit_type != EXIT_NONE

    @property
    def capture_ratio(self) -> float:
        """MFE 포착률 = 실현수익 / MFE20.

        "지금 청산 룰이 얼마나 못 먹는지"를 한 숫자로 보는 지표
        (analysis_2026-09-18 §4 P4). MFE가 0 이하면(= 진입 후 한 번도
        플러스가 안 난 건) 정의되지 않으므로 NaN.

        주의: 분모가 **고정 20일**이라 보유상한이 짧은 라벨(단타 D+2)은
        구조적으로 낮게 나온다. 라벨 간 비교에는
        :attr:`hold_capture_ratio`를 함께 봐야 한다.
        """
        if not np.isfinite(self.mfe_pct) or self.mfe_pct <= 0:
            return float("nan")
        return self.return_pct / self.mfe_pct

    @property
    def hold_capture_ratio(self) -> float:
        """보유 구간 내 포착률 = 실현수익 / (보유 구간 MFE).

        :attr:`capture_ratio`가 "지평이 짧아서 못 먹었나"를 섞어서 보여 주는
        반면, 이 값은 **자기 보유 구간 안에서 잘 나왔는가**만 본다. 둘을
        같이 보면 진단이 갈린다 —
          · 보유내 포착 높음 + MFE20 포착 낮음 → 보유상한이 짧다
          · 보유내 포착 낮음                   → 청산 타이밍 자체가 나쁘다
        """
        if not np.isfinite(self.mfe_hold_pct) or self.mfe_hold_pct <= 0:
            return float("nan")
        return self.return_pct / self.mfe_hold_pct


def _apply_slippage(price: float, bps: float, side: str) -> float:
    """편도 슬리피지. 매수는 비싸게, 매도는 싸게 — 항상 불리한 쪽으로."""
    if bps <= 0:
        return price
    adj = bps / 10_000.0
    return price * (1.0 + adj) if side == "buy" else price * (1.0 - adj)


def _barrier_prices(
    entry: float, atr_at_signal: float, le: "LabelExit"
) -> tuple[float, float]:
    """진입가 기준 SL/TP 절대가격. ATR 모드인데 ATR이 없으면 NaN을 돌려준다."""
    if le.sl_mode == "pct":
        sl = entry * (1.0 - le.sl_value / 100.0)
    else:
        sl = entry - le.sl_value * atr_at_signal
    if le.tp_mode == "pct":
        tp = entry * (1.0 + le.tp_value / 100.0)
    else:
        tp = entry + le.tp_value * atr_at_signal
    return sl, tp


def simulate_exit(sig: Signal, px: PriceSeries, cfg: "ExitConfig") -> ExitResult:
    """시그널 한 건을 청산까지 돌린다.

    진입 = 시그널 다음 거래일 시가. 이후 보유상한까지 매 봉에서 배리어를
    본다. 같은 봉에서 SL·TP가 동시에 닿으면 일봉만으로는 선후를 알 수 없어
    ``cfg.tie_breaker``(기본 SL)를 따른다.
    """
    le = cfg.for_label(sig.label)
    nan = float("nan")

    def _fail(note: str) -> ExitResult:
        return ExitResult(
            symbol=sig.symbol, label=normalize_label(sig.label),
            confidence=sig.confidence, sector=sig.sector, source=sig.source,
            signal_date=sig.signal_date, entry_date=None, entry_price=nan,
            exit_type=EXIT_NONE, exit_date=None, exit_price=nan,
            return_pct=nan, hold_days=0, mfe_pct=nan, mae_pct=nan, note=note,
        )

    sig_idx = px.index_of(sig.signal_date)
    entry_idx = px.next_index_after(sig.signal_date)
    if entry_idx < 0:
        return _fail("진입일 봉 없음 (시그널이 데이터 마지막 날 이후)")

    entry_raw = float(px.open[entry_idx])
    if not np.isfinite(entry_raw) or entry_raw <= 0:
        return _fail("진입가 이상치")
    entry = _apply_slippage(entry_raw, cfg.slippage_bps, "buy")

    # ATR은 시그널 봉까지만. 시그널 봉을 못 찾으면 진입 직전 봉으로 대체한다.
    atr_ref_idx = sig_idx if sig_idx >= 0 else entry_idx - 1
    atr_at_signal = nan
    if cfg.needs_atr():
        px.ensure_atr()
        assert px.atr is not None
        if atr_ref_idx < 0:
            return _fail("ATR 기준 봉 없음")
        atr_at_signal = float(px.atr[atr_ref_idx])
        if not np.isfinite(atr_at_signal) or atr_at_signal <= 0:
            return _fail("ATR 워밍업 부족")

    sl_price, tp_price = _barrier_prices(entry, atr_at_signal, le)

    last_idx = min(entry_idx + le.hold_days - 1, px.dates.size - 1)
    if last_idx < entry_idx:
        return _fail("보유 구간 없음")

    # --- MFE / MAE (고정 20일 창, 청산과 무관하게 관찰) --------------------
    obs_end = min(entry_idx + MFE_MAE_WINDOW - 1, px.dates.size - 1)
    win_high = px.high[entry_idx : obs_end + 1]
    win_low = px.low[entry_idx : obs_end + 1]
    mfe = float((np.max(win_high) / entry - 1.0) * 100.0) if win_high.size else nan
    mae = float((np.min(win_low) / entry - 1.0) * 100.0) if win_low.size else nan

    # --- 단순보유 반사실 (D+N 종가) ---------------------------------------
    dplus: dict[int, float] = {}
    for n in DPLUS_HORIZONS:
        j = entry_idx + n - 1
        if j < px.dates.size:
            dplus[n] = float((px.close[j] / entry - 1.0) * 100.0)

    # --- 배리어 워크 -------------------------------------------------------
    be_armed = False           # 본전 트레일 발동 여부
    eff_sl = sl_price
    exit_type = EXIT_TO
    exit_idx = last_idx
    exit_raw = float(px.close[last_idx])
    tp_leg_ret: float | None = None   # 부분익절로 먼저 실현한 몫

    for i in range(entry_idx, last_idx + 1):
        o, h, l, c = (
            float(px.open[i]), float(px.high[i]),
            float(px.low[i]), float(px.close[i]),
        )

        if cfg.close_based:
            hit_sl, hit_tp = c <= eff_sl, c >= tp_price
            sl_fill, tp_fill = c, c
        else:
            hit_sl, hit_tp = l <= eff_sl, h >= tp_price
            if cfg.gap_aware_fill:
                # 갭은 항상 불리하게만 반영한다.
                #  · SL: 시가가 손절가를 뚫고 열렸으면 그 시가가 체결가다.
                #  · TP: 시가가 목표가를 뚫고 열려도 목표가까지만 인정한다
                #        (갭업 이득을 백테스트가 가져가지 않게).
                # TASK.md는 TP도 min(목표가, 시가)로 적었는데, 그대로 쓰면
                # 갭이 없는 평범한 날(시가 < 목표가)에 TP 체결가가 시가로
                # 내려앉아 익절 수익이 통째로 사라진다. 갭으로 관통한
                # 경우에만 적용하는 것이 그 문장의 의도로 보고 그렇게 구현했다.
                sl_fill = min(eff_sl, o)
                tp_fill = tp_price
            else:
                sl_fill, tp_fill = eff_sl, tp_price

        if hit_sl and hit_tp:
            # 일봉으로는 선후 판정 불가 → 설정을 따른다(기본 SL, 보수적).
            if cfg.tie_breaker == "sl":
                hit_tp = False
            else:
                hit_sl = False

        if hit_sl:
            exit_type, exit_idx, exit_raw = EXIT_SL, i, sl_fill
            break

        if hit_tp:
            if cfg.partial_tp is None:
                exit_type, exit_idx, exit_raw = EXIT_TP, i, tp_fill
                break
            # 부분익절: 일부만 실현하고 잔량은 계속 끌고 간다.
            # 잔량 손절은 본전으로 올린다 — 익절 후 원금을 까먹지 않기 위해.
            if tp_leg_ret is None:
                filled = _apply_slippage(tp_fill, cfg.slippage_bps, "sell")
                tp_leg_ret = (filled / entry - 1.0) * 100.0
                eff_sl = max(eff_sl, entry)
                exit_type = EXIT_TP

        # 본전 트레일: 발동 봉 **다음 봉부터** 적용한다. 같은 봉 안에서
        # 고가가 먼저인지 저가가 먼저인지 모르는데 당겨 쓰면 낙관 편향이 된다.
        if cfg.trail_be_atr is not None and not be_armed:
            trigger = entry + cfg.trail_be_atr * atr_at_signal
            reached = (c >= trigger) if cfg.close_based else (h >= trigger)
            if reached:
                be_armed = True
                eff_sl = max(eff_sl, entry)

    # 보유 구간(진입~청산) 안에서의 MFE/MAE. 청산 타이밍 품질을 보는 분모다.
    hold_high = px.high[entry_idx : exit_idx + 1]
    hold_low = px.low[entry_idx : exit_idx + 1]
    mfe_hold = float((np.max(hold_high) / entry - 1.0) * 100.0) if hold_high.size else nan
    mae_hold = float((np.min(hold_low) / entry - 1.0) * 100.0) if hold_low.size else nan

    exit_fill = _apply_slippage(exit_raw, cfg.slippage_bps, "sell")
    leg_ret = (exit_fill / entry - 1.0) * 100.0

    if tp_leg_ret is not None:
        # 부분익절분 + 잔량분 가중평균. exit_type은 잔량이 어떻게 끝났는지로 덮어쓴다.
        w = cfg.partial_tp or 0.0
        ret = w * tp_leg_ret + (1.0 - w) * leg_ret
        if exit_idx == last_idx and exit_type == EXIT_TP:
            exit_type = "TP+TO"
        elif exit_type == EXIT_SL:
            exit_type = "TP+BE"
    else:
        ret = leg_ret

    return ExitResult(
        symbol=sig.symbol,
        label=normalize_label(sig.label),
        confidence=sig.confidence,
        sector=sig.sector,
        source=sig.source,
        signal_date=sig.signal_date,
        entry_date=px.dates[entry_idx],
        entry_price=entry,
        exit_type=exit_type,
        exit_date=px.dates[exit_idx],
        exit_price=exit_fill,
        return_pct=ret,
        hold_days=exit_idx - entry_idx + 1,
        mfe_pct=mfe,
        mae_pct=mae,
        mfe_hold_pct=mfe_hold,
        mae_hold_pct=mae_hold,
        dplus=dplus,
    )


def simulate_all(
    signals: Iterable[Signal],
    prices: dict[str, PriceSeries],
    cfg: "ExitConfig",
) -> list[ExitResult]:
    """시그널 묶음을 한 번에 돌린다. 가격 데이터가 없는 종목은 NA로 남긴다."""
    out: list[ExitResult] = []
    for sig in signals:
        px = prices.get(sig.symbol)
        if px is None:
            out.append(
                ExitResult(
                    symbol=sig.symbol, label=normalize_label(sig.label),
                    confidence=sig.confidence, sector=sig.sector, source=sig.source,
                    signal_date=sig.signal_date, entry_date=None,
                    entry_price=float("nan"), exit_type=EXIT_NONE, exit_date=None,
                    exit_price=float("nan"), return_pct=float("nan"), hold_days=0,
                    mfe_pct=float("nan"), mae_pct=float("nan"),
                    note="가격 데이터 없음",
                )
            )
            continue
        out.append(simulate_exit(sig, px, cfg))
    return out


# 순환 import 회피용 — 타입 힌트 문자열로만 참조하고 런타임엔 여기서 가져온다.
from stock_auto.exit.config import ExitConfig, LabelExit  # noqa: E402
