# 실행 가이드 — 수정본 돌리기 & 백테스트 동시 확인

작성 2026-09-19 · 대상 브랜치 `ssh_upload_stock`(수정본) · `backtest_100d`(백테스트)

이 문서 하나만 보고 SSH에서 끝까지 갈 수 있게 썼다. 위에서부터 순서대로
따라가면 된다.

---

## 0. 시작 전에 알아야 할 것 두 가지

### ① 이 작업은 종목 선정을 건드리지 않았다

`TASK.md` 원칙 1이 "선정(스코어링) 로직은 건드리지 않는다"이다. 그래서
이번 코드는 **기존 파이프라인 파일을 한 줄도 수정하지 않는다.** 시그널
CSV를 읽고 결과 파일을 쓸 뿐이라, **프로덕션 배치가 도는 중에 병렬로
돌려도 안전하다.**

### ② 이 레포에 `stock_auto/` 원본이 없다

확인해 보니 이 저장소에는 코인 선물 봇 코드와 설계 문서만 있고, 실제
`stock_auto/` 패키지(51개 모듈)가 없다. 그래서 청산·백테스트 계층을
**독립 패키지로 새로 만들고** 문서화된 데이터 계약(`signals.csv` + 일봉)으로만
연결했다. 자세한 것은 `docs/codemap_exit.md` §0.

**그 결과 이번에 못 한 것**: Phase 3(triage 입력 확장), Phase 4(대시보드
HTML 타일). 둘 다 원본 소스가 있어야 한다. 원본을 이 레포에 올려 주면
이어서 할 수 있고, 지금 만든 계층은 그대로 붙는다.

---

## 1. 브랜치 두 개 — 무엇을 어디서 돌리나

```
ssh_upload_stock          ← 수정본. SSH 서버에서 실제 데이터로 돌린다
   │  청산 엔진 + 전 구간 그리드 + 실행 필터
   │
   └─► backtest_100d      ← 백테스트 전용. 동시에 따로 돌린다
          + run_100d.py (최근 100 거래일)
```

| | `ssh_upload_stock` | `backtest_100d` |
|---|---|---|
| 하는 일 | 파라미터를 **정한다** | 정한 값의 **최근 성적을 본다** |
| 구간 | 시그널 전 구간 | 최근 100 거래일 |
| 주 명령 | `run_exit_backtest --grid` | `run_100d` |
| 결과 신뢰도 | 채택 근거로 **쓸 수 있음** | 참고용. 채택 근거로 **쓰면 안 됨** |

### ⭐ 1000일을 보려면 `backtest_100d` 브랜치를 쓴다

이 브랜치의 두 러너는 `signals.csv`를 **읽는다.** 그 파일에는 운영 시작 뒤
쌓인 시그널만 있어서, 2주치(24건)뿐이면 `--window 1000`을 줘도 결과는
여전히 24건이다. **표본의 천장이 가격 데이터 길이가 아니라 기록 이력이다.**

1000일을 보려면 과거 일봉에 스코어링 모델을 다시 돌려 시그널을
**생성**해야 한다. 그 러너는 `backtest_100d` 브랜치에 있다:

```bash
git checkout backtest_100d
python -m stock_auto.backtest.run_model_backtest --days 1000 --price-dir ~/price_cache
```

24종목 × 1000일이면 시그널이 **800~900건** 나온다. 자세한 것은
`docs/BACKTEST_LONG_RUN.md`와 `docs/MODEL_REPRODUCTION.md`(가정 14개).

---

## 2. 설치 (SSH, 한 번만)

```bash
cd ~
git clone <레포 URL> stock_exit          # 이미 있으면 생략
cd stock_exit
git checkout ssh_upload_stock

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install pandas numpy pytest
pip install finance-datareader yfinance   # 일봉 다운로드용 (캐시만 쓸 거면 생략 가능)
```

### 설치 확인 — 데이터 없이 30초

```bash
python -m pytest tests/ -q
# ssh_upload_stock: 87 passed
# backtest_100d:    96 passed (100일 러너 테스트 9건 추가)

python -m stock_auto.backtest.run_exit_backtest --synthetic --quiet
# experiments/results/exit_backtest.md 가 생기면 성공
```

> `--synthetic`은 **합성 데이터**다. 리포트 맨 위에 경고가 붙는다.
> 배선과 실행시간 확인용이지 성과 숫자가 아니다.

