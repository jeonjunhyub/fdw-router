# -*- coding: utf-8 -*-
"""Isaac Sim 셀 런타임 어댑터 — 2차년도 자리.

**현재 상태: 계약만 있고 구현이 없다. 의도된 것이다.**

Isaac Sim 은 아직 도입되지 않았다. 그러나 이 파일을 지금 두는 이유는 둘이다.

1. **경계를 지금 못박아야** 1차년도 J축이 DES 에 종속되지 않는다.
   DES 만 보고 짜면 "셀 내부를 다 안다"는 가정이 코드에 스며든다.
2. 2차년도 인터페이스 규격(산출물 D8)은 결국 이 클래스가 무엇을 필요로 하는지의
   목록이다. 여기 `TODO(2차년도)` 로 적힌 것이 곧 그 규격의 목차다.

────────────────────────────────────────────────────────────────────
규격상 확인된 사실 (협의 문서 확인 3)
────────────────────────────────────────────────────────────────────
`T3_isaac_ingest_ready_spec.md` §1 은 범위를 이렇게 한정했다 —
*"IsaacSim 런타임 실행기 구현이나 실환경 구동 결과가 아니다."*
`T4_e2e_pipeline_mermaid.md` 에서 Isaac 에서 오케스트레이션으로 가는 선은
점선 "DT 실측 비교" 하나뿐이다.

즉 **현행 규격에서 T3 는 합성 데이터를 Isaac 에 한 방향으로 넣는 재생(replay)
명세**이고, "Isaac 안의 가상 셀이 `task_request` 를 받고 `cell_status` 를 올린다"는
정의는 아직 없다. 그 양방향 확장이 D8 의 내용이며, 확인 3 으로 질의해 두었다.

따라서 이 어댑터가 메울 간극은 다음 한 줄로 요약된다.

    T3 (한 방향 재생)  →  D8 (양방향 셀 대리)

────────────────────────────────────────────────────────────────────
어댑터 경계 — 무엇을 지금 정할 수 있는가
────────────────────────────────────────────────────────────────────
2차년도 담당 분담은 2차년도 계획 수립 때 정해진다. **그와 무관하게** 이 어댑터에
들어갈 내용은 정보 의존성으로 갈린다 — 이 구분은 누가 맡든 바뀌지 않는다.

  ① `CONTRACT_DEFINABLE_NOW`  — Isaac 씬을 몰라도 정할 수 있다
                                 1차년도에 정해 두면 2차년도가 그대로 따라온다
  ② `CONTRACT_NEEDS_SCENE`    — 씬·로봇·제어기가 있어야 정해진다
                                 먼저 쓰면 실제 씬과 어긋나 버려진다

①을 미루면 2차년도 가상 셀이 T2 규격과 무관하게 만들어지고, 다 만든 뒤 봉투를
덧씌우는 재작업이 된다. 그래서 ①은 1차년도 산출물(D8)에 넣는다.
"""
from __future__ import annotations

from typing import Iterable

from ..ports.cell_runtime import CellRuntime


class IsaacNotAvailable(RuntimeError):
    """Isaac Sim 미도입 상태에서 호출되었다."""


