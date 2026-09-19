# Auto Trade — EMA 수렴 브레이크아웃 자동매매 시스템

## 프로젝트 개요
바이낸스 선물(USDT-M) 20x 레버리지 자동매매 봇. EMA 12/26 수렴 → 브레이크아웃 시그널 기반.
듀얼 모드 운영: Cascade E (즉시청산) / CasTrail D (트레일링).

## 핵심 파일 역할

### 시그널 생성
- `convergence_strategy.py` — EMA 수렴 감지 + 브레이크아웃 시그널 생성. `detect()` 함수가 핵심.
  - 스퀴즈: EMA 12/26 갭 < threshold 상태가 N캔들 지속
  - 브레이크아웃: 스퀴즈 이탈 시 방향(LONG/SHORT) + confidence + SL/TP 계산
- `cloud_scan_v2.py` — Numba JIT 고속 벡터화 스캐너. 112코인 × 3년 데이터를 GAP별로 스캔.

### 실행
- `auto_trader_v9.py` (86KB) — 프로덕션 봇 메인. 시그널 수신 → 주문 실행 → 포지션 관리.
  - 듀얼 모드: E (Cascade 즉시청산) / D (CasTrail 트레일링)
  - BTC 7단계 필터 (레벨 0~6)
  - CoinGlass 연동 (펀딩레이트, OI, 롱숏비율)
  - 텔레그램 알림

### 백테스트/분석
- `backtest_entry_quality.py` — 진입 품질 백테스트. MAE/MFE 계산 로직 포함.
- `outcome_labeler.py` — 시그널 CSV에 TP/SL/Timeout 결과 라벨링. 24워커 멀티프로세싱.
  - 출력 컬럼: exit_type, exit_time, exit_price, realized_roe, hold_candles, mae_pct, mfe_pct, direction_correct

### 데이터
- `download_data.py` — 바이낸스 OHLCV 15분봉 다운로드
- `coinglass_client.py` — CoinGlass API (펀딩레이트, OI, 롱숏비율)

### 문서
- `v8_strategy_doc.md` — 전략 상세 문서 (한국어)
- `tasks/experiment_plan_rl_llm.md` — RL+LLM 실험계획서 (v4, 370만 시그널 실측 반영)

## 현재 진행 상황 (2026-03-26)

### 완료
- 370만 시그널 라벨링 완료 (outcome_labeler.py)
  - 112코인 × 3년 × GAP 0.2~2.0
  - 저장: Raw_Data/labeled_signals/signals_all_labeled.csv
- 실험계획서 v4 (실측 데이터 기반 우선순위 재편)

### 핵심 발견 (실측)
- **SL 히트 48.6%** — 전체 손실의 최대 원인
- **SL 히트 중 80.1%가 방향은 맞았음** → 144만 건 개선 기회 (동적 SL)
- **방향 정확도 85.9%** — 이미 높아서 방향 필터 개선 여지 제한적
- **넓은 GAP이 방향 정확도 더 높음** (82.6% @GAP0.0~0.3 → 90.3% @GAP1.0~1.5)
- **프로덕션 PF 1.14**, Level 3까지 확장해도 PF > 1.0 유지

### 다음 단계 (우선순위순)
1. **Phase 4: 동적 SL** — `rl/agent/sl_agent.py` 구현. 144만 건 "방향맞지만 SL히트" 해결.
2. **Phase 1: RL 환경** — `rl/env/trading_env.py` (3-Stage: 방향+실행+SL)
3. **Phase 1.5: 방향 필터** — Stage 1 EXECUTE/SKIP 학습 (FLIP 보류)
4. **Phase 2: LLM 센티먼트** — FinBERT + Claude Haiku 티어드 접근

## 디렉토리 구조 (목표)

