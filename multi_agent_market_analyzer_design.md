# 멀티 에이전트 시장 분석 시스템 설계 문서

## 프로젝트 개요

**목적**: Claude AI 에이전트를 활용한 크립토 시장 분석 시스템 구축  
**대상**: Binance 선물 자동매매 시스템 (v9)과 연동  
**특징**: 20배 레버리지 트레이딩 환경에 최적화된 리스크 관리

---

## 1. 시스템 시나리오

### 전체 흐름
1. **auto_trader_v9**로 진입 포지션 진행 (EMA 12/26 수렴 돌파, 15분봉)
2. 멀티 에이전트 시스템이 시장 상황 분석 및 토론
3. 분석 결과를 바탕으로 리스크 관리 및 포지션 관리

### 핵심 제약
- 20배 레버리지 = 약 4% 역행 시 청산
- 모든 판단은 "높은 확률"보다 "생존"이 우선

---

## 2. 에이전트 아키텍처

### 전체 구조

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MARKET CONTEXT LAYER                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │ Agent 1  │  │ Agent 2  │  │ Agent 3  │  │ Agent 4  │            │
│  │ BTC 구조 │  │ 매크로   │  │ 자산간   │  │ 알트     │            │
│  │ 분석가   │  │ 분석가   │  │ 상관관계 │  │ 생태계   │            │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘            │
│       │             │             │             │                   │
│       └─────────────┴──────┬──────┴─────────────┘                   │
│                            ▼                                        │
│                   ┌─────────────────┐                               │
│                   │ 시장 컨센서스    │                               │
│                   │ (Moderator)     │                               │
│                   └────────┬────────┘                               │
└────────────────────────────┼────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       POSITION LAYER                                │
│                    ┌──────────────┐                                 │
│                    │   Agent 5    │                                 │
│                    │ 포지션 심판  │                                 │
│                    │              │                                 │
│                    │ • 각 포지션  │                                 │
│                    │   돌파 확률  │                                 │
│                    │ • 홀딩 시간  │                                 │
│                    │ • 리스크 점수│                                 │
│                    └──────┬───────┘                                 │
│                           │                                         │
│                           ▼                                         │
│              ┌────────────────────────┐                             │
│              │    RISK DECISION       │                             │
│              │ (20x 레버리지 기준)    │                             │
│              │                        │                             │
│              │ • 포지션 유지/축소     │                             │
│              │ • TP/SL 동적 조정      │                             │
│              │ • 신규 진입 허용 여부  │                             │
│              └────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 각 에이전트 상세 설계

### Agent 1: BTC 구조 분석가

**역할**: BTC 가격 구조와 핵심 레벨 분석

**데이터 소스**:
- 즉시 가용:
  - Binance API (가격, 거래량, 오더북)
  - TradingView 매물대 (web_search로 분석글 수집)
  - Coinglass (청산 히트맵, OI 변화)
- 나중 연동:
  - Glassnode (SOPR, NUPL, 거래소 잔고)
  - CryptoQuant (고래 움직임)

**분석 항목**:
- 주요 매물대 (Volume Profile 기반)
- 청산 클러스터 위치 (20x 레버리지 감안)
- 최근 돌파 시도 성공/실패 패턴
- 돌파 후 반동(rejection) 강도

**출력 형식**:
```yaml
btc_structure:
  key_resistance: [68500, 69200, 70000]
  key_support: [66800, 65500, 64000]
  liquidation_clusters:
    longs: [65000, 63500]  # 여기 닿으면 롱 청산 캐스케이드
    shorts: [70500, 72000]
  breakout_probability: 0.35  # 상방 돌파 확률
  recent_rejection_strength: "strong"
```

---

### Agent 2: 매크로 분석가

**역할**: 전통 금융시장 및 거시경제 환경 분석

**데이터 소스**:
- Yahoo Finance / Investing.com (S&P500, 나스닥, DXY)
- FRED (금리, 국채 수익률)
- CME FedWatch (금리 전망)
- 경제 캘린더 (이벤트 일정)

