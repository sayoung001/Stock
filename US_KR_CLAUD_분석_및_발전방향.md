# 📊 US_KR_)CLAUD 트레이딩 시스템 — 코드 분석 및 발전 방향

> 대상: `US_KR_)CLAUD/` (브랜치 `claude/trading-strategy-research-k9RvA`)
> 분석일: 2026-06-21
> 구성: `indicators_v2.py` · `strategies.py` · `regime_v2.py` · `sector_analysis.py` · `sector_map.py` · `run_analysis.py` + README 2종

---

## 1. 시스템 한눈에 보기

미국(NASDAQ)/한국(KOSPI) 주식의 **일봉 기반 매수·매도 신호 스캐너**.
핵심 사상은 *"기술적 가능성"이 아니라 "자금이 실제로 밀어줄 확률"을 점수화* 하는 것이다.

```
Effective_Score = Technical_Score × Liquidity_Score − Penalty
  Technical_Score = Money_Score + Price_Score   (Money==0이면 Price 최대 2.0 Cap)
  Liquidity_Score = sigmoid(거래대금)            (0.3~1.4)
  Penalty         = 윗꼬리 / 장중밀림 / 단발거래량 / 저유동성 차감
```

| 모듈 | 역할 | 비고 |
|------|------|------|
| `indicators_v2.py` | 보조지표 + VAI 2단·Liquidity·Penalty 지표 | 457줄 |
| `strategies.py` | Money 3 + Price 7 전략, 실효점수, 매도신호 | 428줄 |
| `regime_v2.py` | 5단계 레짐 + 전환 감지 | 150줄 |
| `sector_analysis.py` | 섹터 ETF 진단 + 차트/CSV | 456줄 |
| `run_analysis.py` | 통합 실행·CSV·텔레그램 | 464줄 |

---

## 2. 잘 설계된 점 (유지·강화할 강점)

1. **Data Leakage 방어가 코드에 박혀 있음** — VAI가 `V.shift(1).rolling(...)`로 "어제까지"를 기준 삼고, squeeze/다이버전스도 `shift(1)` 사용. CLAUDE.md의 candle_idx 교훈이 반영됨.
2. **Money/Price 신호 분리 + Money Cap** — "거래량 없이 기술지표만으로 고득점"을 구조적으로 차단한 발상이 우수.
3. **Liquidity를 곱셈·연속함수(sigmoid)로** — 계단식 컷오프(Cliff Effect)를 완화. 좋은 방향.
4. **Penalty 차감 도입** — 윗꼬리/단발펌핑 종목을 명시적으로 감점.
5. **모듈 분리 + `--market` 분기** — 미장/국장 노트북 중복을 통합.
6. **매도(Exit) 신호 5종** — 진입만 있던 기존 대비 라이프사이클 완성도 ↑.

---

## 3. 발전 방향 (우선순위순)

### 🔴 P0 — 지금 고치지 않으면 결과가 틀어지는 것

#### 3.1 한국장 Liquidity Score 통화 버그 (실데이터 오류)
`indicators_v2.py`의 `_calc_liquidity_score(series, market='US')`는 시장 인자를 받지만,
`calculate_indicators()`는 **항상 기본값 `US`로만 호출**한다.

```python
# 현재 (calculate_indicators 내부)
df['liquidity_score'] = _calc_liquidity_score(tv_ma5)   # market 인자 미전달 → 항상 'US'
```

- 결과: 한국 종목의 거래대금(원화, 수십~수백억)이 **USD 기준 center=5천만**과 비교되어
  거의 모든 KR 종목이 `liquidity_score ≈ 1.4`로 포화 → **유동성 필터·Penalty가 무력화**.
- `Final_Buy`의 `liquidity_score >= 0.7` 조건도 KR에서 사실상 항상 통과.
- **수정**: `calculate_indicators(df, market='US')` 인자를 추가하고 `run_analysis`의
  `config['market_label']`을 끝까지 전달.

#### 3.2 섹터 상태가 점수에 반영되지 않음 (문서 ↔ 구현 불일치)
README_v2.1는 *"3점 + 섹터강력매수 + VAI지속 → Effective ~4.0 매수후보"*라고 설명하지만,
실제 `apply_all_strategies()`에는 **섹터(Sector_Status)가 들어오지 않는다.** `Sector_Status`는
`run_analysis`에서 표시용 컬럼으로만 붙는다.

- **수정**: 섹터 상태를 점수에 합류시키거나(예: 섹터≥4면 +0.5, 섹터 0이면 매수 차단),
  최소한 README에서 "표시 전용"임을 명확히 해 기대와 구현을 일치시킨다.

#### 3.3 죽은 기능 — `--optuna`
`run_analysis(use_optuna=...)`와 `--optuna` 플래그를 받지만 **함수 내부에서 한 번도 사용되지 않음.**
README의 "3단계: Optuna 오버라이드"는 미구현. 제거하거나 실제 연결할 것.

#### 3.4 커밋된 `__pycache__/*.pyc`
`US_KR_)CLAUD/__pycache__/`의 컴파일 산출물이 저장소에 포함됨. `.gitignore`에
`__pycache__/`, `*.pyc` 추가하고 추적 해제.

---

### 🟠 P1 — 신뢰성·검증 (이 시스템의 가장 큰 빈틈)

#### 3.5 백테스트 엔진 부재 → 모든 가중치·임계값이 미검증 가정치
가중치(2.0/1.5/1.0), 매수 임계(`Effective≥4.0`, `≥2.5 & Money≥1.5`), VAI 컷(1.5/1.2/2.0/1.1),
Penalty 점수(0.5/1.0)가 전부 **손으로 정한 값**이다. `run_analysis`는 백테스트 결과 CSV를
*읽기만* 할 뿐(`load_backtest_stats`), **생성하는 엔진이 없다.**