---

## 3. 두 브랜치를 동시에 돌리기

같은 서버에서 두 작업 디렉토리를 따로 두는 게 가장 깔끔하다.

```bash
# ① 수정본 (기존 디렉토리 그대로)
cd ~/stock_exit
git checkout ssh_upload_stock

# ② 백테스트용 디렉토리를 따로 만든다
cd ~
git clone ~/stock_exit stock_bt
cd stock_bt
git checkout backtest_100d
source ~/stock_exit/.venv/bin/activate     # 가상환경은 공유해도 된다
```

**일봉 캐시는 공유하는 게 좋다** — 같은 종목을 두 번 받을 이유가 없다.

```bash
mkdir -p ~/price_cache

# 터미널 A — 수정본: 전 구간 그리드
cd ~/stock_exit
python -m stock_auto.backtest.run_exit_backtest \
    --signals ~/stock_auto/data/tracking/signals.csv \
    --price-dir ~/price_cache \
    --grid \
    --out-dir ~/results/full

# 터미널 B — 백테스트: 최근 100 거래일
cd ~/stock_bt
python -m stock_auto.backtest.run_100d \
    --signals ~/stock_auto/data/tracking/signals.csv \
    --price-dir ~/price_cache \
    --out-dir ~/results/100d
```

> **주의**: 캐시가 비어 있을 때 두 작업을 **동시에** 시작하면 같은 파일에
> 같이 쓸 수 있다. 처음 한 번은 A를 먼저 돌려 캐시를 채우고, 끝난 뒤 B를
> 시작한다. 그 뒤로는 동시에 돌려도 된다.

`nohup`으로 띄워 두고 나중에 봐도 된다:

```bash
nohup python -m stock_auto.backtest.run_exit_backtest \
    --signals ... --price-dir ~/price_cache --grid \
    --out-dir ~/results/full > ~/results/full.log 2>&1 &
tail -f ~/results/full.log
```

---

## 4. 얼마나 걸리나 — 실측

4 vCPU 기준 실측값이다. `docs/BACKTEST_100D.md` §3에 전체 표가 있다.

| 하려는 것 | 계산 시간 |
|---|---|
| `run_100d` 기본 | **1초 미만** |
| `run_100d --grid` (600셀) | **5~8초** |
| `run_exit_backtest --grid` 1년치 | **15~20초** |
| `--with-partial-tp --with-trail-be` (1,800셀) | **1~3분** |
| 3년치 5,000건 × 600셀 | **90초** |

비례식은 `시간 ≈ 시그널 수 × 셀 수 × 30µs`. 선형이다.

**계산은 병목이 아니다.** 실제로 기다리는 시간은 일봉 다운로드다:

| | 첫 실행 | 두 번째부터 |
|---|---|---|
| 일봉 다운로드 (20종목) | 30초 ~ 2분 | **0초** (캐시) |
| 계산 | 1~20초 | 1~20초 |
| **합계** | **1~3분** | **10~30초** |

`--price-dir`를 반드시 주라는 이유가 이것이다.

---

## 5. 결과 읽는 법 — 단타 / 중단기 / 스윙

리포트는 **세 라벨을 항상 분리**한다. 합쳐 놓으면 "평균적으로 괜찮다"는
답만 나오고 정작 필요한 "어느 지평이 망가졌나"가 안 보이기 때문이다.

### 읽는 순서

**① 0장 판정** — 한 줄 결론 + 라벨 간 격차.

```
❌ 전체 목표 1/4 통과 (n=412). 미달: PF≥1.3, 손익비≥1.5, MFE포착≥50%.
라벨 간 격차: 스윙 +0.83%/건 (n=140) ↔ 단타 -0.41%/건 (n=138).
→ 단타 지평이 전체를 끌어내리고 있다.
```

**② 2장 라벨별** — 이 리포트의 핵심. 라벨마다 이렇게 나온다:

```
### 단타
판정: ❌ 목표 0/4 통과 (n=138).

관찰:
- 청산 구성 TP 12 / SL 41 / 미결착 85 · 평균보유 1.0일
  · MFE20포착 18% · 보유내포착 71%
- 보유 구간 안에서는 71%를 먹는데 20일 기준으로는 18%다
  → 청산 타이밍이 아니라 보유상한이 병목이다.
- D+2 단순보유가 더 좋다 (+27.0% vs 배리어 -4.1%).

조치 후보:
- 보유상한을 늘리는 쪽을 먼저 본다. SL/TP 폭 조정은 그다음이다.
```

