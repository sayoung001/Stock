# Vision AI + RAG + 리스크 관리 에이전트 통합 계획

> 작성일: 2026-04-16 (v2: 기존 에이전트 통합 + Vision 보조역할 반영)
> 기반: experiment_plan_rl_llm.md (v4), qrak/LLM_trader, Swarm/AutoHedge
> 원칙: 기존 agents/ 프레임워크를 기반으로 확장. Vision AI는 보조 피처, RAG + 리스크 에이전트가 핵심.
> 구현: Claude Code (CLI)로 자율 코딩 위임. 중간 보고 + 최종 검증 방식.

---

## 배경 및 동기

### 현재 시스템의 한계
```
1. 고정 SL 문제: 48.6% SL 히트, 그 중 80.1%가 방향 맞음 (→ Phase 4로 해결 중)
2. 컨텍스트 부족: 기술적 지표만 사용, 뉴스/온체인/시각적 패턴 미반영
3. 20x 고정 레버리지: 급변 시장에 유연한 대응 불가
4. 학습 불능: 과거 거래에서 규칙을 자동 도출하는 메커니즘 없음
5. 헷지 부재: 수렴 포지션에 반대 포지션으로 리스크 완화 수단 없음
```

### 차용 프레임워크

| 소스 | 핵심 차용 포인트 | 적용 모듈 |
|------|------------------|-----------|
| qrak/LLM_trader | Vision AI 차트 분석 (Plotly 렌더 → LLM 비전) | vision/ |
| qrak/LLM_trader | RAG 뉴스/분석 통합 (ChromaDB 벡터스토어) | rag/ |
| qrak/LLM_trader | 적응형 메모리 (시간 감쇠 + 자동 반성 루프) | memory/ |
| AutoHedge | 멀티에이전트 파이프라인 (Director→Quant→Risk→Exec) | risk/ |
| AutoHedge | 리스크 에이전트 (포지션 사이징, 헷지 판단) | risk/ |
| 기존 agents/ | 6개 분석 에이전트 + Orchestrator + Memory | 재활용 (기반) |

### ★ 기존 에이전트 시스템 활용 (2026-04-16 추가)

```
hello-world-claude-trading-strategy-research-k9RvA/agents/ 에
이미 완성된 멀티에이전트 프레임워크가 존재 (총 5,668줄):

■ 재활용 대상:
  agents/core/base.py          → AgentBase 클래스 (Haiku/Sonnet 듀얼 모델)
  agents/core/orchestrator.py  → Bull/Bear 토론 + 합의 메커니즘
  agents/core/protocol.py      → Pydantic 데이터 모델 (AgentMessage 등)
  agents/memory/store.py       → 계층형 메모리 (단기/중기)

■ 기존 분석 에이전트 6개 (그대로 활용):
  btc_structure.py     → Analyst Agent의 BTC 분석 입력으로 연결
  macro.py             → Analyst Agent의 매크로 컨텍스트 입력
  news_sentiment.py    → RAG 뉴스 검색 결과를 입력으로 교체
  alt_ecosystem.py     → 알트코인 생태계 분석 유지
  correlation.py       → 상관 코인 헷지 판단의 입력으로 활용
  position_judge.py    → Risk Agent의 포지션 판단 로직으로 흡수

■ 신규 에이전트 (AgentBase 상속):
  risk/analyst_agent.py   → 기존 6개 에이전트 출력을 종합
  risk/risk_agent.py      → SL/TP/사이징 제안
  risk/hedge_agent.py     → 헷지 전략 판단
  risk/leverage_agent.py  → 레버리지 동적 조절

■ 통합 구조:
  기존 Orchestrator(Bull/Bear 합의)
    ↓ market_consensus
  Risk Pipeline(Analyst→Risk→Hedge→Leverage)
    ↓ risk_decision
  auto_trader_v9.py 실행
```

### ★ Vision AI 역할 조정 (2026-04-16 추가)

```
■ 신뢰도 평가:
  패턴 인식 (트렌드/형태): 70~75%
  수치 정확도 (가격 읽기): 60~75% (지표 겹침 시 저하)
  → Gemini Flash가 차트 해석에서 다소 우위 (원래 멀티모달 설계)

■ 역할 조정:
  기존 계획: Vision AI를 독립 시그널 보강 피처로 활용
  변경: Vision AI를 "보조 확인 채널"로 격하
  
  사용 방식:
  - 수치 시그널이 LONG인데 Vision이 "하락 패턴" → confidence 하향
  - 수치 시그널과 Vision이 일치 → confidence 소폭 상향
  - Vision 단독으로 시그널 생성/거부하지 않음
  
  비용: 월 ~$0.45 (시그널당 $0.003 × 150건)
  → 낮은 비용으로 실험, 효과 미미하면 제거

■ 대안 검토:
  Claude Vision이 효과 없으면 → Gemini Flash로 교체 (qrak 방식)
  둘 다 효과 없으면 → Vision 모듈 전체 비활성화, 수치 지표에 집중
```

---