**분석 항목**:
- 당일/익일 주요 경제 이벤트
- Risk-on vs Risk-off 환경 판단
- DXY 방향성과 BTC 역상관 확인
- VIX 수준 (공포 지수)

**출력 형식**:
```yaml
macro_environment:
  risk_appetite: "risk-off"  # risk-on, neutral, risk-off
  upcoming_events:
    - event: "FOMC Minutes"
      time: "2026-04-01 18:00 UTC"
      impact: "high"
  dxy_trend: "bullish"  # BTC에 부정적
  equity_correlation: 0.72  # BTC-나스닥 상관계수
  recommendation: "defensive"
```

---

### Agent 3: 상관관계 분석가

**역할**: BTC와 타 자산 간 연동성 분석

**데이터 소스**:
- BTC vs S&P500/나스닥 상관계수 (rolling 30일)
- BTC vs 금 상관계수
- BTC vs DXY 역상관

**분석 항목**:
- BTC가 "디지털 금"으로 움직이는가 vs "테크 주식"으로 움직이는가
- 상관관계 regime 변화 감지
- 펀드 리밸런싱 시점 추정 (월말, 분기말)

**출력 형식**:
```yaml
correlation_regime:
  btc_behaves_as: "tech_stock"  # gold, tech_stock, independent
  correlation_stability: "stable"  # stable, shifting, breakdown
  rebalancing_risk: false  # 월말 리밸런싱 주의
```

---

### Agent 4: 알트코인 생태계 분석가

**역할**: 알트코인 시장 전체 동향 및 현재 포지션 코인 분석

**데이터 소스**:
- Binance 전체 티커 (상위 50개 알트)
- BTC.D (비트코인 도미넌스)
- 섹터별 퍼포먼스 (AI, 밈, L1, DeFi 등)

**분석 항목**:
- 분산도 분석:
  - 알트코인 간 방향 일치도 (Dispersion Index)
  - BTC 상승 시 알트 베타 (1보다 크면 과민반응)
  - 섹터별 강세/약세 판단
- 현재 포지션 분석:
  - 각 포지션 코인의 BTC 연동성
  - 포지션 방향(롱/숏)에 유리한 환경인가
  - 해당 코인이 속한 섹터 모멘텀

**출력 형식**:
```yaml
altcoin_ecosystem:
  dispersion_index: 0.35  # 0=완전동조, 1=완전분산
  market_consensus: "unclear"  # bullish, bearish, unclear
  leading_sector: "AI"
  lagging_sector: "DeFi"
  btc_dominance_trend: "rising"  # 알트에 불리
  
position_context:
  - symbol: "SUIUSDT"
    btc_beta: 1.4
    sector: "L1"
    sector_momentum: "neutral"
    favorable_for: "long"
  - symbol: "WLDUSDT"
    btc_beta: 1.8
    sector: "AI"
    sector_momentum: "bullish"
    favorable_for: "long"
```

**Agent 4 가설 검증**:
- "알트들이 서로 다른 방향으로 움직이면 시장이 불안정하다" → **대체로 타당**
- 단, 상관관계가 낮아지는 게 항상 "위험"은 아님 (섹터 로테이션일 수 있음)
- "분산도 높음 = 즉시 청산"이 아니라 "분산도 높음 = 포지션 사이즈 보수적으로"

---

### Agent 5: 포지션 심판

**역할**: 현재 보유 포지션 각각에 대한 심층 분석 및 판결

**입력**:
- Agent 1~4의 분석 결과
- 현재 오픈 포지션 목록 (심볼, 방향, 진입가, 현재가, PnL)
- 20x 레버리지 사실

**분석 항목**:
- 각 포지션별:
  - 목표가 도달 확률 (매물대 + 시장 환경 종합)
  - 청산가까지 여유 (20x 기준 ~4% 역행 시 청산)
  - 독자 이슈 존재 여부 (업그레이드, 파트너십, 악재 등)
  - 권장 홀딩 시간