class IsaacCellRuntime(CellRuntime):
    """Isaac Sim 안의 가상 셀들을 대리하는 런타임 (2차년도).

    1차년도에는 인스턴스를 만들 수는 있으나 동작하지 않는다.
    `DesCellRuntime` 과 **같은 포트**를 구현하므로, 도입 시점에
    `RuntimeProfile(name="isaac")` 로 바꾸는 것 외에 라우터 수정은 없다.
    """

    name = "isaac"

    def __init__(self, *, bridge_url: str | None = None, usd_stage: str | None = None):
        self.bridge_url = bridge_url
        self.usd_stage = usd_stage
        self._available = False   # 도입 전까지 항상 False

    # ── 도입 전 방어 ───────────────────────────────────────────────
    def _require(self) -> None:
        raise IsaacNotAvailable(
            "Isaac Sim 이 아직 도입되지 않았습니다. 1차년도에는 "
            "RuntimeProfile(name='des') 를 사용하십시오. "
            "이 어댑터의 구현 범위는 산출물 D8(2차년도 인터페이스 규격)입니다."
        )

    # ── 포트 구현 ──────────────────────────────────────────────────
    def send(self, envelope: dict) -> None:
        # TODO(2차년도): T2 봉투 → ROS 2 토픽/액션 변환
        #   - task_request.params 의 param_id 를 각 셀 제어기 인터페이스로 사상
        #     (정본: T1_specs_consolidated.xlsx `04_파라미터통합`)
        #   - task_request.step_id 를 USD 애니메이션/동작 시퀀스로 사상
        #     (정본: `03a_실행단위_step_id`)
        #   - 이송 작업(to.system == fleet)은 AMR 경로계획으로 사상
        self._require()

    def poll(self) -> Iterable[dict]:
        # TODO(2차년도): ROS 2 구독 → T2 봉투 생성
        #   - 관절/TCP 상태 → cell_status (cell_state.schema.json 준수)
        #   - 시뮬레이션 사건 → event (11_이벤트코드 카탈로그 내 코드만)
        #   - 작업 종료 → task_complete (kpi_summary 키 체계는 확인 1 회신 대기)
        #   - 모든 봉투에 source="simulated" 를 싣는다 (§2.5)
        self._require()

    def step(self, until: float | None = None) -> None:
        # TODO(2차년도): Isaac 물리 스텝 진행 또는 실시간 대기
        self._require()

    def now(self) -> float:
        # TODO(2차년도): Isaac 시뮬레이션 시각. wall clock 이 아니다.
        self._require()

    @property
    def finished(self) -> bool:
        return False

    # ── 2차년도 항목 — 정보 의존성으로 두 갈래 ─────────────────────
    #
    # 담당 분담이 아니라 "씬을 알아야 정해지는가"로 가른다.
    # 분담은 2차년도에 바뀔 수 있지만 이 구분은 바뀌지 않는다.
    #
    # 이 구분이 산출물 D8 의 성격을 정한다 —
    #   D8 = "우리가 Isaac 사상을 정의하는 규격"  (✗)
    #   D8 = "가상 셀이 만족해야 할 봉투 계약"    (○)

    #: ① Isaac 씬을 몰라도 정할 수 있다. 1차년도에 정해 둔다.
    #:    미루면 2차년도 셀을 다 만든 뒤 봉투를 덧씌워야 하고, 그게 재작업이다.
    CONTRACT_DEFINABLE_NOW = (
        "가상 셀의 node_id 배정 형식 (§3.2b 갱신 주체 — 규격 협의)",
        "가상 셀이 발행해야 할 봉투 종류와 시점 "
        "(cell_capability(join) → cell_status → task_accept → task_complete/task_failed)",
        "모든 봉투에 source='simulated' 표기 (§2.5)",
        "Isaac 시뮬레이션 시각 ↔ timestamp_iso 변환 규약 (부록 B.3: ms 3자리 + 오프셋)",
        "task_complete.kpi_summary 키 체계 (규격 확인 회신 후)",
        "응답 타임아웃·correlation_id 규약 (항목 108·13 제안값 그대로)",
    )

    #: ② 씬·로봇·제어기가 있어야 정해진다. 2차년도 사안이다.
    #:    먼저 쓰면 실제 씬과 어긋나 버려진다.
    CONTRACT_NEEDS_SCENE = (
        "step_id 어휘 ↔ USD 동작 시퀀스 사상 ",
        "param_id ↔ 제어기 인터페이스 사상 (04_파라미터통합 기준)",
        "가상 셀 구성 — 셀 수, 장비, 물리·센서 모델",
        "ROS 2 브리지 토픽 구성 (T3 ros2_topic_binding 은 publish 전용이므로 역방향 필요)",
        "Sim/Real Gap 정량화 지표 (공동연구 항목)",
    )

    #: 전체 목록
    OPEN_ITEMS = CONTRACT_DEFINABLE_NOW + CONTRACT_NEEDS_SCENE