## 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────────┐
│                    시그널 생성 (convergence_strategy.py)              │
│                    ★ 기존 유지 (넓은 그물 + Level 3)                  │
└──────────────┬──────────────────────────────────────────────────────┘
               │ 후보 시그널
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              ★ NEW: 컨텍스트 강화 파이프라인                           │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐      │
│  │ Vision AI    │  │ RAG Engine   │  │ Adaptive Memory      │      │
│  │              │  │              │  │                      │      │
│  │ 차트 렌더링   │  │ ChromaDB     │  │ 시간 감쇠 규칙 저장   │      │
│  │ → Claude     │  │ 뉴스/분석    │  │ 자동 반성 루프        │      │
│  │   Vision     │  │ 시맨틱 검색   │  │ 거래 규칙 생성/갱신   │      │
│  │ → 패턴 점수  │  │ → 컨텍스트   │  │ → 신뢰도 조정        │      │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘      │
│         │                 │                      │                  │
│         └─────────────────┴──────────────────────┘                  │
│                           │ enriched_state                          │
└───────────────────────────┼─────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│              ★ NEW: 리스크 관리 에이전트 (risk/)                      │
│              AutoHedge 참고 멀티에이전트 파이프라인                     │
│                                                                     │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ Analyst    │  │ Risk       │  │ Hedge      │  │ Leverage     │  │
│  │ Agent      │→ │ Agent      │→ │ Agent      │→ │ Agent        │  │
│  │            │  │            │  │            │  │              │  │
│  │ 시장상태   │  │ 포지션위험  │  │ 헷지판단   │  │ 배율조절     │  │
│  │ 종합분석   │  │ SL/TP제안  │  │ 반대포지션  │  │ 10x~20x     │  │
│  │ (Vision+   │  │ 사이징제안  │  │ 부분청산   │  │ 변동성기반   │  │
│  │  RAG 활용) │  │ 드로다운   │  │ 페어트레이드│  │ 마진율관리   │  │
│  └────────────┘  └────────────┘  └────────────┘  └──────────────┘  │
│                                                                     │
│  출력: RiskDecision { sl_adjust, hedge_action, leverage, sizing }   │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│              기존: RL 게이트키퍼 (Phase 1~4)                         │
│              Stage 1(방향) + Stage 2(실행) + Stage 3(동적 SL)        │
│              ★ RiskDecision을 state에 주입                           │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    auto_trader_v9.py 실행                            │
│           (RL + Risk Agent 결정으로 주문 실행)                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Module A: Vision AI 차트 분석 (`vision/`)

### 목표
15분봉 차트를 이미지로 렌더링 → Claude Vision으로 패턴 인식 → 시그널 보강 피처 생성

### 왜 필요한가
```
현재: 수치 지표만 사용 (EMA, ADX, RSI, ATR 등)
문제: 차트 패턴(헤드앤숄더, 삼각수렴, 더블바텀 등)은 수치로 포착하기 어려움
qrak 접근: Plotly로 ~150캔들 차트 렌더 → Vision AI가 "사람 트레이더처럼" 패턴 인식
→ 기존 수치 지표로 놓치는 시각적 패턴을 보완
```

### 파일 구조
```
auto_trade/
├── vision/
│   ├── __init__.py
│   ├── chart_renderer.py      # Plotly/mplfinance → PNG 렌더링
│   ├── pattern_analyzer.py    # Claude Vision API 호출 → 패턴 분류
│   ├── vision_scorer.py       # 패턴 → 수치 점수 변환 (state 피처용)
│   └── cache.py               # 차트 이미지/분석 결과 캐싱
```

### 핵심 설계

#### A-1. 차트 렌더러 (`vision/chart_renderer.py`)
```python
# qrak 방식 차용: Plotly로 AI 최적화 차트 생성
# 150캔들 = 15분봉 × 150 = 약 37.5시간 (1.5일+) 범위

def render_chart(symbol: str, ohlcv_df: pd.DataFrame, signal: dict) -> bytes:
    """
    시그널 발생 시점 기준 150캔들 차트를 PNG로 렌더링
    
    포함 요소:
    - 캔들스틱 (OHLC)
    - EMA 12/26 라인
    - 볼린저밴드
    - 볼륨 바
    - 스퀴즈 구간 하이라이트 (배경 음영)
    - 시그널 포인트 마커
    - SL/TP 레벨 수평선
    
    AI 최적화:
    - 고대비 색상 (다크 배경, 밝은 캔들)
    - 불필요한 그리드/라벨 최소화
    - 해상도: 1024×768 (Vision 모델 최적)
    """
    pass
```

#### A-2. 패턴 분석기 (`vision/pattern_analyzer.py`)
```python
# Claude Vision API로 차트 패턴 분석
# qrak의 multi-provider 전략 차용: Claude → Gemini 폴백

CHART_ANALYSIS_PROMPT = """
당신은 암호화폐 테크니컬 분석 전문가입니다.
이 15분봉 차트를 분석하고 다음을 판단하세요.

1. 식별된 차트 패턴 (패턴명, 신뢰도 0~100)
2. 지지/저항 레벨 (가격대)
3. 추세 강도 (-1.0 ~ 1.0, 음수=하락, 양수=상승)
4. 현재 가격의 패턴 내 위치 (초기/중간/말기)
5. 브레이크아웃 방향 예측 (LONG/SHORT/UNCERTAIN)
6. 권장 SL 레벨 (차트 기반)

JSON으로만 응답하세요:
{
    "patterns": [{"name": str, "confidence": int, "phase": str}],
    "support_levels": [float],
    "resistance_levels": [float],
    "trend_strength": float,
    "pattern_position": str,
    "breakout_prediction": str,
    "chart_based_sl": float,
    "risk_assessment": str  // "low", "medium", "high"
}
"""

class PatternAnalyzer:
    def __init__(self, primary_model="claude-sonnet-4-5-20250514"):
        # Claude Vision 기본, Gemini Flash 폴백 (비용 최적화)
        # 시그널당 1회 호출 → 월 ~150건 × $0.003 = ~$0.45
        pass
    
    async def analyze(self, chart_image: bytes, signal_context: dict) -> dict:
        """차트 이미지 + 시그널 컨텍스트 → 패턴 분석 결과"""
        pass
```

