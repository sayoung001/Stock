"""스코어링 모델 재현 — 과거 전 구간에서 시그널을 **생성**한다.

## 왜 이게 필요한가

기존 백테스트는 `data/tracking/signals.csv`를 **읽는다.** 그 파일에는
운영을 시작한 뒤 쌓인 시그널만 있어서, 2주치(24건)뿐이면 `--window 1000`을
줘도 볼 수 있는 시그널은 여전히 24건이다. **표본의 천장이 가격 데이터
길이가 아니라 기록된 시그널 이력이었다.**

이 패키지는 그 천장을 없앤다. 일봉 1000일을 넣고 스코어링 모델을 매 봉에
돌려 **그때 시그널이 났을 것인가**를 재구성한다. 그러면 표본이 가격 데이터가
있는 만큼 늘어난다.

`US_KR_CLAUD_분석_및_발전방향.md` §3.5가 지적한 "백테스트 엔진 부재 →
모든 가중치·임계값이 미검증 가정치" 문제의 그 엔진이다.

## 중요 — 이것은 원본이 아니라 **재현**이다

이 저장소에 `stock_auto/` 스코어링 원본이 없다(`docs/codemap_exit.md` §0).
그래서 `현재_적용_로직_전수.md` §2.3~2.8에 적힌 명세를 읽고 다시 구현했다.
문서에 수치가 없는 부분은 가정을 세웠고, 그 가정은 전부
`docs/MODEL_REPRODUCTION.md`에 모아 두었다.

**따라서 이 모델이 뱉는 시그널은 프로덕션 시그널과 완전히 같지 않다.**
같은지 확인하는 방법은 §"검증" 참고 — 최근 2주 실제 시그널과 대조하면
재현도를 숫자로 볼 수 있다.

## 누출 차단

모든 지표는 **i번째 봉까지만** 써서 i번째 봉의 시그널을 만들고, 진입은
i+1 봉 시가다. `tests/test_model_leakage.py`가 이를 고정한다 — 과거
candle_idx 오프바이원 사고(CLAUDE.md 교훈 2)와 같은 종류의 실수를 막는다.
"""

from stock_auto.model.generator import (
    ModelConfig,
    generate_signals,
    generate_signals_for_symbol,
)
from stock_auto.model.strategies import (
    MONEY_STRATEGIES,
    PRICE_STRATEGIES,
    STRATEGY_STYLE,
    apply_strategies,
)

__all__ = [
    "ModelConfig",
    "generate_signals",
    "generate_signals_for_symbol",
    "apply_strategies",
    "MONEY_STRATEGIES",
    "PRICE_STRATEGIES",
    "STRATEGY_STYLE",
]
