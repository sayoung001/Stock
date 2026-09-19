# Phase 0 — 코드 파악 결과 (청산 관련)

작성 2026-09-19 · 대상 레포 `sayoung001/stock` 브랜치 `ssh_upload_stock`
· 방법: 전체 파일 전수 + 문서 교차 대조

---

## 0. 먼저 보고할 것 — `stock_auto/` 소스가 이 레포에 없다

TASK.md Phase 0은 "청산 시뮬레이션 코드가 어디에 하드코딩돼 있는지" 등을
찾으라고 지시한다. **찾을 수 없었다. 이 레포에 `stock_auto/` 패키지가
들어 있지 않기 때문이다.**

확인한 범위:

| 확인 | 결과 |
|---|---|
| 작업 브랜치 `ssh_upload_stock` 파일 전수 | Python 15개 — 전부 **코인 선물 봇**(`auto_trader_v9.py`, `convergence_strategy.py` 등) |
| 원격 브랜치 목록 | `main`, `ssh_upload_stock` 둘뿐. 어느 쪽에도 `stock_auto/` 없음 |
| `stock_auto` 문자열 검색 | **md 문서 8개에서만** 발견. `.py` 파일에는 0회 |

즉 이 레포에 있는 것은 `stock_auto/`를 **설명하는 문서**이지 코드가 아니다.
`구글클라우드_배포_가이드.md`가 zip 업로드로 GCP에 올리는 절차를 적고 있는
것으로 보아, 실제 소스는 별도 배포본(로컬 또는 GCE 인스턴스)에 있다.

### 그래서 이번 작업은 이렇게 진행했다

선정 로직을 건드리지 않는다는 TASK.md 원칙 1을 **패키지 경계로** 바꿔 지켰다.
청산·백테스트·계측 계층을 `stock_auto/` 하위에 **독립 패키지로 새로 구현**하고,
기존 시스템과는 **문서화된 데이터 계약**으로만 연결한다.

```
기존 배포본 (이 레포에 없음)            이번에 만든 것 (이 레포)
─────────────────────────────          ──────────────────────────────
pipeline/run_daily_batch.py
  → data/tracking/signals.csv  ────►  backtest/dataset.load_signals()
  → 일봉 CSV 캐시              ────►  backtest/dataset.load_prices()
                                              │
                                              ▼
                                      exit/engine.simulate_all()
                                      backtest/exit_grid.run_grid()
                                              │
                                              ▼
                                      experiments/results/*.md, *.csv
```

기존 코드를 **한 줄도 수정하지 않는다.** 시그널 CSV를 읽고 결과를 파일로
쓸 뿐이다. 그래서 프로덕션 배치가 도는 중에도 병렬로 돌릴 수 있다.

**확인이 필요한 것**: 실제 `signals.csv`의 컬럼명. 아래 §2의 별칭 표로
흔한 표기는 자동 인식하지만, 실물을 보지 못했으므로 첫 실행에서
"종목/날짜 컬럼을 못 찾았다"가 나오면 그 에러가 출력하는 컬럼 목록을
알려 주면 별칭을 추가한다.

---

## 1. TASK.md Phase 0 항목별 답

| 찾아야 할 것 | 결과 |
|---|---|
| 추천 시그널 저장 위치/스키마 | **코드 부재.** 문서상 `data/tracking/signals.csv`. 라벨은 단타/중단기/스윙, 확신도는 `conviction`(0~1). 실물 스키마 미확인 |
| 청산 시뮬레이션 코드 | **코드 부재.** 문서상 `backtest/labeler.py`의 삼중 배리어. 아래 §3에 문서에서 복원한 동작 |
| TP/SL/보유상한 하드코딩 위치 | **확인 불가.** 새 구현에서는 `stock_auto/exit/config.py`의 `ExitConfig` 한 곳으로 모았다 |
| 장중 vs 종가 판정 | 문서상 **장중**(삼중 배리어, 갭 관통 시 skip). 새 구현은 `close_based` 옵션으로 양쪽 비교 가능 |
| 체결가 산정 방식 | 문서상 배리어 가격 그대로. 갭 미반영 — analysis §3-2가 지적한 문제의 원인 |
| D+N / MFE20 / MAE20 계산 | **코드 부재.** 새 구현: `exit/engine.py`가 진입가 기준으로 계산 |
| `pipeline.triage` 입력 구조 | **코드 부재.** Phase 3 대상이며 이번 범위 밖 (아래 §5) |
| 대시보드 HTML 생성 코드 | **코드 부재.** Phase 4 대상이며 이번 범위 밖 (아래 §5) |
| 누적 시그널 건수 | **확인 불가** — `signals.csv`가 없다. 백테스트 표본 크기는 실행해 봐야 안다 |
| 가격 데이터 소스 / ATR 가능 여부 | 문서상 FinanceDataReader → yfinance 폴백, OHLCV 400일. **OHLC가 있으므로 ATR 계산 가능** ✅ |