#### A-3. 비전 점수 변환기 (`vision/vision_scorer.py`)
```python
# 패턴 분석 결과를 RL state에 주입 가능한 수치 피처로 변환

def to_state_features(analysis: dict) -> dict:
    """
    Vision AI 분석 결과 → RL state 피처
    
    Returns:
        {
            "vision_trend": float,         # -1.0 ~ 1.0
            "vision_pattern_conf": float,  # 0.0 ~ 1.0
            "vision_breakout_align": float, # 시그널 방향과 Vision 예측 일치도
            "vision_sl_suggest": float,    # Vision 기반 SL (ATR 단위)
            "vision_risk_level": float,    # 0.0(저) ~ 1.0(고)
        }
    """
    pass
```

### 비용/성능 추정
```
호출 빈도: 시그널 발생 시만 (Level 3 기준 월 ~150건)
모델: Claude Sonnet (Vision) — 이미지 + 텍스트 프롬프트
비용: ~$0.003/건 × 150건 = 월 ~$0.45
지연: ~2초/건 (비동기 호출, 시그널 처리 중 병렬)
캐싱: 동일 심볼+시점 5분 내 중복 호출 방지
```

---

## Module B: RAG 뉴스/분석 엔진 (`rag/`)

### 목표
뉴스, 온체인 데이터, 과거 거래 분석을 벡터 DB에 저장 → 시그널 발생 시 유사 상황 시맨틱 검색 → 컨텍스트 피처 제공

### 왜 필요한가
```
현재: 뉴스/온체인 데이터 미활용 (Phase 2 LLM 센티먼트는 아직 미구현)
qrak 접근: ChromaDB에 뉴스+거래이력 저장 → 유사 상황 검색으로 컨텍스트 제공
→ "지금과 비슷한 과거 상황에서 어떤 결과였는가?"를 실시간 참조
→ Phase 2(LLM 센티먼트)를 RAG로 더 풍부하게 구현
```

### 파일 구조
```
auto_trade/
├── rag/
│   ├── __init__.py
│   ├── vectorstore.py         # ChromaDB 설정/관리
│   ├── embedder.py            # 텍스트 → 임베딩 (sentence-transformers)
│   ├── news_ingester.py       # CryptoPanic/CryptoCompare 뉴스 수집 → 벡터 저장
│   ├── trade_ingester.py      # 과거 거래 결과 → 벡터 저장 (outcome_labeler 연동)
│   ├── onchain_ingester.py    # DefiLlama TVL/펀딩레이트 → 벡터 저장
│   ├── retriever.py           # 시맨틱 검색 (시그널 → 유사 상황 top-k)
│   └── context_builder.py     # 검색 결과 → LLM 프롬프트용 컨텍스트 구성
```

### 핵심 설계

#### B-1. 벡터 스토어 (`rag/vectorstore.py`)
```python
# ChromaDB 컬렉션 구조
# qrak 방식 차용: 카테고리별 분리 컬렉션

COLLECTIONS = {
    "news": {
        # 뉴스 헤드라인 + 요약
        "metadata": ["symbol", "source", "timestamp", "sentiment_score"],
        "embedding_model": "all-MiniLM-L6-v2",  # 경량, CPU OK
    },
    "trades": {
        # 과거 거래 상황 요약 (진입 조건 + 결과)
        "metadata": ["symbol", "direction", "exit_type", "roe", "gap", "timestamp"],
        "embedding_model": "all-MiniLM-L6-v2",
    },
    "market_states": {
        # 시장 레짐 스냅샷 (BTC 상태, 변동성, 펀딩레이트 등)
        "metadata": ["btc_level", "vix_crypto", "dominant_trend", "timestamp"],
        "embedding_model": "all-MiniLM-L6-v2",
    },
    "onchain": {
        # 온체인 데이터 (TVL 변화, 대규모 이체 등)
        "metadata": ["symbol", "metric_type", "value", "timestamp"],
        "embedding_model": "all-MiniLM-L6-v2",
    },
}
```

#### B-2. 시맨틱 검색기 (`rag/retriever.py`)
```python
class TradeContextRetriever:
    """
    시그널 발생 시 유사한 과거 상황을 검색하여 컨텍스트 제공
    
    qrak 차용: 시간 가중 검색 (최근 데이터에 가중치)
    """
    
    def retrieve(self, signal: dict, top_k: int = 5) -> list[dict]:
        """
        시그널 조건을 자연어 쿼리로 변환 → 유사 상황 검색
        
        쿼리 예시:
        "ETHUSDT EMA 수렴 후 롱 브레이크아웃, GAP 0.5%, ADX 35,
         BTC 필터 레벨 2, 펀딩레이트 0.02%, 변동성 증가 중"
        
        Returns:
        [
            {
                "situation": "2025-08 ETH 유사 수렴, 롱 진입",
                "outcome": "TP 도달, ROE +180%, 32캔들 보유",
                "lesson": "초기 SL 넓게 설정이 유효했음",
                "similarity": 0.87,
                "recency_weight": 0.95,
            },
            ...
        ]
        """
        pass
    
    def retrieve_news(self, symbol: str, hours_back: int = 24) -> list[dict]:
        """최근 뉴스 중 해당 코인 관련 시맨틱 검색"""
        pass
```

