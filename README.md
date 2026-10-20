# fdw-router

FDW 메시지 규격 v2.2(`fdw.v1.1`) 기준 **AI 라우터**(`router` / `nuc-01`). 오케스트레이션 계층과 셀 계층 사이에서 두 가지 일을 한다.

| 일 | 내용 | 기준 |
|---|---|---|
| **메시지 라우팅** | 규격 메시지(봉투)를 받아 검증하고, 규칙대로 중계·보류·폐기하며 이벤트(2011~2015)를 발행한다 | 규격 `routing_rules.json` 을 그대로 읽는다 (조건을 코드에 적지 않는다) |
| **작업 배정** | 생산 오더를 작업으로 전개하고 각 작업을 어느 셀에 맡길지 정해 `task_request` 를 보낸다. 못 정하면 2014 이벤트로 오케스트레이터에 넘긴다 | 자체 설계: 실행가능성 필터 → ATCS 우선순위 → 비용 최소 셀. 난수 없음, 결정은 전부 JSON Lines 로 기록 |

규격 패키지(`FDW_Y1_DataDefinition_v2.2`)는 **저장소에 넣지 않고 읽는다.** 별도로 받아 환경변수 `FDW_SPEC_ROOT` 로 지정한다.

## 설치·실행

Python 3.10 이상(또는 `uv`)과 규격 폴더, 인터넷(fdw-ops 수령)만 있으면 한 줄이다. 가상환경 생성 → fdw-ops 수령 → 시험 1,721건 → fdw-ops 위에서 원본 오케스트레이터와 라우터를 나란히 실행까지 약 6분.

```powershell
# Windows
$env:FDW_SPEC_ROOT = "<경로>\FDW_Y1_DataDefinition_v2.2"     # 저장소 옆에 두면 생략 가능
powershell -ExecutionPolicy Bypass -File deploy\run_local.ps1
```

```bash
# Linux / macOS
export FDW_SPEC_ROOT=/path/to/FDW_Y1_DataDefinition_v2.2
bash deploy/run_local.sh
```

끝에 `KPI side by side` 표가 나오고 원본과 라우터 열이 같으면(완료 1 · 격리 2 · makespan 215.4 s) 정상이다. 공정당 셀이 하나인 기본 설정이라 수치가 같다. 상세: `deploy/README_배포.md`.

수동 실행:

```bash
pip install -r requirements.txt PyYAML numpy
pytest -q                                                    # 1,721 passed
python -m fdw_router.integrations.fdwops --mode discrete     # fdw-ops(Isaac Sim 가상 작업장) 위에서 라우터가 배정
python tools/mqtt_smoke.py --embedded                        # MQTT 브로커 왕복 (amqtt 내장)
```

## 시연 (demo/)

공정당 셀이 둘일 때 원본 `FDWOrchestrator`(고정 표 `process_to_cell`)와 `RouterOrchestrator`(작업 배정)를 같은 조건으로 나란히 돌린다. 주문 8건 · 용접 셀 2 (40 s) · 검사 셀 2 (15 / 20 s) · AMR 3대 · 같은 seed.

| 판 | 명령 | 결과 (마지막 주문 완료) |
|---|---|---|
| 2D (discrete, 아무 PC) | `.venv\Scripts\python.exe -X utf8 demo\run_demo.py` (`--no-video` 면 수 초) | 원본 621 s → 라우터 434 s (30 % 단축) |
| 3D (Isaac Sim 6.1 필요) | `demo\run_demo_3d.py` 뒤 `demo\compose_3d.py` | 원본 777 s → 라우터 695 s (11 %, AMR 이송이 병목) |
| Isaac Sim 창으로 보기 | `demo\3D로_보기_1_원본공장장.bat` · `3D로_보기_2_라우터.bat` | — |

원본은 고정 표 때문에 두 번째 셀을 전혀 쓰지 않는다. 라우터는 후보마다 비용(초)을 계산해 번갈아 배정한다. 영상과 해석: [Releases](../../releases), `demo/README_시연.md`.

## 구조

```
fdw_router/
├─ spec.py              규격 패키지 로더 (복사하지 않고 읽는다)
├─ config.py            확정값·회신 대기 항목 한곳 (타임아웃, 이벤트 코드, process_type 대응표)
├─ msg/                 ── 메시지 라우팅 (규격 준수)
│  ├─ envelope.py       스키마 검증 (Registry + FormatChecker)
│  ├─ node_registry.py  §3.2b 노드 사전 파싱 + 미등재 보류
│  ├─ rules.py          4규칙 판정기 — routing_rules.json 을 읽어 계층별 평가
│  └─ build.py          봉투 생성기 (결정론적 ID·시각, 라우터 발행분 correlation_id)
├─ core/                ── 작업 배정 (자체 설계, msg 를 import 하지 않음)
│  ├─ model.py · feasibility.py · priority.py · selector.py
│  ├─ dispatcher.py     이벤트 트리거 재평가 + 타임아웃 2종 + 재배정 한도
│  ├─ delegation.py     2014 조건 3종   decision_log.py  JSON Lines
│  └─ adapt.py          봉투 ↔ 도메인 (두 축의 유일한 접점)
├─ transport/           mqtt.py (fdw/v1/<system>/<node>/<type>, paho 2.x) · router_node.py (nuc-01)
├─ ports/cell_runtime.py        셀 런타임 포트
├─ adapters/            des_cell.py (SimPy) · fdwops_cell.py (fdw-ops 브리지) · isaac_cell.py (Isaac 연동 계약)
├─ integrations/        fdwops/RouterOrchestrator · orchestrator_stub.py (ops-01 상대역)
└─ sim/s1_smoke.py      S1 시나리오 결정론 확인
demo/                   2D · 3D 시연 스크립트
deploy/                 run_local.ps1 / run_local.sh / fetch_external.ps1
```