**두 종류 포착률**이 진단을 갈라 준다:

| 보유내 포착 | MFE20 포착 | 해석 | 손댈 곳 |
|---|---|---|---|
| 높음 (≥50%) | 낮음 (<50%) | 자기 창 안에선 잘 먹는데 창이 짧다 | **보유상한** |
| 낮음 (<30%) | 낮음 | 자기 창 안에서도 고점을 지나쳐 나온다 | **부분익절 / 트레일** |
| 높음 | 높음 | 문제 없음 | — |

**③ 각 라벨의 반사실 표** — 배리어 청산 vs D+N 단순보유.

```
| 청산 방식          | 합계   | 건당   | 승 | 패 |
| 배리어 청산 (현설정) | -4.08% | -0.34% |  3 |  6 |
| D+2 단순보유        | +27.0% | +2.25% |  6 |  3 |
```

단순보유가 더 좋으면 **청산 규칙이 수익을 깎고 있다는 직접 증거**다.
`analysis_2026-09-18.md` §3-1이 손으로 한 계산을 자동화한 것이다.

**④ 3장 그리드 (수정본 브랜치만)** — 파라미터를 정하는 표.

정렬은 **test 구간 기대값** 기준이다. train 옆에 test를 나란히 붙여 둔
이유는 과적합을 눈으로 잡으라는 것이다:

```
| # | 보유 | SL    | TP    | 판정 | train n/기대값/PF | test n/기대값/PF |
| 1 | D+3  | 1.5ATR| 2.5ATR| 장중 | 240 / +1.10% / 1.9| 160 / +0.82% / 1.5|
| 2 | D+1  | 2%    | 3%    | 장중 | 240 / +2.40% / 3.1| 160 / -0.30% / 0.9|
```

2번처럼 **train만 좋고 test에서 무너지는 셀이 과적합**이다. 절대 고르지 않는다.

---

## 6. 표본이 부족하면 어떻게 되나

`TASK.md` 완료 기준은 `n ≥ 100`(검증 구간)이다. 못 채우면 코드가
**최적값을 고르지 않는다.** 리포트에 이렇게 찍힌다:

```
⚠️ 표본 부족 — 최적값을 고르지 않는다. 검증 구간 최대 43건으로
   기준 n ≥ 100에 미달. 57건 더 필요. 이 구간에서는 legacy 설정을 유지한다.
```

부족한 표본에서 1등을 집어 주는 것이 곧 과적합이므로, 그 경로를 코드에서
막아 두었다. **이 문구가 보이면 파라미터를 바꾸지 말고 표본을 더 쌓는다.**

---

## 7. 보조로 확인할 것

### ① 테스트가 통과하는가

```bash
python -m pytest tests/ -q     # ssh_upload_stock 87 / backtest_100d 96
```

특히 아래 네 건이 이번 변경의 핵심을 고정한다:

| 테스트 | 무엇을 지키나 |
|---|---|
| `test_legacy_is_one_day_hold_for_every_label` | legacy 재현 (D+1) |
| `test_gap_down_through_sl_fills_at_open_not_at_stop` | 갭다운 손실을 과소평가하지 않음 |
| `test_gap_up_through_tp_does_not_credit_the_gap` | 갭업 이득을 백테스트가 가져가지 않음 |
| `test_atr_barrier_uses_signal_bar_not_entry_bar` | 미래 데이터 누출 차단 |

### ② 시그널 CSV가 제대로 읽혔는가

실행 첫 줄에 나온다:

```
데이터: 시그널 412건 (2026-03-02 ~ 2026-09-17) · 종목 20 · 단타 138 · 중단기 134 · 스윙 140
```

- **건수가 0** → `--signals` 경로 확인
- **라벨이 한쪽에 쏠림** → 라벨 컬럼명을 못 읽은 것. §8 참고
- **`미분류`가 많음** → 라벨 값 표기가 예상과 다르다. §8 참고

### ③ 판정 불가 건이 많지 않은가

```
시뮬레이션 380건 (판정불가 32건)
```