#### B-3. 컨텍스트 빌더 (`rag/context_builder.py`)
```python
class ContextBuilder:
    """
    검색된 RAG 결과를 LLM/RL에 주입 가능한 형태로 구성
    """
    
    def build_for_rl(self, retrieval_results: list) -> dict:
        """
        RL state에 주입할 수치 피처 생성
        
        Returns:
            {
                "similar_trade_winrate": float,   # 유사 상황 승률
                "similar_trade_avg_roe": float,    # 유사 상황 평균 ROE
                "news_sentiment_rag": float,       # RAG 뉴스 센티먼트 (-1~1)
                "historical_sl_hit_rate": float,   # 유사 상황 SL 히트율
                "context_confidence": float,       # 컨텍스트 검색 품질 (유사도)
            }
        """
        pass
    
    def build_for_llm(self, retrieval_results: list) -> str:
        """
        리스크 에이전트 LLM에 제공할 자연어 컨텍스트
        
        "과거 유사 상황 5건 분석:
         - 3건 TP 도달 (평균 ROE +156%)
         - 1건 SL 히트 (방향 맞았으나 변동성 급등)
         - 1건 타임아웃 (횡보)
         → 유사 상황 승률 60%, SL 주의 필요"
        """
        pass
```

### 데이터 소스 및 수집 전략
```
■ 뉴스 (news_ingester.py):
  소스: CryptoPanic API (무료 300건/일) + CryptoCompare News API
  수집: 15분 주기 (pipeline_runner.py 동기화)
  보관: ChromaDB 영구 저장 (시간 감쇠는 검색 가중치로 처리)

■ 과거 거래 (trade_ingester.py):
  소스: signals_all_labeled.csv (370만 건)
  초기: 전체 라벨링 데이터를 벡터 변환하여 일괄 적재
  이후: 실전 거래 결과를 실시간 적재

■ 온체인 (onchain_ingester.py):
  소스: DefiLlama API (TVL, 프로토콜 데이터)
  소스: CoinGlass (기존 coinglass_client.py 연동)
  수집: 1시간 주기

■ 시장 상태 (market_state_ingester.py):
  소스: 기존 auto_trader_v9.py의 BTC 필터, ATR, 변동성 계산
  수집: 15분 주기 (시그널 생성 시 자동 스냅샷)
```

---

## Module C: 적응형 메모리 시스템 (`memory/`)

### 목표
거래 결과에서 자동으로 규칙을 도출하고, 시간이 지나면 감쇠시키며, 지속적으로 갱신하는 "학습하는 트레이딩 브레인"

### qrak 차용 포인트
```
1. 시간 감쇠: 오래된 규칙의 가중치를 자동으로 낮춤
2. 자동 반성 루프: 거래 결과 → LLM이 분석 → 새 규칙 생성/기존 규칙 갱신
3. 영구적 시맨틱 규칙: "이런 상황에서는 이렇게 해라" 형태의 규칙을 벡터로 저장
4. 신뢰도 추적: 규칙별 적중률 추적 → 실적 나쁜 규칙 자동 비활성화
```

### 파일 구조
```
auto_trade/
├── memory/
│   ├── __init__.py
│   ├── trading_brain.py       # 핵심: 규칙 저장소 + 감쇠 엔진
│   ├── reflection_engine.py   # 거래 결과 → LLM 분석 → 규칙 생성
│   ├── rule_store.py          # 규칙 CRUD + ChromaDB 연동
│   └── decay_engine.py        # 시간 감쇠 + 성과 기반 가중치 조정
```

### 핵심 설계

#### C-1. 트레이딩 브레인 (`memory/trading_brain.py`)
```python
class TradingBrain:
    """
    qrak의 TradingBrain 컨셉 차용:
    - 코인별/패턴별 승률 추적
    - 규칙별 성과 기반 활성화/비활성화
    - 시간 감쇠 메커니즘
    """
    
    def consult(self, signal: dict, market_state: dict) -> dict:
        """
        시그널 발생 시 관련 규칙 조회
        
        Returns:
            {
                "applicable_rules": [
                    {
                        "rule": "ETH 수렴 후 롱 시 GAP 0.3~0.5 구간에서 SL을 ATR×2.5로 확대 권장",
                        "confidence": 0.78,
                        "sample_size": 23,
                        "last_validated": "2026-03-15",
                        "decay_factor": 0.92,
                    }
                ],
                "coin_winrate": 0.67,       # 해당 코인의 최근 승률
                "pattern_winrate": 0.72,     # 유사 패턴의 최근 승률
                "brain_confidence": 0.85,    # 전체 조언 신뢰도
            }
        """
        pass
```

#### C-2. 반성 엔진 (`memory/reflection_engine.py`)
```python
class ReflectionEngine:
    """
    qrak 차용: 거래 종료 후 자동으로 반성 루프 실행
    
    흐름:
    1. 거래 종료 이벤트 수신
    2. 진입 조건, 시장 상태, 결과를 수집
    3. LLM에게 분석 요청 → 교훈 도출
    4. 기존 규칙과 비교 → 강화/약화/신규 생성
    """
    
    REFLECTION_PROMPT = """
    거래 결과를 분석하세요.

    [진입 조건]
    {entry_conditions}

    [시장 상태]
    {market_state}

    [결과]
    {trade_result}

    [유사 과거 거래 {n}건]
    {similar_trades}

    다음을 도출하세요:
    1. 이 거래가 성공/실패한 핵심 요인
    2. 앞으로 유사 상황에서 적용할 규칙 (있다면)
    3. 기존 규칙 중 수정이 필요한 것 (있다면)

    JSON 형식:
    {
        "key_factor": str,
        "new_rules": [{"condition": str, "action": str, "confidence": float}],
        "rule_updates": [{"rule_id": str, "adjustment": str}],
        "lesson_learned": str
    }
    """
    
    async def reflect(self, trade_record: dict) -> dict:
        """거래 결과 분석 → 규칙 생성/갱신"""
        pass
```