```
auto_trade/
├── CLAUDE.md              ← 이 파일
├── auto_trader_v9.py
├── convergence_strategy.py
├── cloud_scan_v2.py
├── backtest_entry_quality.py
├── outcome_labeler.py
├── download_data.py
├── coinglass_client.py
├── rl/                    ← RL 모듈 (구현 예정)
│   ├── env/
│   │   ├── trading_env.py
│   │   └── reward.py
│   ├── agent/
│   │   ├── sl_agent.py    ★ 최우선
│   │   ├── ppo_agent.py
│   │   └── dqn_agent.py
│   ├── features/
│   │   └── state_builder.py
│   └── train.py
├── llm/                   ← LLM 모듈 (구현 예정)
│   ├── sentiment/
│   │   ├── news_scorer.py
│   │   └── funding_interpreter.py
│   ├── prompts/
│   │   └── templates.py
│   └── pipeline.py
├── data/
│   ├── collector/
│   │   └── news_collector.py
│   └── store/
│       └── feature_store.py
├── experiments/
│   ├── configs/
│   ├── logs/
│   └── results/
└── tasks/
    ├── experiment_plan_rl_llm.md
    ├── todo.md
    └── lessons.md
```

## 데이터 경로 (Windows 로컬)

```
D:/AutoTrade/                          (또는 사용자 지정 BASE_DIR)
├── Raw_Data/
│   ├── CRYPTO_BINANCE_15M/            # OHLCV 15분봉 (코인별 CSV)
│   └── labeled_signals/               # outcome_labeler.py 출력
│       └── signals_all_labeled.csv    # 370만 행, 33컬럼
├── Results/
│   └── cloud_scan_v2/all/             # GAP별 시그널 CSV
│       ├── signals_gap0.2.csv
│       ├── signals_gap0.3.csv
│       └── ... (0.1 단위, 19개 파일)
```

## 코딩 규칙

### 일반
- **한국어** 주석/문서/커밋 메시지 사용
- Python 3.10+ 타입 힌트 사용
- 15분봉(candle) 기반 — 모든 시간 단위는 캔들 수로 표현
- 레버리지 20x — ROE 계산 시 항상 반영

### RL 관련
- **Walk-Forward 3윈도우 검증 필수** — 과거 XGBoost 실패 교훈 (데이터 누출)
  - Window 1: Train ~2025-03 → Val ~2025-06 → Test ~2025-09
  - Window 2: Train ~2025-06 → Val ~2025-09 → Test ~2025-12
  - Window 3: Train ~2025-09 → Val ~2025-12 → Test ~2026-03
- **순차 학습**: Stage 3(SL) → Stage 1(방향) → Stage 2(실행)
- Gymnasium 환경 표준 사용
- 라이브러리: stable-baselines3, d3rlpy, gymnasium

### 시그널 레벨 정의
```
프로덕션: conf ≥ 60, ADX ≥ 30, GAP ≤ 0.5%
Level 1:  conf ≥ 50, ADX ≥ 25, GAP ≤ 0.7%
Level 2:  conf ≥ 40, ADX ≥ 20, GAP ≤ 0.8%
Level 3:  conf ≥ 30, ADX ≥ 15, GAP ≤ 1.0%
```

### SL 계산 (현재 — 고정)
```
convergence_strategy.py:
  LONG SL = squeeze_low - buffer
  SHORT SL = squeeze_high + buffer
  buffer = squeeze_range × ratio
→ 시장 변동성 무관한 고정 SL이 문제의 핵심
```

## 과거 교훈 (lessons)

1. **XGBoost 오버피팅 사건**: Walk-Forward 없이 단순 train/test split → 백테스트에서만 좋은 모델. 이후 3윈도우 검증 의무화.
2. **candle_idx 버그**: 인덱스 오프바이원으로 미래 데이터 누출. 시간 기반 분리 강화.
3. **FLIP 전략 보류**: 방향 정확도 82~90%로 이미 높아 반전 시그널 불필요. Phase 5에서 재검토.
4. **outcome_labeler.py 성능**: 24워커 멀티프로세싱으로 370만 시그널 3.7분 완료.

## 실행 환경
- **개발**: Jupyter Notebook (`%run` 방식으로 스크립트 실행)
- **프로덕션**: Windows 서버, Python 3.10+
- **CPU 코어**: 24+ (멀티프로세싱 시 workers=24)
- **GPU**: 선택사항 (FinBERT 추론은 CPU 충분)