**출력 형식**:
```yaml
position_verdicts:
  - symbol: "SUIUSDT"
    direction: "long"
    entry: 1.85
    current: 1.92
    pnl_percent: 3.8
    verdict:
      breakout_probability: 0.55
      next_resistance: 2.05
      risk_to_liquidation: "safe"  # safe, caution, danger
      independent_catalyst: "Sui ecosystem growth news"
      recommended_action: "hold"
      hold_duration: "4-8 hours"
      confidence: 0.7
      
  - symbol: "WLDUSDT"
    direction: "long"
    entry: 2.45
    current: 2.41
    pnl_percent: -1.6
    verdict:
      breakout_probability: 0.40
      next_resistance: 2.60
      risk_to_liquidation: "caution"  # 20x면 -4%에서 청산
      independent_catalyst: "None detected"
      recommended_action: "reduce_size"
      hold_duration: "reassess in 2 hours"
      confidence: 0.6
```

---

## 4. 에이전트 토론 프로토콜

### 시스템 프롬프트

```python
DISCUSSION_SYSTEM_PROMPT = """
당신들은 20배 레버리지 선물 트레이딩에 대해 토론하는 전문가 패널입니다.

핵심 제약:
- 20배 레버리지 = 약 4% 역행 시 청산
- 따라서 모든 판단은 "높은 확률"보다 "생존"이 우선
- 의견이 갈리면 보수적 판단을 우선

토론 규칙:
1. 각 에이전트는 자신의 분석을 먼저 제시
2. 다른 에이전트의 분석과 충돌하는 부분을 지적
3. 특히 "내 분석에서는 괜찮아 보이지만, Agent X의 우려가 맞다면 위험"을 명시
4. 최종적으로 각자의 신뢰도(0-1)와 함께 포지션별 권고안 제시

20배 기준 리스크 분류:
- SAFE: 청산가까지 충분한 여유, 시장 환경 우호적
- CAUTION: 하나 이상의 불확실성 존재, 포지션 축소 권고
- DANGER: 복수의 위험 신호, 즉시 청산 또는 대폭 축소 권고
"""
```

---

## 5. 기존 유사 프로젝트 리서치 결과

### 5.1 TradingAgents (TauricResearch) — 가장 관련성 높음

**GitHub**: https://github.com/TauricResearch/TradingAgents  
**논문**: arXiv:2412.20138

**핵심 특징**:
- 실제 트레이딩 펌 구조를 모방한 멀티 에이전트 LLM 프레임워크
- Fundamental, Sentiment, Technical Analyst + Bull/Bear Researcher + Risk Manager + Fund Manager
- 구조화된 리포트 + 선택적 자연어 토론의 하이브리드 방식
- Quick-thinking LLM(데이터 수집용)과 Deep-thinking LLM(분석용) 분리
- LangGraph 기반 아키텍처

**차용 포인트**:
- 구조화된 출력 스키마
- Bull/Bear Researcher 토론 구조
- Quick/Deep LLM 분리 전략

**보완 필요**:
- 주식 시장 대상이라 크립토 특화 지표 없음
- 레버리지 트레이딩 리스크 관리 로직 없음
- 일봉 기준이라 15분봉 고빈도 트레이딩에 직접 적용 불가

---

### 5.2 AI Hedge Fund (virattt)

**GitHub**: https://github.com/virattt/ai-hedge-fund

**핵심 특징**:
- 실제 투자 대가들의 철학을 가진 에이전트들이 협업
  - Aswath Damodaran (밸류에이션)
  - Ben Graham (가치투자)
  - Bill Ackman (행동주의)
  - Cathie Wood (성장투자)
  - Charlie Munger, Michael Burry 등
- 갈등하는 투자 철학을 가진 에이전트들의 토론과 합의 도출