#### C-3. 시간 감쇠 엔진 (`memory/decay_engine.py`)
```python
class DecayEngine:
    """
    규칙의 시간 감쇠 + 성과 기반 가중치 조정
    
    감쇠 공식: weight = base_weight × exp(-λ × days_since_creation)
    성과 조정: weight *= (적중률 / 기대 적중률)
    비활성화: weight < 0.1이면 규칙 비활성화
    """
    
    DECAY_LAMBDA = 0.01  # 약 70일 반감기
    MIN_WEIGHT = 0.1     # 이 이하면 비활성화
    
    def decay_all(self):
        """매일 1회 실행: 전체 규칙 감쇠 적용"""
        pass
    
    def update_performance(self, rule_id: str, hit: bool):
        """규칙 적용 결과 피드백 → 가중치 조정"""
        pass
```

---

## Module D: 리스크 관리 에이전트 (`risk/`)

### 목표
AutoHedge의 멀티에이전트 파이프라인을 차용하여, 포지션별 리스크 분석 → 헷지/SL조정/레버리지 변경을 제안하는 독립 에이전트 시스템 구축

### 왜 독립 모듈인가
```
기존 Phase 4(동적 SL)는 RL 기반 → 학습 데이터 필요, 구축 시간 2~3주
리스크 에이전트는 LLM 기반 → 즉시 구축 가능, 규칙+LLM 하이브리드
두 시스템이 병렬 운영되며 서로 보완:
  - RL(Phase 4): 15분마다 SL 미세조정 (수치 최적화)
  - Risk Agent: 큰 그림의 리스크 판단 (헷지, 레버리지, 포트폴리오 레벨)
```

### 파일 구조
```
auto_trade/
├── risk/
│   ├── __init__.py
│   ├── pipeline.py            # 멀티에이전트 파이프라인 오케스트레이터
│   ├── analyst_agent.py       # 시장 상태 종합 분석 (Vision+RAG 활용)
│   ├── risk_agent.py          # 포지션별 리스크 평가 + SL/TP 제안
│   ├── hedge_agent.py         # 헷지 전략 판단 + 반대 포지션 제안
│   ├── leverage_agent.py      # 레버리지 동적 조절 (10x~20x)
│   ├── models.py              # Pydantic 데이터 모델 (RiskDecision 등)
│   └── config.py              # 리스크 파라미터 설정
```

### 핵심 설계

#### D-1. 파이프라인 (`risk/pipeline.py`)
```python
# AutoHedge 차용: Director → Quant → Risk → Execution 순차 파이프라인
# 우리 버전: Analyst → Risk → Hedge → Leverage

class RiskPipeline:
    """
    시그널 또는 포지션 업데이트 시 실행되는 리스크 분석 파이프라인
    
    실행 트리거:
    1. 새 시그널 발생 시 (진입 전 리스크 점검)
    2. 보유 포지션의 15분 업데이트 시 (동적 리스크 관리)
    3. 급격한 시장 변동 감지 시 (긴급 리스크 점검)
    """
    
    async def evaluate(self, context: PipelineContext) -> RiskDecision:
        # Step 1: Analyst — 시장 상태 종합 분석
        market_analysis = await self.analyst.analyze(context)
        
        # Step 2: Risk — 포지션별 리스크 평가
        risk_assessment = await self.risk_agent.assess(context, market_analysis)
        
        # Step 3: Hedge — 헷지 필요성 판단
        hedge_recommendation = await self.hedge_agent.recommend(context, risk_assessment)
        
        # Step 4: Leverage — 레버리지 조절
        leverage_recommendation = await self.leverage_agent.recommend(context, risk_assessment)
        
        return RiskDecision(
            risk_level=risk_assessment.level,
            sl_adjustment=risk_assessment.sl_suggestion,
            tp_adjustment=risk_assessment.tp_suggestion,
            sizing_mult=risk_assessment.sizing_mult,
            hedge_action=hedge_recommendation,
            leverage=leverage_recommendation,
            reasoning=self._compile_reasoning(market_analysis, risk_assessment),
        )
```

