# -*- coding: utf-8 -*-
"""미확정 사항과 확정값을 한곳에 모은 설정.

여기 있는 값은 원래 전부 **규격 담당 확정 대기 중인 항목**이었다. 2026-09-20 배포된
규격 v2.2 가 협의 요청을 전건 반영하였으므로, 이제
대부분은 **확정값**이고, 남은 것은 규격 측 회신 대상뿐이다.

원칙은 그대로다 — 값이 바뀌면 코드를 고치는 것이 아니라 이 파일의 값만 바꾼다.
그것이 가능하도록 아래 값을 참조하는 쪽은 분기를 한 곳에만 둔다.

각 항목의 근거는 규격 협의 결과로 적어 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── 우리 계층의 정체 ────────────────────────────────────────────────
ROUTER_NODE_ID = "nuc-01"
ROUTER_SYSTEM = "router"          # §3.2b 확정
ORCHESTRATOR_NODE_ID = "ops-01"
ORCHESTRATOR_SYSTEM = "fdw-ops"
STATEDB_NODE_ID = "statedb-01"


@dataclass(frozen=True)
class PendingDecisions:
    """규격 확정값과 회신 대기 항목. 값이 바뀌면 여기만 교체한다."""

    # ── 요청 1 (B1): task_request 발신 계층에 router 추가 ── v2.2 확정 ──
    # 규격 v2.2: envelope allOf[20] from.system = ["fdw-ops","router"].
    # 라우터 발행 task_request 에는 correlation_id 가 스키마 필수(NEG-44).
    # 응답 4종 to.system 에 router 허용. 도달 경로는 ⓐ 직접 회신 잠정.
    task_request_from_system: str = "router"
    task_request_emulated: bool = False
    response_path: str = "direct"               # "direct"(ⓐ) | "relay"(ⓑ) 

    # ── 요청 2 (B2): production_order 의 작업 우선순위 필드 ── v2.2 확정 ──
    # `order_priority` 선택 필드, enum standard|expedited|rush, 미기재 시 standard.
    # 부여 주체는 상위 생산계획(MES), 라우터는 변환만 한다. 등급 수는 규격 측 확인 대상.
    order_priority_source: str = "payload"
    order_priority_field: str = "order_priority"
    order_priority_default: str = "standard"
    order_priority_weights: dict = field(
        default_factory=lambda: {"standard": 1.0, "expedited": 2.0, "rush": 4.0}
    )  # w_j 표 — 라우팅 계층 소관(정의서 §3.4). 값은 P5 시나리오 튜닝 대상

    # ── 요청 3 (B3): 위임 수단 ── v2.2 확정 ──
    # 이벤트 코드 2014(comm·WARN) 신설. 발행 조건은 규격 §3.2c 가 셋으로 명시 —
    # ⓐ 가용 셀 없음 ⓑ 후보 소진 ⓒ 재배정 한도 초과. 조치 요청이 아니라 사건 통지이며
    # 오케스트레이션 계층이 자기 권한으로 replanning_request 를 발행한다.
    # 위임 전용 message_type 은 **2차년도 안건**.
    delegation_mode: str = "event"               # 1차년도 고정
    delegation_event_code: int = 2014            # v2.2 확정
    delegation_message_type: str = "delegation_request"   # 2차년도 가칭
    reassign_limit: int = 3                      # 조건 ⓒ 재배정 한도 — 시나리오로 확정

    # ── 요청 5 (B5/항목 133): 규격 위반 봉투 수신 통지 ── v2.2 확정 ──
    # 2015(comm·WARN). 필수는 violation_path 하나. 발신 노드+종류 조합으로 반복 억제.
    schema_reject_event_code: int | None = 2015

    # ── 항목 123: 2012 억제 (내부 결정, v2.2 동의) ──────────────────
    unregistered_event_suppress_s: int = 300    # 최초 1회 후 5분

    # ── 항목 102: 등재 전 배정 가부 (내부 결정, v2.2 동의) ──────────
    assign_before_registration: bool = True     # 신규 배정만 허용
    migrate_running_before_registration: bool = False

    # ── 항목 108·13: 상관·타임아웃 ──────────────────────────────────
    # 108 — 동의(스키마에 반영됨). 13 — **이견**: 수락 대기와 완료 대기를 구분하라는 제안.
    # 그래서 둘로 나눈다. 값은 시나리오 검증(P5)으로 확정 후 회신.
    require_correlation_id: bool = True         # 우리가 발행하는 봉투에만
    accept_timeout_s: float = 5.0               # 수락 대기: 지시 발행 → task_accept/reject
    complete_timeout_factor: float = 1.5        # 완료 대기: expected_duration_s × 1.5

    @property
    def response_timeout_factor(self) -> float:
        """예전 이름(단일 타임아웃 배수). 완료 대기 배수로 읽는다."""
        return self.complete_timeout_factor

    # ── 확인 2: cell_status 주기 상한 (잠정값. heartbeat·응답 4종도 예산에 포함) ──
    cell_status_hz_assumed: float = 10.0

    # ── 확인 4: 전송 프로토콜 (가능. cell_capability.interface.protocol 로 공지) ──
    transport_protocol: str = "mqtt5"

    # ── 항목 150: 능력 공지 보관 (동의) ─────────────────────────────
    capability_retention: str = "until_leave"


PENDING = PendingDecisions()


# ── 요청 6 (B6): process_type 두 어휘의 대응 ── v2.2 확정 (가이드라인 표 9) ──
#
# `cell_capability.process_type`        6종  (welding, forming, …)
# `process_sequence.steps[].process_type` 14종 (welding_arc, …, control 신설)
#
# 정본은 가이드라인 v3.0 표 9 / 정의서 §2.1 대응표. 이 표는 그것을 옮겨 적은 것이며
# **규격이 개정되면 이 표만 교체한다.** 다른 코드는 손대지 않는다.
#
# 값이 `None` 인 것은 셀 배정 대상이 아닌 종류다(시퀀스상의 보조 단계. 단계 자체가
# 생략되지는 않으며 시퀀스 진행만 기록 — 잠정값).
PROCESS_TYPE_MAP: dict[str, str | None] = {
    # 동명 대응
    "forming": "forming",
    "waam": "waam",
    "machining": "machining",
    # 세분 → 통합
    "welding_arc": "welding",
    "welding_laser": "welding",
    "amr_transfer": "transport",
    "transfer": "transport",
    # 검사: 1차년도는 별도 배정하지 않고 해당 공정 셀이 수행(표 9). 대응값만 둔다.
    "inspection": "inspection",
    "qa": "inspection",
    # 소재 취급: 실행 주체 확인 전 잠정 대응 (잠정)
    "handling": "transport",
    # 셀 배정 대상 아님 (표 9 확정)
    "cooling": None,
    "fixture": None,
    "exception": None,
    "control": None,
}

#: 셀 상태별 배정 가능 여부 (§3.3a 전이표).
#: `idle` 만 새 작업을 받는다. `running` 은 동시 처리 여유가 있을 때만.
#: `error` 는 복구 후 반드시 `idle` 을 거치므로 직접 배정하지 않는다.
ASSIGNABLE_STATES = frozenset({"idle", "running"})


@dataclass(frozen=True)
class RuntimeProfile:
    """셀 런타임 선택. Isaac Sim 도입 전/후를 여기서만 가른다."""

    name: str = "des"          # "des" (1차년도) | "isaac" (2차년도)
    seed: int = 0
    realtime_factor: float = 0.0   # 0 = 최대 속도


DEFAULT_RUNTIME = RuntimeProfile()

# 보류 기본 TTL (routing_rules.unregistered_node_fallback.hold.default_ttl_ms)
DEFAULT_HOLD_TTL_MS = 600_000