**차용 포인트**:
- 투자 대가 페르소나 개념 → 크립토 버전 가능
- 갈등 상황에서의 합의 도출 메커니즘
- 의사결정 투명성 (추론 과정 기록)

---

### 5.3 CryptoTrade (Xtra-Computing)

**GitHub**: https://github.com/Xtra-Computing/CryptoTrade  
**논문**: EMNLP 2024

**핵심 특징**:
- 온체인 + 오프체인 데이터 결합
- Reflective 메커니즘 (이전 거래 결정 분석으로 개선)
- BTC, ETH, SOL 각각에 대한 Bull/Bear 마켓 테스트

**차용 포인트**:
- Reflection 메커니즘 — 과거 판단 성공/실패 학습
- 온체인 데이터 통합 구조

---

### 5.4 FS-ReasoningAgent — 사실 vs 주관 분리

**논문**: arXiv:2410.12464

**핵심 인사이트**:
- 강력한 LLM이 거래에서 항상 우수하지 않음 (사실적 정보 선호 편향 때문)
- 추론 과정을 사실적 요소와 주관적 요소로 분리하면 더 높은 수익
- **주관적 뉴스 의존 → 상승장에서 높은 수익**
- **사실적 정보 집중 → 하락장에서 나은 결과**

**차용 포인트 (매우 중요)**:
- Agent 1,3은 Factual 성격, Agent 4,5는 Subjective 성격 부여
- 시장 상황에 따른 가중치 조절

---

### 5.5 FinMem — 계층적 메모리 시스템

**GitHub**: https://github.com/pipiku915/FinMem-LLM-StockTrading

**핵심 특징**:
- Profiling, Memory, Decision-making 세 가지 핵심 모듈
- 메모리 모듈이 인간 트레이더의 인지 구조와 일치
- 에이전트 자기 진화, 새로운 투자 단서에 민첩하게 반응

**차용 포인트**:
- 계층적 메모리: 단기(최근 분석), 중기(패턴 인식), 장기(시장 사이클 학습)
- Character Design: 에이전트에 일관된 "성격" 부여

---

### 5.6 LLM_trader (qrak)

**GitHub**: https://github.com/qrak/LLM_trader

**핵심 특징**:
- Vision AI 차트 분석 + RAG 엔진
- ChromaDB 벡터 스토어로 시맨틱 거래 검색
- 자동 반성 루프를 통한 영구적 시맨틱 규칙 생성
- 다중 거래소 집계 (Binance, KuCoin, Gate.io 등)

**차용 포인트**:
- Vision AI 차트 분석 (Claude 비전 기능 활용)
- RAG로 뉴스/분석 통합

---

### 5.7 AutoHedge (Swarm Corporation)

**GitHub**: https://github.com/The-Swarm-Corporation/AutoHedge

**핵심 특징**:
- 엔터프라이즈급 자율 에이전트 헤지펀드
- Swarm Intelligence + 특화 AI 에이전트
- 현재 Solana에서 완전 자율 트레이딩 지원

**차용 포인트**:
- 실제 실행 파이프라인 아키텍처
- 멀티 에이전트 → 단일 실행 결정 수렴 구조

---

## 6. 데이터 플랫폼 분석

### Glassnode
- 주요 블록체인 네트워크의 건강, 행동, 경제적 조건 반영
- 기저 시장 구조 이해에 특화
- Professional 플랜: $799/월

### CryptoQuant
- 시장 타이밍, 유동성 추적, 거래소 행동에 집중
- 마이너, 고래, 거래소의 블록체인 상호작용 실시간 추적
- Exchange Whale Ratio 등 독자 지표
- Professional 플랜: $99/월

### Coinglass
- 파생상품 시장 데이터 특화
- 청산 히트맵, 오픈 인터레스트, 펀딩 레이트
- 롱/숏 비율, 테이커 매수/매도 비율
- API 제공

---

## 7. 차용 아이디어 종합