#### D-2. 헷지 에이전트 (`risk/hedge_agent.py`)
```python
class HedgeAgent:
    """
    수렴 시 헷지 전략 판단
    
    ★ 핵심: 20x 레버리지에서 급격한 변동은 치명적
    → 수렴 구간에서 불확실성이 높을 때 반대 포지션으로 리스크 분산
    
    헷지 전략 유형:
    1. 동일 코인 반대 포지션 (바이낸스 헷지 모드)
       - 롱 포지션 보유 중 → 50% 크기의 숏 오픈
       - 방향이 확정되면 헷지 해제
       
    2. 상관 코인 헷지 (페어 트레이딩)
       - BTC 롱 보유 중 → 상관계수 높은 ETH 숏 오픈
       - 시스테매틱 리스크 상쇄
       
    3. 부분 청산 헷지
       - 전체 포지션의 30~50% 부분 청산으로 익스포저 축소
       - 가장 단순하고 안전한 방법
       
    4. 스테이블코인 전환
       - 극단적 불확실성 시 전량 청산 → USDT 대기
    """
    
    HEDGE_PROMPT = """
    현재 포지션과 시장 상태를 분석하여 헷지 필요성을 판단하세요.
    
    [현재 포지션]
    {positions}
    
    [시장 상태]
    {market_analysis}
    
    [리스크 평가]
    {risk_assessment}
    
    [RAG 컨텍스트: 유사 과거 상황]
    {rag_context}
    
    다음 중 하나를 선택하고 근거를 제시하세요:
    {
        "action": "NONE" | "PARTIAL_CLOSE" | "OPPOSITE_POSITION" | "CORRELATED_HEDGE" | "FULL_EXIT",
        "size_pct": float,         // 헷지 크기 (전체 포지션 대비 %)
        "hedge_symbol": str | null, // 상관 코인 헷지 시 심볼
        "urgency": "low" | "medium" | "high",
        "reasoning": str,
        "exit_condition": str       // 헷지 해제 조건
    }
    """
    
    async def recommend(self, context, risk_assessment) -> HedgeRecommendation:
        """헷지 전략 추천"""
        pass
```

#### D-3. 레버리지 에이전트 (`risk/leverage_agent.py`)
```python
class LeverageAgent:
    """
    시장 상황에 따라 레버리지를 동적으로 조절 (10x~20x)
    
    ★ 핵심 로직:
    - 변동성 낮음 + 방향 확신 높음 → 20x (최대)
    - 변동성 높음 + 수렴 중 → 10x~15x (보수적)
    - BTC 급변 + 불확실 → 10x (최소)
    
    ATR 기반 변동성 → 레버리지 매핑:
      ATR < 1.0%  → 20x (저변동)
      ATR 1.0~2.0% → 15x (보통)
      ATR 2.0~3.0% → 12x (고변동)
      ATR > 3.0%   → 10x (극고변동)
      
    추가 조정 요인:
      - BTC 필터 레벨 4+ → -2x
      - 펀딩레이트 극단값 → -3x
      - Vision AI high risk → -2x
      - 연속 손실 3회+ → -3x
    """
    
    async def recommend(self, context, risk_assessment) -> LeverageRecommendation:
        """레버리지 조절 추천"""
        pass
```

#### D-4. 데이터 모델 (`risk/models.py`)
```python
from pydantic import BaseModel
from enum import Enum

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class HedgeAction(str, Enum):
    NONE = "none"
    PARTIAL_CLOSE = "partial_close"
    OPPOSITE_POSITION = "opposite_position"
    CORRELATED_HEDGE = "correlated_hedge"
    FULL_EXIT = "full_exit"

class RiskDecision(BaseModel):
    """리스크 파이프라인의 최종 출력"""
    risk_level: RiskLevel
    sl_adjustment: float | None      # SL 조정 제안 (ATR 단위)
    tp_adjustment: float | None      # TP 조정 제안
    sizing_mult: float               # 사이징 배수 (0.3 ~ 1.5)
    hedge_action: HedgeAction
    hedge_size_pct: float             # 헷지 크기 (0~100%)
    hedge_symbol: str | None
    leverage: int                     # 추천 레버리지 (10~20)
    reasoning: str                    # 판단 근거 (텔레그램 알림용)
    urgency: str                      # 긴급도
    
class PipelineContext(BaseModel):
    """파이프라인 입력 컨텍스트"""
    signal: dict | None               # 시그널 (진입 전 평가 시)
    positions: list[dict]             # 현재 보유 포지션
    market_state: dict                # 시장 상태
    vision_analysis: dict | None      # Vision AI 결과
    rag_context: dict | None          # RAG 검색 결과
    memory_rules: list[dict] | None   # 메모리 시스템 규칙
    account_state: dict               # 계좌 상태 (잔고, 마진율 등)
```

---

## 실행 순서 및 로드맵