판정불가가 10%를 넘으면 원인을 본다. `trades_*.csv`의 `note` 컬럼에 이유가 있다:

| note | 뜻 | 조치 |
|---|---|---|
| `가격 데이터 없음` | 그 종목 일봉을 못 받음 | 종목 코드 확인, 캐시 확인 |
| `진입일 봉 없음` | 시그널이 데이터 마지막 날 이후 | 정상 (최근 시그널) |
| `ATR 워밍업 부족` | 상장 직후 등 봉이 14개 미만 | 정상 |

### ④ legacy 대비가 말이 되는가

6장(또는 100일 리포트 3장)에서 현 설정이 legacy보다 나은지 본다.
**단, 여기가 좋아졌다는 것만으로 채택하면 안 된다.** 근거는 그리드의
walk-forward 결과다 — `TASK.md` Phase 5 마지막 항목이 명시한 원칙이다.

### ⑤ 필터가 무엇을 걸렀는가

```
전체 412건 → 실행 대상 291건 (차단 121건)
| 제외: 확신도   | 68 |
| 제외: 쿨다운   | 31 |
| 제외: 섹터한도 | 22 |
```

차단분도 그대로 시뮬레이션되므로, 필터 전/후 지표를 비교해
**그 필터가 옳았는지** 확인할 수 있다. 차단분 성과가 통과분보다 좋으면
필터를 완화해야 한다는 신호다.

---

## 8. 막혔을 때

| 증상 | 원인 | 조치 |
|---|---|---|
| `종목/날짜 컬럼을 못 찾았다` | 컬럼명이 별칭 표에 없음 | 에러가 출력한 컬럼 목록을 알려 주면 별칭 추가. 급하면 CSV 헤더를 `symbol,signal_date,label,confidence,sector`로 바꿔도 된다 |
| 라벨이 전부 `미분류` | 라벨 값 표기가 다름 | `stock_auto/horizons.py`의 `_ALIASES`에 추가 |
| `일봉을 하나도 못 받았다` | 네트워크 또는 라이브러리 없음 | `pip install finance-datareader yfinance`, 또는 `--price-dir`로 캐시 지정 |
| 일봉 다운로드가 너무 느림 | 매번 새로 받음 | `--price-dir` 지정 (필수에 가깝다) |
| 그리드가 너무 오래 걸림 | 축을 너무 넓게 잡음 | `--with-partial-tp`/`--with-trail-be`를 빼면 셀이 1/9로 준다 |
| 네트워크가 막힌 서버 | — | `--no-download`(캐시만) 또는 `--synthetic`(배선 확인) |

---

## 9. 파라미터를 바꾸고 싶을 때

기본값은 `stock_auto/exit/config.py`의 두 프리셋에 있다.

```python
ExitConfig.legacy()     # 현행: D+1 · 고정 3%/3% · 장중 · 무보정
ExitConfig.proposed()   # 제안: 단타 D+2 / 중단기 D+3 / 스윙 D+5
                        #      · SL 1.5ATR · TP 2.5ATR · 갭반영 · 5bp
```

> `proposed()`는 `analysis_2026-09-18.md` §4 P0의 **초기값**이지 확정값이
> 아니다. 확정은 그리드를 보고 사람이 한다 (`TASK.md` Phase 1 "멈춤" 항목).

확정한 뒤에는 `proposed()`의 숫자를 고치고 테스트를 돌린다.
`run_exit_backtest --config legacy`로 언제든 현행과 비교할 수 있다.

---

## 10. 다음 단계

| 할 일 | 필요한 것 |
|---|---|
| 그리드 결과 보고 `ExitConfig` 기본값 확정 | 실데이터 백테스트 결과 (n ≥ 100) |
| Phase 3 — triage에 `fill_price`/`fill_status` | `stock_auto/` 원본 소스 |
| Phase 4 — 대시보드 타일·분해표·반사실 곡선 | 대시보드 생성 코드 |
| 기존 12건 수치 회귀 테스트 | 그 12건의 원본 CSV |

Phase 3·4는 원본을 이 레포에 올려 주면 이어서 할 수 있다. 지금 만든
계층은 그때 그대로 붙는다 — `ExitConfig.describe()`가 대시보드 sub 라인
문자열을, `FilterDecision.pill`이 회색 pill 문구를 이미 내놓는다.