---

## 2. 데이터 계약 (새 코드가 기대하는 것)

### 시그널 CSV

컬럼명은 아래 별칭 중 아무거나 인식한다 (대소문자·공백 무시).

| 역할 | 인식하는 컬럼명 | 필수 |
|---|---|:---:|
| 종목 | `symbol` `ticker` `code` `종목` `종목코드` | ✅ |
| 기준일 | `signal_date` `date` `asof` `base_date` `기준일` `기준봉` `날짜` | ✅ |
| 라벨 | `label` `horizon` `hold_label` `보유기간` `라벨` `구분` | — |
| 확신도 | `confidence` `conviction` `conf` `확신도` | — |
| 섹터 | `sector` `sector_etf` `섹터` `섹터ETF` | — |
| 출처 | `source` `origin` `출처` | — |

라벨 값은 `단타`/`day`/`중단기`/`mid`/`스윙`/`swing` 등을 정규화한다.
**못 알아본 라벨은 단타로 떨어뜨리지 않고 `미분류`로 남긴다** — 라벨마다
보유상한이 다르므로 조용한 오분류가 결과를 바꾼다.

### 일봉

`--price-dir`의 `{종목}.csv` (컬럼 `date,open,high,low,close`), 없으면
FinanceDataReader → yfinance 순으로 받아 캐시에 적는다.

---

## 3. 문서에서 복원한 기존 청산 동작 (= `ExitConfig.legacy()`)

`현재_적용_로직_전수.md` §5 + `analysis_2026-09-18.md` §2 기준:

```
진입      시그널 다음 거래일 시가
보유상한  D+1  ← 세 라벨 전부. 라벨이 청산에 반영되지 않는다 (설계 모순)
TP/SL     고정 % (대시보드 관측상 TP +3% 부근)
판정      장중 (고가/저가)
동시도달  SL 우선
체결가    배리어 가격 그대로 — 갭·슬리피지 미반영
갭 관통   문서상 "미체결(skip)"
```

`ExitConfig.legacy()`가 이 동작을 재현하고, `tests/test_exit_engine.py`의
`test_legacy_*` 4건이 그것을 고정한다.

> **회귀 테스트 관련 한계**: TASK.md Phase 1은 "기존 12건 결과가 동일하게
> 나오는 회귀 테스트"를 요구한다. 그 12건의 원본 데이터(종목·날짜·체결가)가
> 이 레포에 없어 **수치 일치 검증은 못 했다.** 대신 legacy의 *동작*
> (D+1 · 고정% · 장중 · 무보정)을 테스트로 고정해 두었다. 12건 CSV를 주면
> 바로 수치 회귀 테스트를 추가할 수 있다.

---

## 4. 새로 만든 것 — 파일·함수 지도