```
═══════════════════════════════════════════════════════════════
Phase A: 기반 인프라 (Week 1) — 모든 모듈의 공통 기반
═══════════════════════════════════════════════════════════════

Day 1-2: RAG 인프라
  ├── ChromaDB 설치 + 컬렉션 구조 설정 (rag/vectorstore.py)
  ├── 임베딩 모듈 (rag/embedder.py — sentence-transformers)
  ├── 370만 시그널 → 벡터 변환 일괄 적재 (rag/trade_ingester.py)
  └── 시맨틱 검색 기본 동작 확인 (rag/retriever.py)

Day 2-3: 뉴스 수집 파이프라인
  ├── CryptoPanic API 연동 (rag/news_ingester.py)
  ├── 15분 주기 수집 → ChromaDB 저장
  └── 뉴스 시맨틱 검색 테스트

Day 3-4: Vision AI 기본
  ├── Plotly/mplfinance 차트 렌더링 (vision/chart_renderer.py)
  ├── Claude Vision API 연동 (vision/pattern_analyzer.py)
  └── 샘플 차트 10건으로 패턴 분석 품질 확인

═══════════════════════════════════════════════════════════════
Phase B: 리스크 에이전트 (Week 2) — AutoHedge 차용
═══════════════════════════════════════════════════════════════

Day 5-6: 리스크 모델 + 분석 에이전트
  ├── Pydantic 데이터 모델 (risk/models.py)
  ├── Analyst Agent (risk/analyst_agent.py — Vision+RAG 통합)
  └── Risk Agent (risk/risk_agent.py — SL/TP/사이징 제안)

Day 7-8: 헷지 + 레버리지 에이전트
  ├── Hedge Agent (risk/hedge_agent.py — 4가지 헷지 전략)
  ├── Leverage Agent (risk/leverage_agent.py — 10x~20x 동적 조절)
  └── Pipeline 통합 (risk/pipeline.py)

Day 9-10: 파이프라인 테스트
  ├── 과거 시그널 100건으로 파이프라인 드라이런
  ├── RiskDecision 출력 검증 (합리성 체크)
  └── 텔레그램 알림 연동 (리스크 판단 결과 전송)

═══════════════════════════════════════════════════════════════
Phase C: 적응형 메모리 (Week 3) — qrak 차용
═══════════════════════════════════════════════════════════════

Day 11-12: 메모리 시스템 기본
  ├── 규칙 저장소 (memory/rule_store.py — ChromaDB 연동)
  ├── 시간 감쇠 엔진 (memory/decay_engine.py)
  └── 트레이딩 브레인 (memory/trading_brain.py)

Day 13-14: 반성 엔진
  ├── 거래 결과 → LLM 분석 → 규칙 생성 (memory/reflection_engine.py)
  ├── 370만 시그널 중 랜덤 1000건으로 규칙 부트스트랩
  └── 규칙 품질 검증 (수동 리뷰)

═══════════════════════════════════════════════════════════════
Phase D: 통합 + 검증 (Week 4) — 기존 시스템과 연결
═══════════════════════════════════════════════════════════════

Day 15-16: auto_trader_v9.py 통합
  ├── Vision + RAG + Memory → enriched_state 생성 파이프라인
  ├── Risk Pipeline → RiskDecision → auto_trader_v9.py 래퍼
  ├── RL 게이트키퍼(Phase 1~4)와 Risk Agent 의사결정 병합 로직
  └── 텔레그램 알림 강화 (Vision 분석 + 리스크 판단 포함)

Day 17-18: 백테스트 검증
  ├── 과거 3개월 시그널로 전체 파이프라인 시뮬레이션
  ├── 기존 시스템 vs Vision+RAG+Risk Agent 비교
  ├── Walk-Forward 3윈도우 검증
  └── PF, Sharpe, MDD 비교

Day 19-20: Paper Trading 시작
  ├── 실제 시장 데이터로 가상 실행
  ├── 기존 시스템과 병렬 운영
  └── 최소 2주 비교 후 실전 전환 판단

═══════════════════════════════════════════════════════════════
이후: 기존 Phase 4(동적 SL RL)과 통합
═══════════════════════════════════════════════════════════════

- Phase 4 RL 에이전트의 state에 Vision/RAG/Memory 피처 추가
- Risk Agent의 RiskDecision을 RL의 보조 입력으로 활용
- 궁극적으로: RL(미세조정) + Risk Agent(거시적 판단) 듀얼 시스템
```

---

## 디렉토리 구조 (최종)

```
auto_trade/
├── CLAUDE.md
├── auto_trader_v9.py
├── convergence_strategy.py
├── cloud_scan_v2.py
├── backtest_entry_quality.py
├── outcome_labeler.py
├── download_data.py
├── coinglass_client.py
│
├── vision/                    ★ NEW: Vision AI 모듈
│   ├── __init__.py
│   ├── chart_renderer.py
│   ├── pattern_analyzer.py
│   ├── vision_scorer.py
│   └── cache.py
│
├── rag/                       ★ NEW: RAG 엔진
│   ├── __init__.py
│   ├── vectorstore.py
│   ├── embedder.py
│   ├── news_ingester.py
│   ├── trade_ingester.py
│   ├── onchain_ingester.py
│   ├── retriever.py
│   └── context_builder.py
│
├── memory/                    ★ NEW: 적응형 메모리
│   ├── __init__.py
│   ├── trading_brain.py
│   ├── reflection_engine.py
│   ├── rule_store.py
│   └── decay_engine.py
│
├── risk/                      ★ NEW: 리스크 관리 에이전트
│   ├── __init__.py
│   ├── pipeline.py
│   ├── analyst_agent.py
│   ├── risk_agent.py
│   ├── hedge_agent.py
│   ├── leverage_agent.py
│   ├── models.py
│   └── config.py
│
├── rl/                        (기존 계획 유지)
│   ├── env/
│   ├── agent/
│   ├── features/
│   └── train.py
│
├── llm/                       (기존 계획 — RAG 모듈과 통합)
│   ├── sentiment/
│   ├── prompts/
│   └── pipeline.py
│
├── data/
│   ├── collector/
│   └── store/
│
├── experiments/
│   ├── configs/
│   ├── logs/
│   └── results/
│
└── tasks/
    ├── experiment_plan_rl_llm.md
    ├── plan_vision_rag_risk_agent.md  ★ 이 문서
    ├── todo.md
    └── lessons.md
```

---

## 필요 라이브러리 (추가분)

```
# Vision AI
plotly>=5.20.0                  # 차트 렌더링
kaleido>=0.2.1                  # Plotly → PNG 변환
mplfinance>=0.12.10             # 대안 차트 렌더링
anthropic>=0.25.0               # Claude Vision API

# RAG
chromadb>=0.5.0                 # 벡터 스토어
sentence-transformers>=3.0.0    # 임베딩 모델 (all-MiniLM-L6-v2)
langchain>=0.2.0                # RAG 파이프라인 (선택)

# 리스크 에이전트
pydantic>=2.7.0                 # 데이터 모델
aiohttp>=3.9.0                  # 비동기 HTTP (멀티 API 호출)

# 데이터 소스
ccxt>=4.3.0                     # 멀티 거래소 (선택, 확장용)
defillama-python>=0.1.0         # DefiLlama API (선택)

# 기존 유지
stable-baselines3>=2.3.0
gymnasium>=0.29.0
d3rlpy>=2.6.0
transformers>=4.40.0            # FinBERT
torch>=2.2.0
```

