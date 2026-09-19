# Stock — 청산 규칙 재설계 & 백테스트

반자동 주식 추천 시스템의 **청산(exit)·백테스트·계측 계층**이다.
종목 선정(스코어링) 로직은 건드리지 않는다 (`TASK.md` 원칙 1).

## 빠른 시작

```bash
pip install pandas numpy pytest
python -m pytest tests/ -q
python -m stock_auto.backtest.run_exit_backtest --synthetic   # 데이터 없이 배선 확인
```

## 문서

| 문서 | 내용 |
|---|---|
| **[docs/RUNBOOK_exit_backtest.md](docs/RUNBOOK_exit_backtest.md)** | **실행 가이드 — 여기서 시작** |
| [TASK.md](TASK.md) | 작업 지시서 (Phase 0~5) |
| [docs/analysis_2026-09-18.md](docs/analysis_2026-09-18.md) | 배경 성과 분석 |
| [docs/codemap_exit.md](docs/codemap_exit.md) | Phase 0 코드 파악 결과 + 알려진 한계 |
| [docs/BACKTEST_LONG_RUN.md](docs/BACKTEST_LONG_RUN.md) | **1000일 백테스트 — 모델로 시그널 생성** |
| [docs/BACKTEST_100D.md](docs/BACKTEST_100D.md) | 100일 백테스트 (기록된 시그널 기준) |
| [docs/MODEL_REPRODUCTION.md](docs/MODEL_REPRODUCTION.md) | 재현한 스코어링 모델의 가정 14개와 한계 |

## 브랜치

```
ssh_upload_stock   수정본 — 청산 엔진 + 전 구간 그리드. 파라미터를 정한다
backtest_100d      백테스트 — + 최근 100 거래일 러너. 정한 값의 최근 성적을 본다
```

## 구조

### 시그널 출처가 표본 크기를 결정한다

```
run_exit_backtest / run_100d   signals.csv 읽기  →  표본 = 기록된 시그널 이력
run_model_backtest             모델로 생성       →  표본 = 가격 데이터 길이
```

2주 운영 시 전자는 24건, 후자는 1000일 × 종목 수만큼(800~3,800건) 나온다.
`--window`를 늘려도 시그널 파일이 2주치면 결과가 2주치인 이유가 이것이다.

```bash
python -m stock_auto.backtest.run_model_backtest --days 1000 --price-dir data/cache
```

```
stock_auto/
├── horizons.py          단타 / 중단기 / 스윙 단일 정의
├── model/               ← 스코어링 재현 (시그널 생성)
│   ├── indicators.py    지표 (전부 인과적 — 절단 불변 검증)
│   ├── strategies.py    Money 3 + Price 7, Money Cap, Effective Score
│   ├── regime.py        종목 5단계 + 매크로 지수 게이트
│   ├── universe.py      유니버스 (추정 — --universe로 교체)
│   └── generator.py     과거 N일 시그널 생성
├── exit/
│   ├── config.py        ExitConfig — 라벨별 보유상한·SL/TP
│   ├── atr.py           ATR(14), 진입 전일 기준
│   ├── engine.py        배리어 시뮬레이션, 갭 체결가
│   ├── metrics.py       PF·기대값·손익비·MDD·포착률
│   └── filters.py       확신도 / 쿨다운 / 섹터한도
├── backtest/
│   ├── dataset.py       시그널·일봉 로더 (+ 합성)
│   ├── exit_grid.py     그리드 + walk-forward
│   ├── report.py        라벨별 분리 리포트
│   └── run_exit_backtest.py
└── advisor/advisor.py   라벨별 규칙 기반 조언
```

※ 분석 보조 자료이며 투자 권유가 아닙니다.
