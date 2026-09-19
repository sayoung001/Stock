"""라벨별 진단·조언 생성기.

리포트에 숫자만 있으면 매번 사람이 같은 해석을 반복해야 한다. 여기서는
**규칙 기반**으로 해석을 붙인다. LLM을 쓰지 않는 이유는 두 가지다 —
(a) 같은 입력에 같은 답이 나와야 리포트 간 비교가 되고, (b) 근거 없는
문장이 섞이면 안 된다.

조언은 라벨(단타/중단기/스윙)마다 다르다. 각 지평이 구조적으로 다른
문제를 겪기 때문이다:

- **단타** — 목표 도달 전에 시간이 끝난다. 미결착(TO) 비율이 핵심 병목이고,
  `추천_읽는_법 §1-④`이 지적한 "구조적으로 불리하다"가 이것이다.
- **중단기** — 손절폭과 보유기간의 균형이 문제. 실적 발표가 보유기간에
  걸리는 조합도 이 지평에서 생긴다.
- **스윙** — MFE를 얼마나 먹고 나오는지(포착률)가 성패를 가른다.
  변동성이 큰 종목에 짧은 손절을 걸면 오르는 길목에서 털린다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from stock_auto.exit.metrics import Metrics
from stock_auto.horizons import DAYTRADE, SWING, SWING_SHORT

#: analysis_2026-09-18 §5 목표치.
TARGET_PF = 1.3
TARGET_PAYOFF = 1.5
TARGET_EXPECTANCY = 0.5
TARGET_CAPTURE = 0.50


@dataclass(slots=True)
class LabelDiagnosis:
    """한 라벨의 진단 결과. 리포트가 문장으로 렌더한다."""

    label: str
    verdict: str                                  # 한 줄 판정
    findings: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = [f"**판정**: {self.verdict}"]
        if self.findings:
            out.append("")
            out.append("관찰:")
            out.extend(f"- {f}" for f in self.findings)
        if self.actions:
            out.append("")
            out.append("조치 후보:")
            out.extend(f"- {a}" for a in self.actions)
        return "\n".join(out)


def _pct(v: float, digits: int = 1) -> str:
    return "—" if not np.isfinite(v) else f"{v:.{digits}f}%"


def _num(v: float, digits: int = 2) -> str:
    return "—" if not np.isfinite(v) else f"{v:.{digits}f}"


def diagnose_label(label: str, m: Metrics, *, min_n: int = 100) -> LabelDiagnosis:
    """라벨 하나를 진단한다. 표본이 부족하면 판정을 보류한다."""
    d = LabelDiagnosis(label=label, verdict="")

    if m.n == 0:
        d.verdict = "시그널 없음 — 판정 불가."
        return d

    # --- 표본 크기가 먼저다 ------------------------------------------------
    if m.n < min_n:
        d.verdict = (
            f"⚠️ **표본 부족 ({m.n}건 / 기준 {min_n}건)** — 아래 숫자는 방향 "
            f"힌트이지 결론이 아니다. {min_n - m.n}건 더 필요."
        )
        d.actions.append(
            f"이 라벨의 파라미터는 legacy 유지. 표본 {min_n}건을 채운 뒤 재평가한다."
        )
    else:
        hits = m.meets_targets()
        n_ok = sum(hits.values())
        if n_ok == len(hits):
            d.verdict = f"✅ 목표 4개 전부 통과 (n={m.n}). 채택 후보."
        elif n_ok >= 2:
            d.verdict = (
                f"🟡 목표 {n_ok}/{len(hits)} 통과 (n={m.n}). "
                f"미달: {', '.join(k for k, v in hits.items() if not v)}."
            )
        else:
            d.verdict = (
                f"❌ 목표 {n_ok}/{len(hits)} 통과 (n={m.n}). "
                f"이 설정으로 이 라벨을 실행하면 안 된다."
            )

    # --- 공통 관찰 --------------------------------------------------------
    d.findings.append(
        f"승률 {_pct(m.win_rate)} · PF {_num(m.profit_factor)} · "
        f"건당 {_pct(m.expectancy, 2)} · 손익비 {_num(m.payoff_ratio)}"
    )
    d.findings.append(
        f"청산 구성 TP {m.n_tp} / SL {m.n_sl} / 미결착 {m.n_to} · "
        f"평균보유 {_num(m.avg_hold_days, 1)}일 · MFE20포착 "
        f"{_pct(m.capture_ratio * 100, 0) if np.isfinite(m.capture_ratio) else '—'}"
        f" · 보유내포착 "
        f"{_pct(m.hold_capture_ratio * 100, 0) if np.isfinite(m.hold_capture_ratio) else '—'}"
    )

    # 두 포착률의 대비가 진단을 갈라 준다. 보유 구간 안에서는 잘 나왔는데
    # MFE20 기준으로 낮으면 문제는 타이밍이 아니라 **지평의 길이**다.
    if np.isfinite(m.capture_ratio) and np.isfinite(m.hold_capture_ratio):
        if m.hold_capture_ratio >= 0.5 and m.capture_ratio < TARGET_CAPTURE:
            d.findings.append(
                f"보유 구간 안에서는 {_pct(m.hold_capture_ratio * 100, 0)}를 먹는데 "
                f"20일 기준으로는 {_pct(m.capture_ratio * 100, 0)}다 → **청산 타이밍이 "
                f"아니라 보유상한이 병목**이다."
            )
            d.actions.append(
                "보유상한을 늘리는 쪽을 먼저 본다. SL/TP 폭 조정은 그다음이다."
            )
        elif m.hold_capture_ratio < 0.3:
            d.findings.append(
                f"보유 구간 안에서도 {_pct(m.hold_capture_ratio * 100, 0)}밖에 못 먹는다 "
                f"→ 자기 창 안에서 고점을 지나쳐 나오고 있다. **청산 타이밍 문제**."
            )
            d.actions.append(
                "부분익절(`partial_tp`) 또는 본전 트레일(`trail_be_atr`)로 "
                "되돌림을 막는 쪽을 그리드에서 확인한다."
            )

    # 역손익비 — analysis §3-2의 핵심 진단
    if np.isfinite(m.payoff_ratio) and m.payoff_ratio < 1.0:
        d.findings.append(
            f"**역손익비** — 평균 손실({_pct(m.avg_loss, 2)})이 평균 이익"
            f"({_pct(m.avg_win, 2)})보다 크다. 승률이 50%를 넘어야 본전인 구조."
        )
        d.actions.append(
            "TP를 넓히거나 SL을 좁히기 전에, 먼저 SL 체결가가 목표보다 "
            "얼마나 나쁘게 잡히는지(갭 슬리피지) 확인한다."
        )

    # 손익분기 승률 대비
    if np.isfinite(m.payoff_ratio) and m.payoff_ratio > 0:
        be = 100.0 / (1.0 + m.payoff_ratio)
        margin = m.win_rate - be
        d.findings.append(
            f"손익분기 승률 {_pct(be)} vs 실제 {_pct(m.win_rate)} "
            f"({margin:+.1f}%p 여유). 왕복 거래비용 30bp를 더하면 여유가 더 줄어든다."
        )
        if margin < 0:
            d.actions.append(
                "손익비 기준으로 승률이 미달이다. 보유상한을 늘려 TP 도달 기회를 "
                "주는 쪽과, SL을 넓혀 노이즈 청산을 줄이는 쪽을 그리드로 비교한다."
            )

    # MFE 포착률
    if np.isfinite(m.capture_ratio) and m.capture_ratio < TARGET_CAPTURE:
        d.findings.append(
            f"MFE 포착률 {_pct(m.capture_ratio * 100, 0)} — 20일 안에 생긴 "
            f"유리한 변동의 절반도 못 먹고 나온다."
        )

    # 반사실 — 단순보유가 더 나은가
    if m.dplus_total:
        best_n = max(m.dplus_total, key=lambda k: m.dplus_total[k])
        best_v = m.dplus_total[best_n]
        if best_v > m.total_return:
            d.findings.append(
                f"**D+{best_n} 단순보유가 더 좋다** ({_pct(best_v)} vs "
                f"배리어 {_pct(m.total_return)}). 청산 규칙이 수익을 깎고 있다는 직접 증거."
            )
            d.actions.append(
                f"이 라벨의 보유상한을 D+{best_n} 방향으로 늘리는 셀을 그리드에서 확인한다."
            )

    # --- 라벨별 고유 진단 ---------------------------------------------------
    to_rate = m.n_to / m.n if m.n else 0.0
    sl_rate = m.n_sl / m.n if m.n else 0.0

    if label == DAYTRADE:
        if to_rate >= 0.35:
            d.findings.append(
                f"미결착 비율 {to_rate:.0%} — 목표에 닿기 전에 시간이 끝나는 "
                f"전형적인 단타 병목이다."
            )
            d.actions.append(
                "목표의 60% 지점 부분익절(`partial_tp`)을 그리드 축에 넣어 본다. "
                "단타는 목표 전액을 기다리는 것이 구조적으로 불리하다."
            )
        if m.avg_hold_days <= 1.05 and m.n_to > 0:
            d.actions.append(
                "평균보유가 1일에 붙어 있다 — 보유상한이 사실상 유일한 청산 사유다. "
                "D+2를 먼저 시험한다."
            )
    elif label == SWING_SHORT:
        if sl_rate >= 0.40:
            d.findings.append(
                f"SL 비율 {sl_rate:.0%} — 중단기 지평에서 손절이 절반 가까이면 "
                f"손절폭이 변동성 대비 좁다는 신호다."
            )
            d.actions.append("SL을 ATR 배수(1.5→2.0)로 넓히는 셀과 종가 판정을 비교한다.")
        d.actions.append(
            "보유기간(D+3 이상)이 실적 발표일을 넘는 조합을 점검한다. "
            "실적 D-4~D-7 추천은 보유 중 발표를 맞는다."
        )
    elif label == SWING:
        if np.isfinite(m.capture_ratio) and m.capture_ratio < TARGET_CAPTURE:
            d.actions.append(
                "스윙은 포착률이 성패를 가른다. 본전 트레일(`trail_be_atr`)로 "
                "손절 리스크를 줄이면서 보유상한을 늘리는 조합을 본다."
            )
        if m.avg_hold_days < 3:
            d.findings.append(
                f"평균보유 {_num(m.avg_hold_days, 1)}일 — 스윙 라벨인데 실제로는 "
                f"단타로 청산되고 있다. 라벨과 청산이 어긋난 상태."
            )
            d.actions.append("스윙 보유상한을 D+5 이상으로 올리는 셀을 우선 확인한다.")

    if not d.actions:
        d.actions.append("현 설정에서 이 라벨의 구조적 문제는 발견되지 않았다.")
    return d


def advise_label(label: str, m: Metrics, *, min_n: int = 100) -> str:
    """리포트에 꽂을 markdown 조각."""
    return diagnose_label(label, m, min_n=min_n).render()


def advise_overall(
    overall: Metrics, labelled: dict[str, Metrics], *, min_n: int = 100
) -> str:
    """전체 한 줄 판정 + 라벨 간 비교."""
    lines: list[str] = []

    if overall.n == 0:
        return "시뮬레이션된 건이 없다. 시그널·가격 데이터 연결을 먼저 확인한다."

    if overall.n < min_n:
        lines.append(
            f"⚠️ **현재 표본으로는 결론 불가** — {overall.n}건 (기준 n ≥ {min_n}). "
            f"{min_n - overall.n}건 더 필요하고, 그때까지 legacy 설정을 유지한다."
        )
    else:
        hits = overall.meets_targets()
        n_ok = sum(hits.values())
        if n_ok == len(hits):
            lines.append(
                f"✅ 전체 기준 통과 (n={overall.n}) — PF {_num(overall.profit_factor)}, "
                f"기대값 {_pct(overall.expectancy, 2)}, 손익비 "
                f"{_num(overall.payoff_ratio)}."
            )
        else:
            lines.append(
                f"❌ 전체 목표 {n_ok}/{len(hits)} 통과 (n={overall.n}). "
                f"미달: {', '.join(k for k, v in hits.items() if not v)}."
            )

    # 라벨 간 격차 — 어느 지평이 전체를 끌어내리는가
    scored = {
        k: v for k, v in labelled.items()
        if v.n > 0 and np.isfinite(v.expectancy)
    }
    if len(scored) >= 2:
        best = max(scored, key=lambda k: scored[k].expectancy)
        worst = min(scored, key=lambda k: scored[k].expectancy)
        if best != worst:
            lines.append("")
            lines.append(
                f"라벨 간 격차: **{best}** {_pct(scored[best].expectancy, 2)}/건 "
                f"(n={scored[best].n}) ↔ **{worst}** "
                f"{_pct(scored[worst].expectancy, 2)}/건 (n={scored[worst].n})."
            )
            if scored[worst].expectancy < 0 and scored[best].expectancy > 0:
                lines.append(
                    f"→ {worst} 지평이 전체를 끌어내리고 있다. 세 라벨에 같은 청산 "
                    f"규칙을 쓰는 것이 원인인지 라벨별 그리드에서 확인한다."
                )
    return "\n".join(lines)