### A. 아키텍처 레벨

| 출처 | 아이디어 | 적용 방안 |
|------|----------|-----------|
| TradingAgents | 구조화된 리포트 + 선택적 토론 | Agent 출력을 JSON 스키마로 표준화 |
| TradingAgents | Quick/Deep LLM 분리 | 데이터 수집은 Haiku, 분석/토론은 Sonnet |
| AI Hedge Fund | 갈등하는 철학의 에이전트 토론 | Bull Agent vs Bear Agent 도입 |
| FS-ReasoningAgent | Factual vs Subjective 분리 | Agent 1,3은 Factual, Agent 4,5는 Subjective |
| FinMem | 계층적 메모리 | 단기/중기/장기 메모리 구조 |

### B. 데이터 소스 레벨

| 출처 | 아이디어 | 적용 방안 |
|------|----------|-----------|
| CryptoTrade | 온체인 + 오프체인 결합 | Coinglass(즉시) + Glassnode(나중) + 웹 검색 |
| LLM_trader | RAG 뉴스 통합 | CryptoCompare 뉴스 API 연동 |
| Coinglass | 청산 히트맵 | Agent 1의 핵심 데이터 소스 |
| CoinMarketCap | Altcoin Season Index | Agent 4의 분산도 계산 참고 |

### C. 리스크 관리 레벨

| 출처 | 아이디어 | 적용 방안 |
|------|----------|-----------|
| TradingAgents | Risk Management Team 토론 | 20배 레버리지 특화 리스크 팀 |
| FS-ReasoningAgent | 시장 상황별 가중치 | 하락장에서 보수적 Agent 가중치 상향 |
| 신규 개발 | 청산 거리 기반 리스크 | 20배 기준 ~4% 역행 = 청산 |

---

## 8. 차별화 포인트 (신규 개발 필요)

기존 프로젝트들에서 찾아볼 수 없는 영역:

1. **20배 레버리지 특화 리스크 프레임워크**
   - 청산 거리 계산
   - 롱/숏 청산 클러스터 회피 로직

2. **15분봉 고빈도 의사결정**
   - 기존 프로젝트들은 일봉 기준
   - 더 빠른 의사결정 주기에 맞는 경량화 필요

3. **포지션 유지 시간 추정**
   - "몇 시간 홀딩 가능한가" 판단
   - Agent 5의 고유 역할

4. **기존 v9 시스템과의 통합**
   - TA 기반 진입 + LLM 기반 리스크 모니터링의 하이브리드

---

## 9. 개발 로드맵

### Phase 1: 핵심 인프라 (1주)

```
tasks/todo.md:
[ ] 기본 에이전트 프레임워크 구축
    [ ] AgentBase 클래스 (API 호출, 웹 검색 래핑)
    [ ] 에이전트 간 메시지 프로토콜 정의
    [ ] 토론 오케스트레이터 구현
[ ] 데이터 수집 모듈
    [ ] Binance 실시간 데이터 (이미 있음, 재사용)
    [ ] Coinglass API 연동 (청산 히트맵, OI)
    [ ] 웹 검색 기반 뉴스/분석 수집
[ ] 텔레그램 출력 포맷터
    [ ] 토론 요약 → 읽기 쉬운 형태로 변환
```

### Phase 2: 에이전트 구현 (2주)

```
[ ] Agent 1: BTC 구조 분석가
    [ ] 매물대 분석 로직 (Volume Profile 근사)
    [ ] Coinglass 청산맵 파싱
    [ ] 돌파/반동 패턴 인식
[ ] Agent 2: 매크로 분석가
    [ ] 경제 캘린더 파싱
    [ ] DXY/VIX 데이터 수집
    [ ] Risk-on/off 판단 로직
[ ] Agent 3: 상관관계 분석가
    [ ] Rolling 상관계수 계산
    [ ] Regime 변화 감지
[ ] Agent 4: 알트 생태계 분석가
    [ ] Dispersion Index 계산
    [ ] 섹터 분류 및 모멘텀
    [ ] 포지션별 BTC 베타 계산
[ ] Agent 5: 포지션 심판
    [ ] 종합 판결 로직
    [ ] 홀딩 시간 추천 알고리즘
```