## 작업 배정 — 어떻게 고르나

| 단계 | 내용 | 모듈 |
|---|---|---|
| 재평가 시점 | `production_order` 수신, 작업 완료, 셀 idle 전이, 셀 fault, 긴급 오더 삽입 | `core/dispatcher.py` |
| 실행가능성 | 하드 제약 8종(process_type, task_type, 파라미터 정격, 셀 상태, 동시 처리 한도, 선행 op, 노드 등재 …). 위배 셀은 제외하고 사유 기록 | `core/feasibility.py` |
| 우선순위 | ATCS `I = (w/p) · exp(−slack/(k1·p_avg)) · exp(−setup/(k2·s_avg))`, `w` = `order_priority` 3등급 | `core/priority.py` |
| 셀 선택 | `Cost = α·Wait + β·p + γ·Transport + δ·Setup + ε·Imbalance` 최소 (단위 초). 가중치 묶음 leadtime / balanced / urgent. 동점은 `cell_id` 순 | `core/selector.py` |
| 디스패치 | `task_request` 발행, 수락·완료 타임아웃, 초과 시 재배정, 한도 초과 시 위임 | `core/dispatcher.py` |
| 위임 | 2014 발행 조건은 규격 §3.2c 의 셋뿐: 가용 셀 없음 · 후보 전부 거절 · 재배정 한도 초과 (busy 는 사유가 아니다) | `core/delegation.py` |
| 결정 로그 | 결정 1건 = 1행: 후보, 셀별 탈락 사유, 비용 성분, ATCS, 소요 ms | `core/decision_log.py` |

## 오케스트레이터와의 관계

fdw-ops 의 `SimulationManager` 는 `FDWOrchestrator` 를 내부에서 생성한다. `fdw_router.integrations.fdwops` 는 실행 직전에 그 자리에 하위 클래스 `RouterOrchestrator` 를 넣어 "다음 공정을 어느 셀에" 고르는 부분만 바꾼다. fdw-ops 소스는 고치지 않고, 이송·격리·출하·KPI 는 원본 코드가 그대로 돈다. 이것은 라우터를 시뮬레이터에 연결하는 방법이다. 현장 구조(규격 v2.2)에서는 오케스트레이션(`ops-01`)과 라우팅(`nuc-01`)이 별도 노드로 MQTT 로 대화하며, 어느 쪽이든 셀 선택은 라우터가 한다.

## 시험 (1,721건)

| 시험 | 건수 | 근거 |
|---|---|---|
| 규격 기준선 (v2.2 · fdw.v1.1 · NEG-44) | 5 | `00_INDEX.json`, `envelope.schema.json` |
| 진리표 전건 TT-01~64 | 65 | `routing_rules.json` |
| 부정 표본 44건 거절 + 정상 예시 24종 | 91 | `examples/negative_fixtures.json` |
| 판정 입력 불변성 전수 | 1,453 | `decision_inputs_note` |
| DES 런타임 규격 준수 | 9 | 자체 |
| 작업 배정 (DAG · 제약 · ATCS · Cost · 디스패처 · 위임 · 대응표) | 69 | 자체 |
| 전송·라우터 노드·봉투↔도메인 결합 | 19 | 자체 |
| fdw-ops 통합 (상태 7종 변환, 3 job 완주, 2014 3건, 다중 셀 2:2 배정) | 15 | `external/fdw-ops` |

규격의 `decision_inputs_note` 는 "진리표만으로는 부족하다"고 적는다. 그래서 각 규칙이 선언한 입력을 전수 조합하고, 선언되지 않은 축(priority·ttl_ms·payload·session_id 등)을 흔들어도 판정이 불변인지 함께 시험한다.

## 미확정 사항

규격 측 확정 대기 항목은 전부 `config.py` 한 곳에 있다. 확정되면 값만 바꾼다 (응답 경로 direct/relay, 수락·완료 타임아웃, 미등재 억제 간격, process_type 대응표의 잠정 항목).

## 배포

- 태그 = 버전 (`VERSION`, `CHANGELOG.md`). 로컬 zip: `python tools/make_release.py`.
- 외부 소스는 저장소에 넣지 않는다: 규격 패키지는 `FDW_SPEC_ROOT`, fdw-ops 는 `deploy/fetch_external.ps1` 이 공개 저장소에서 받는다 (`FDW_OPS_ROOT`).
