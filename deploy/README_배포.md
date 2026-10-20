# fdw-router 배포·실행 안내

AI 라우터(`nuc-01`)를 **어느 PC 한 대에서든** 돌려 보는 절차다. Isaac Sim 은 필요 없다 — 가상 작업장(`fdw-ops`)의 discrete 모드가 셀 역할을 한다.

## 준비물

| 항목 | 위치 / 비고 |
|---|---|
| **Python 3.10 이상** 또는 **`uv`** | 둘 중 하나면 된다. 스크립트가 `uv` → `py -3.11` → `python` 순으로 찾는다. 없으면 설치 안내를 띄우고 멈춘다 |
| **규격 패키지** (필수) | `FDW_Y1_DataDefinition_v2.2` (`fdw.v1.1`). 저장소 한 단계 위 또는 두 단계 위에 두면 자동으로 찾고, 다른 곳이면 환경변수 `FDW_SPEC_ROOT` 로 지정한다. **저장소·zip 에 들어 있지 않다** |
| fdw-ops 소스 | 기본 위치 `..\external\fdw-ops`. **없으면 스크립트가 GitHub 공개 저장소에서 받는다** (`opt1064/fdw-ops`, 브랜치 `fix/fdw-quality-disposition-base-pose`). 환경변수 `FDW_OPS_ROOT` 로 바꿀 수 있다 |
| git (선택) | fdw-ops 를 받을 때 쓴다. 없으면 GitHub 의 브랜치 zip 을 받아 푼다 |

## 명령

```powershell
# 1) 전부 한 번에 — venv 생성(다른 PC 에서 만든 깨진 venv 는 자동 재생성) · 의존성 · fdw-ops 자동 수령 · pytest 전건 · fdw-ops 위에서 라우터 배정
powershell -ExecutionPolicy Bypass -File deploy\run_local.ps1
#    옵션: -SkipTests  -RouterOnly(원본 비교 생략)  -NoFetch(GitHub 접근 안 함)  -Recreate(.venv 새로)

# 2) 외부 소스만 받기 / 갱신
powershell -ExecutionPolicy Bypass -File deploy\fetch_external.ps1            # fdw-ops
powershell -ExecutionPolicy Bypass -File deploy\fetch_external.ps1 -Update    # 같은 브랜치 최신으로

# 3) 시험만
$env:FDW_SPEC_ROOT = "<규격 v2.2 경로>"
.venv\Scripts\python.exe -X utf8 -m pytest -q

# 4) fdw-ops 통합만 (원본 FDWOrchestrator 와 RouterOrchestrator 를 같은 설정·시드로 돌려 KPI 를 나란히 출력)
.venv\Scripts\python.exe -X utf8 -m fdw_router.integrations.fdwops --mode discrete        # --router-only 로 비교 생략
```

```bash
# Linux / macOS / 원격 GPU 호스트 — 같은 일을 하는 셸 스크립트
export FDW_SPEC_ROOT=/path/to/FDW_Y1_DataDefinition_v2.2
bash deploy/run_local.sh            # --skip-tests --router-only --no-fetch --recreate
```

`--mode isaac` 은 Isaac Sim 환경에서 같은 모듈을 `ISAACSIM_PYTHON_EXE` 로 실행한다.

## 처음 돌릴 때 (체크리스트)

1. 저장소를 clone 하거나 zip 을 아무 폴더에 푼다.
2. 규격 폴더 `FDW_Y1_DataDefinition_v2.2` 를 받아 저장소 옆에 두거나, `$env:FDW_SPEC_ROOT` 에 경로를 넣는다.
3. `deploy\run_local.ps1` 실행. 인터넷이 되면 fdw-ops 를 알아서 받는다. 안 되면 fdw-ops 폴더를 복사해 오고 `-NoFetch` 를 붙인다.
4. 끝에 `KPI side by side` 표가 나오고 원본과 라우터 열의 숫자가 같으면 성공이다 (poc1.yaml, seed 0: 완료 1 · 격리 2 · makespan 215.4 s · 결정 6건).

## 결과물이 떨어지는 곳

| 결과물 | 경로 |
|---|---|
| 라우터 결정 로그 (JSON Lines, 결정 1건 = 1행) | `fdw_router\integrations\fdwops\logs\decisions_<run>.jsonl` |
| fdw-ops KPI 로그·요약 | `fdw_router\integrations\fdwops\logs\fdw_sim\original_kpi.jsonl`, `router_kpi.jsonl`, `*_summary.json` |
| 2014 통지 | fdw-ops 버스 `/fdw/fault` 로 발행 + 결정 로그 `reason` 열 + KPI `router_replan_2014_count` |

결정 로그 한 행의 열: `job_id · task_id · route_index · process · sim_time · candidates · excluded(셀별 탈락 사유) · chosen · chosen_by(selector | min_load) · cost_components · atcs · reason · decision_ms`.

## 어떻게 끼워 넣었나

fdw-ops 의 `SimulationManager` 는 `FDWOrchestrator` 를 내부에서 직접 생성한다. `fdw_router.integrations.fdwops` 는 실행 직전에 `fdw_sim.simulation.manager.FDWOrchestrator` 를 `RouterOrchestrator`(같은 표면의 하위 클래스)로 바꿔 끼운다. fdw-ops 소스는 한 줄도 고치지 않는다. 바뀌는 것은 "다음 공정을 어느 셀에 보낼지"뿐이고 격리·출하·KPI 의미는 그대로라 원본과 수치를 나란히 비교할 수 있다. 이것은 라우터를 시뮬레이터에 연결하는 방법이며, 현장 구조에서는 오케스트레이션(`ops-01`)과 라우팅(`nuc-01`)이 별도 노드로 MQTT 로 대화한다.