### Phase 3: Shadow Mode 운영 (2-4주)

```
[ ] v9에 훅 추가 (포지션 오픈 시 에이전트 트리거)
[ ] 텔레그램으로 분석 결과만 전송 (개입 없음)
[ ] 결과 로깅 및 정확도 측정
    [ ] 에이전트가 "DANGER" 판정 → 실제로 손실 발생했는가?
    [ ] 에이전트가 "SAFE" 판정 → 실제로 TP 도달했는가?
[ ] False positive/negative 비율 분석
```

### Phase 4: Soft Integration (선택적)

```
[ ] 검증 결과 기반 개입 규칙 설정
    [ ] DANGER 시 포지션 50% 축소
    [ ] CAUTION 시 TP 보수적 조정
[ ] 점진적 자동화
```

---

## 10. MVP 범위

리소스 한정 시 가장 임팩트가 큰 부분:

```
MVP 범위:
1. Agent 1 (BTC 구조) - Coinglass 청산맵 + 주요 레벨
2. Agent 5 (포지션 심판) - 현재 포지션 리스크 평가
3. 간단한 토론 (1+5만 대화)
4. 텔레그램 알림

이유:
- 20배 레버리지에서 가장 중요한 건 "청산 안 당하기"
- BTC 구조 + 포지션 리스크만 봐도 80%의 가치
- 매크로/상관관계는 나중에 추가해도 됨
```

---

## 11. 추천 개발 우선순위

**1순위 (즉시 차용)**:
- TradingAgents의 구조화된 출력 스키마
- Coinglass API 청산 히트맵 연동
- Agent 간 토론 프로토콜 (JSON + 선택적 자연어)

**2순위 (핵심 차별화)**:
- 20배 레버리지 기준 리스크 분류 체계 (SAFE/CAUTION/DANGER)
- FS-ReasoningAgent의 Factual/Subjective 분리 개념
- 포지션별 홀딩 시간 추정 로직

**3순위 (점진적 추가)**:
- FinMem의 계층적 메모리 시스템
- LLM_trader의 RAG 뉴스 통합
- Glassnode/CryptoQuant 온체인 데이터 (결제 후)

---

## 12. 참고 레포지토리

| 프로젝트 | URL | 핵심 참고 내용 |
|----------|-----|----------------|
| TradingAgents | https://github.com/TauricResearch/TradingAgents | 전체 아키텍처, LangGraph |
| AI Hedge Fund | https://github.com/virattt/ai-hedge-fund | 에이전트 페르소나, 토론 |
| CryptoTrade | https://github.com/Xtra-Computing/CryptoTrade | 온체인 데이터, Reflection |
| FinMem | https://github.com/pipiku915/FinMem-LLM-StockTrading | 메모리 시스템 |
| LLM_trader | https://github.com/qrak/LLM_trader | RAG, Vision AI |
| AutoHedge | https://github.com/The-Swarm-Corporation/AutoHedge | 실행 파이프라인 |
| awesome-ai-in-finance | https://github.com/georgezouq/awesome-ai-in-finance | 종합 리소스 |

---

## 13. 다음 단계 옵션

1. **TradingAgents 코드 분석** — GitHub 레포 클론해서 구조 파악
2. **Coinglass API 연동 테스트** — 청산 히트맵 데이터 실제 수집
3. **에이전트 프롬프트 상세 설계** — 각 Agent의 시스템 프롬프트 구체화
4. **MVP 프로토타입 구현** — Agent 1 + 5 + 텔레그램 알림 바로 시작

---

*문서 생성일: 2026-04-01*  
*프로젝트: Multi-Agent Market Analyzer for Crypto Futures Trading*