- CLAUDE.md 최대 교훈("XGBoost 오버피팅 → Walk-Forward 의무화")이 여기엔 적용되지 않음.
- **제안**: `backtest.py` 신설 — 신호 발생 후 N봉 보유 시 TP/SL/Timeout 라벨링
  (auto_trade의 `outcome_labeler.py` 패턴 재사용). 그 위에서
  **Walk-Forward 3윈도우**로 가중치/임계값을 검증·튜닝.

#### 3.6 어닝(실적) 필터가 매수를 막지 않음
README 체크리스트는 "실적 D-3 이내 회피"를 명시하지만, 코드는 `D-Day`를 **표시만** 한다.
`Final_Buy`에 `0 <= dday <= 3 → 매수 차단(또는 경고+1)` 로직을 넣어 갭 리스크 제거.

#### 3.7 매크로(지수) 레짐 부재
레짐이 **개별 종목 MA 배열**로만 산출된다. 약세장에서도 개별 종목은 정배열일 수 있어
시장 위험을 놓친다.
- **제안**: SPY/QQQ(미장), KOSPI/KOSDAQ(국장) 지수 레짐을 별도 산출해
  종목 레짐과 **AND 게이트**(예: 지수 레짐 0이면 전 종목 매수 보류).

#### 3.8 상대강도(RS) 부재
"섹터/지수 대비 강한 종목"을 고르는 RS(Relative Strength)가 없다. 추세장 종목 선별의
핵심 축이므로 `RS_vs_Sector`, `RS_vs_Index` 컬럼 추가 권장.

---

### 🟡 P2 — 견고성·유지보수

#### 3.9 광범위한 `except Exception: continue` (조용한 실패)
`run_analysis`의 종목 루프, 로더 함수들이 모든 예외를 삼킨다. **수백 종목이 조용히 누락**돼도
알 수 없다. 최소한 `logging`으로 실패 종목/사유를 집계 출력.

#### 3.10 점수 체계 중복 정리
`Signal_Count`(레거시), `Composite_Score`(=Effective 복제), `Buy_Pattern`, `Trend_Up`···
레거시 불리언이 출력에 공존한다. 하위호환이 필요 없다면 정리해 혼선 제거.

#### 3.11 포지션 사이징·리스크 미연결
`REGIME_TRADING_PARAMS`에 `size`가 있고 `get_trading_params()`도 있으나 **어디서도 호출 안 함.**
레짐·R:R 기반 포지션 크기를 출력 컬럼/알림에 연결하면 실거래 활용도 ↑.

#### 3.12 성능 (24코어 미활용)
종목 단위 순차 처리 + `iterrows`. CLAUDE.md 환경은 24코어 멀티프로세싱 가능
(`outcome_labeler.py`가 370만 행 3.7분 선례). `multiprocessing`으로 종목 병렬화 권장.

#### 3.13 데이터 품질 가드 부재
분할/배당 조정, 결측, 거래정지/이상치(0거래량) 검증이 없다. 로딩 단계 sanity-check 추가.

#### 3.14 시장별 파라미터 분리
RSI<30, RVOL>2 등 임계값이 미·한 동일. 변동성·거래시간 구조가 다르므로
`MARKET_CONFIG`에 시장별 임계 프로파일을 두는 편이 정확.

#### 3.15 테스트 코드 부재
지표·전략의 회귀 방지를 위한 최소 단위테스트(`tests/`)와 look-ahead 검증 픽스처 필요.

---

## 4. 더 나아간 발전 아이디어 (로드맵)

| 단계 | 내용 | auto_trade 자산 재사용 |
|------|------|------------------------|
| **A. 검증 인프라** | 백테스트 엔진 + Walk-Forward 3윈도우로 가중치/임계 튜닝 | `outcome_labeler.py` 라벨링 패턴 |
| **B. 랭킹 레이어** | 규칙 점수를 feature로, 결과 라벨로 **학습형 랭커**(LightGBM) → 매수 후보 정렬 | RL 실험계획서의 Walk-Forward 규율 |
| **C. 센티먼트** | 실적/뉴스 LLM 스코어를 Penalty/Bonus로 가산 | CLAUDE.md `llm/` 모듈, FinBERT+Haiku 티어드 |
| **D. 매크로 레짐** | 지수·금리·변동성(VIX) 레짐 게이트 | `coinglass_client.py` 구조 참고(외부지표 클라이언트) |
| **E. 포트폴리오** | 종목 단위 신호 → 비중·상관·섹터 분산 고려한 포트폴리오 구성 | 신규 |

---

## 5. 즉시 착수 권장 Top 5

1. **한국장 Liquidity 통화 버그 수정** (§3.1) — 가장 시급, KR 결과 신뢰성 직결
2. **`.gitignore` + `__pycache__` 추적 해제** (§3.4) — 5분, 즉시
3. **어닝 D-3 매수 차단 로직** (§3.6) — 적은 코드로 큰 리스크 제거
4. **섹터 상태를 점수에 실제 반영하거나 문서 정정** (§3.2) — 기대-구현 일치
5. **백테스트 엔진 + Walk-Forward 골격** (§3.5) — 모든 임계값을 "가정"에서 "검증"으로

---

*본 문서는 코드 정적 분석 기반이며, 실데이터 백테스트로 각 가설을 재검증할 것을 전제로 한다.*