---

## 비용 추정 (월간)

```
■ Claude Vision API (차트 분석):
  시그널 150건/월 × $0.003/건 = $0.45/월

■ Claude Haiku API (리스크 에이전트 + 반성 루프):
  리스크 평가: 150건 × $0.001 = $0.15/월
  반성 루프: 150건 × $0.001 = $0.15/월
  
■ 뉴스 API:
  CryptoPanic 무료 티어: $0/월

■ ChromaDB:
  로컬 실행: $0/월

■ 임베딩:
  sentence-transformers 로컬: $0/월

총 예상: ~$1/월 (시그널 기반 호출만)
극단 시: ~$5/월 (보유 중 15분마다 리스크 평가 시)
```

---

## 위험 요소 및 대응

| 위험 | 확률 | 대응 |
|------|------|------|
| Vision AI 패턴 인식이 노이즈만 추가 | 중간 | 100건 샘플 테스트 → 기존 수치 지표와 상관관계 검증. 독립적 가치 없으면 제외 |
| RAG 검색이 무관한 결과 반환 | 중간 | 유사도 threshold (0.7+)로 필터링. 저품질 결과는 state에 주입하지 않음 |
| 리스크 에이전트가 과도하게 보수적 | 높음 | 초기엔 "제안만" 모드 (실행은 기존 시스템). 백테스트에서 행동 분석 후 자율도 점진적 확대 |
| 헷지 포지션이 양방향 손실 유발 | 중간 | 헷지 크기 상한(포지션의 50%), 헷지 비용(수수료) 고려한 시뮬레이션 필수 |
| 레버리지 낮추면 수익률도 감소 | 확실 | 레버리지 조절은 MDD 대비 수익률 trade-off. Sharpe Ratio 기준으로 평가 |
| 메모리 시스템이 잘못된 규칙 학습 | 중간 | 규칙 최소 검증 횟수(10회+), 자동 감쇠(70일 반감기), 수동 리뷰 주기(주 1회) |
| LLM API 지연으로 시그널 놓침 | 낮음 | 비동기 호출 + 타임아웃 3초 + 폴백(API 실패 시 수치 피처만으로 진행) |
| ChromaDB 로컬 저장소 용량 | 낮음 | 370만 시그널 벡터 ≈ 2~3GB. 디스크 여유 확인 후 시작 |

---

## 성공 기준

### Phase A 완료 기준 (Week 1)
- [ ] ChromaDB에 370만 시그널 벡터 적재 완료
- [ ] 시맨틱 검색으로 유사 시그널 top-5 검색 품질 > 0.7 유사도
- [ ] Vision AI 차트 분석이 10건 중 7건+ 합리적 패턴 식별
- [ ] 뉴스 수집 파이프라인 15분 주기 정상 동작

### Phase B 완료 기준 (Week 2)
- [ ] 리스크 파이프라인이 시그널 입력 → RiskDecision 출력 정상
- [ ] 과거 시그널 100건 드라이런에서 합리적 판단 비율 > 80%
- [ ] 헷지 제안이 실제 급변 상황에서 적절하게 발동 (과거 사례 검증)
- [ ] 레버리지 조절이 변동성과 역상관 관계 (ATR↑ → Leverage↓)

### Phase C 완료 기준 (Week 3)
- [ ] 1000건 거래에서 규칙 50개+ 자동 생성
- [ ] 규칙의 적중률이 랜덤(50%) 대비 유의미하게 높음 (> 60%)
- [ ] 시간 감쇠가 오래된 규칙을 적절히 비활성화

### Phase D 완료 기준 (Week 4)
- [ ] 전체 파이프라인이 15초 이내에 RiskDecision 출력
- [ ] 백테스트에서 기존 대비 MDD 10%+ 개선 (Sharpe 동등 이상)
- [ ] Paper Trading 시작 가능 상태

### 궁극적 목표
- [ ] Risk Agent + RL(Phase 4) 통합으로 PF ≥ 1.30
- [ ] "방향 맞았지만 SL 히트" 비율: 80.1% → 50% 이하
- [ ] MDD: 현재 대비 20%+ 개선
- [ ] 레버리지 동적 조절로 극단 손실 시나리오 방지

---

## 기존 계획과의 관계

```
experiment_plan_rl_llm.md (v4)     이 계획 (Vision+RAG+Risk)
═════════════════════════════      ════════════════════════════
Phase 0: 데이터 인프라     ─────→  Phase A에서 확장 (RAG 추가)
Phase 1: RL 환경           ─────→  유지 (risk/ 피처를 state에 추가)
Phase 1.5: 방향 필터       ─────→  유지 (Vision 피처 추가)
Phase 2: LLM 센티먼트      ─────→  RAG 모듈로 대체/확장
Phase 3: 통합 A/B          ─────→  Phase D에서 Risk Agent 포함
Phase 4: 동적 SL ★최우선   ─────→  유지 + Risk Agent가 거시적 보완
Phase 5: 고급 최적화       ─────→  Memory + 멀티에이전트로 확장
```

★ 기존 Phase 4(동적 SL RL)는 여전히 최우선. 이 계획은 병렬로 진행하며,
  궁극적으로 RL(수치 최적화) + Risk Agent(거시적 판단)의 듀얼 시스템으로 수렴.
