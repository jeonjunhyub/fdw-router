# CHANGELOG — fdw-router

규격 대응 버전을 항상 병기한다. 형식: SemVer. 태그 = `vX.Y.Z`.

## v0.3.4 — 2026-10-20 · 공개 저장소 첫 판 · 규격 v2.2 (`fdw.v1.1`)

- 시연(2D · 3D · 합성 · Isaac Sim 창 실행 bat)을 `demo/` 로 편입. 결과물은 `demo/output/` (git 제외, `FDW_DEMO_OUT`).
- 규격 폴더 기본 탐색을 저장소 옆(또는 두 단계 위)의 `FDW_Y1_DataDefinition_v2.2` 로 단순화. 외부 패키지 수령 스크립트 제거.
- 라우터 동작은 v0.3.3 과 같다.

## v0.3.3 — 2026-10-19 · 작업 배정 대기시간 항 수정 · 다중 셀 시연

- `core/selector.py`: `Wait(c)` 가 `remaining_s` 만 보고 `queue_depth` 를 더하지 않던 것을 docstring 대로 고침 — `load = remaining_s + queue_depth × nominal_cycle_time_s`. `Imbalance` 도 같은 load 로 계산.
- `integrations/fdwops/router_orchestrator.py`: 같은 틱에 배정했지만 아직 도착·착수하지 않은 작업(`_pending_assigned`)을 그 셀의 큐로 센다. 이전에는 오더 8건을 동시에 받으면 전부 첫 셀로 보냈다. 결정 로그 `cost_components` 에 후보 전원의 비용을 남긴다.
- 시험 추가: `test_router_spreads_same_tick_orders_over_two_welders`. 1,721건 통과.
- 시연: 주문 8 · 용접 2 · 검사 2 · AMR 3, 원본 621 s vs 라우터 434 s (30 % 단축).

## v0.3.2 — 2026-10-19 · 어느 PC 에서나 실행

- `deploy/run_local.ps1` · `run_local.sh`: uv 또는 설치된 Python 으로 가상환경 생성, 다른 PC 에서 만든 venv 감지·재생성, fdw-ops 자동 수령, 시험 전건, 원본/라우터 나란히 실행.
- `deploy/fetch_external.ps1`: fdw-ops 공개 저장소 브랜치 수령 (git 없으면 zip).

## v0.3.1 — 2026-10-16 · 폴더 재배치 대응

- 규격 폴더·fdw-ops 경로를 환경변수(`FDW_SPEC_ROOT`, `FDW_OPS_ROOT`)와 기본 탐색으로 처리.

## v0.3.0 — 2026-10-16 · 규격 v2.2 (`fdw.v1.1`) 기준선

- 규격 v2.2 반영: `task_request` 는 라우터가 발행(`from.system = router`), `order_priority` 3등급, 2014 위임 조건 3종, 2015 규격 위반 이벤트(`violation_path`), process_type 대응표 14종.
- fdw-ops 통합: `RouterOrchestrator` 가 `FDWOrchestrator` 자리에서 배정. 같은 seed 로 KPI 동일 확인.
- MQTT 전송(`fdw/v1/<system>/<node>/<type>`, paho 2.x)과 오케스트레이터 스텁.

## v0.2.0 — 규격 v2.1 (`fdw.v1.0`) 기준선

- 메시지 라우팅(봉투 검증 · 노드 사전 · 4규칙 판정 · 중계) 과 작업 배정(실행가능성 · ATCS · 비용 · 디스패처 · 위임 · 결정 로그) 첫 구현. SimPy 셀 런타임으로 검증.