| 파일 | 핵심 심볼 | 역할 |
|---|---|---|
| `stock_auto/horizons.py` | `normalize_label` `SCORING_HORIZON_DAYS` `LEGACY_HOLD_DAYS` `PROPOSED_HOLD_DAYS` | 단타/중단기/스윙 단일 정의. **세 가지로 섞여 쓰이던 라벨 의미를 분리** |
| `stock_auto/exit/config.py` | `ExitConfig` `LabelExit` `.legacy()` `.proposed()` | 청산 파라미터 전부. 라벨별 보유상한·SL/TP 모드 |
| `stock_auto/exit/atr.py` | `atr_wilder` `true_range` | ATR(14). 워밍업 부족은 NaN (0으로 안 채움) |
| `stock_auto/exit/engine.py` | `simulate_exit` `simulate_all` `PriceSeries` `Signal` `ExitResult` | 배리어 워크. 갭 체결가·부분익절·본전 트레일 |
| `stock_auto/exit/metrics.py` | `compute_metrics` `by_label` `by_confidence_bucket` `by_sector` | PF·기대값·손익비·MDD·포착률 |
| `stock_auto/exit/filters.py` | `FilterConfig` `apply_filters` `split_by_filter` | Phase 2 실행 필터 (플래그만, 삭제 안 함) |
| `stock_auto/backtest/dataset.py` | `load_signals` `load_prices` `make_synthetic_dataset` | 로더 + 오프라인 합성 |
| `stock_auto/backtest/exit_grid.py` | `GridSpec` `run_grid` `walk_forward_split` `best_per_label` | 그리드 + 시간순 60/40 |
| `stock_auto/backtest/report.py` | `build_report` | 라벨별 분리 markdown |
| `stock_auto/advisor/advisor.py` | `diagnose_label` `advise_overall` | 규칙 기반 라벨별 조언 |
| `stock_auto/backtest/run_exit_backtest.py` | `main` | CLI 진입점 |

### 체결가 규칙에서 TASK.md와 다르게 구현한 곳 (의도적)

TASK.md Phase 1은 `TP는 min(목표가, 시가)`로 적었다. 그대로 구현하면
**갭이 없는 평범한 날**(시가 < 목표가)에 TP 체결가가 시가로 내려앉아
익절 수익이 통째로 사라진다. 갭으로 관통한 경우에만 적용하는 것이 그
문장의 의도로 보고 그렇게 구현했다:

```
SL 체결가 = min(손절가, 당일 시가)        ← 갭다운을 항상 떠안는다
TP 체결가 = 목표가                        ← 갭업 이득을 절대 가져가지 않는다
```

두 규칙 다 **불리한 쪽으로만** 작동한다. `test_gap_down_through_sl_fills_at_open_not_at_stop`과
`test_gap_up_through_tp_does_not_credit_the_gap`이 이를 고정한다.

---

## 5. 이번 작업에서 **하지 않은** 것

| TASK.md 항목 | 상태 | 이유 |
|---|---|---|
| Phase 3 — `triage` 입력에 `fill_price`/`fill_status` 추가 | **미착수** | `triage` 소스가 이 레포에 없다. 스키마를 추측해 만들면 실물과 충돌한다 |
| Phase 3 — 슬리피지 실측 집계 | **부분** | 백테스트 쪽 `slippage_bps`는 구현. 실제 체결가 입력은 triage 소스 필요 |
| Phase 4 — 대시보드 HTML 타일 추가 | **미착수** | 대시보드 생성 코드가 이 레포에 없다 |
| Phase 5 — README 갱신 | **대체** | `docs/RUNBOOK_exit_backtest.md`로 대신 작성 |
| Phase 1 — 기존 12건 수치 회귀 | **미완** | 원본 12건 데이터 부재 (§3 참고) |

Phase 3·4는 `stock_auto/` 소스를 이 레포에 올려 주면 이어서 할 수 있다.
지금 만든 계층은 그때 그대로 붙는다 — `ExitConfig.describe()`가 대시보드
sub 라인 문자열을, `FilterDecision.pill`이 회색 pill 문구를 이미 내놓는다.

---

## 6. 표본 크기 — 완료 기준과의 관계

TASK.md 완료 기준은 `n ≥ 100`(walk-forward 검증 구간)이다. 현재
`signals.csv`를 못 봐서 **표본이 그 기준을 넘는지 알 수 없다.**

코드는 이 상황을 가정하고 짰다. `best_per_label(rows, min_n=100)`은
검증 구간 표본이 기준 미달이면 **최적값을 고르지 않고 `None`을 돌려주며**,
리포트는 그 자리에 다음 문장을 찍는다:

> ⚠️ **표본 부족 — 최적값을 고르지 않는다.** 검증 구간 최대 N건으로
> 기준 n ≥ 100에 미달. M건 더 필요. 이 구간에서는 legacy 설정을 유지한다.

부족한 표본에서 1등을 집어 주는 것이 곧 과적합이므로, 그 경로를 코드에서
막아 두었다.
