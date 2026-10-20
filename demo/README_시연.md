# 라우터 시연 — 다중 셀 시나리오 (S6-데모)

> 위치: 저장소 `demo\`. 결과물 기본 위치는 `demo\output\` 이고 환경변수 `FDW_DEMO_OUT` 으로 바꿀 수 있다.

- 작성: 2026-10-19 · 라우터 v0.3.3 기준
- 목적: **영상과 이미지**로 공정당 작업대가 둘일 때 원본 오케스트레이터(고정 표)와 우리 라우터(작업대 고르기)가 같은 조건에서 어떻게 다른지 보인다.
- 시나리오는 이 저장소에서 정한 것이다 (규격 시나리오가 확정되면 그 조건으로 다시 돌리면 된다).

## 시나리오

| 항목 | 값 |
|---|---|
| 작업대 | MAT_01 (소재 창고, AMR 3대) · WELD_01, WELD_02 (용접, 40 s/개, 같은 성능) · INSP_01 (검사 15 s) · INSP_02 (검사 20 s) |
| 배치 | MAT (0, 0) · WELD (5, ±2.5) · INSP (10, ±2.5) m. AMR 1 m/s, 통로 1개 공유 |
| 주문 | 8건, 공정 순서 용접 → 검사, 전부 시각 0 에 투입. JOB_5 는 긴급(high) |
| 원본 | fdw-ops `FDWOrchestrator`. `process_to_cell` 고정 표 → 용접은 전부 WELD_01, 검사는 전부 INSP_01 |
| 라우터 | `RouterOrchestrator` (fdw-ops 소스 무수정, 셀 선택만 라우터). 할 수 있는 작업대 거르기 → 급한 순서 점수 → 작업대 비용 |
| 공통 | discrete 모드, physics 60 Hz, seed 0, 검사 결정론적 합격 (배정 효과에 집중) |

## 결과 (2026-10-19)

| 지표 | 원본 | 라우터 |
|---|---|---|
| 마지막 주문 완료 | 621 s | 434 s |
| 평균 makespan (주문당) | 373 s | 295 s |
| WELD_01 / WELD_02 가동 | 320 / 0 s | 160 / 160 s |
| INSP_01 / INSP_02 가동 | 120 / 0 s | 60 / 80 s |
| 라우터 결정 | — | 16건 (전부 `decisions_*.jsonl`) |

같은 주문을 **30% 빨리** 끝냈다. 원본은 고정 표 때문에 두 번째 작업대를 전혀 쓰지 않는다. 라우터는 후보마다 비용(초)을 계산해 번갈아 배정한다 (결정 로그 표 참고).

## 산출물 (`output/`)

| 파일 | 내용 | 보고서 용도 |
|---|---|---|
| `demo.mp4` | 나란히 재생 영상, 1920×1080, 10 fps, 1 프레임 = 시뮬레이션 1 초. 위: 원본, 아래: 라우터. 왼쪽 공장 지도(작업대 상태·부품 이동), 오른쪽 간트 | 발표·동영상 첨부 |
| `demo_gantt.png` | 최종 작업대 점유 비교 | 본문 그림 |
| `demo_kpi.png` | 완료 시각 · makespan · 2번 작업대 가동 막대 | 본문 그림 |
| `demo_decisions.png` | 라우터 결정 로그 첫 10건 (후보별 비용, 선택, 판단 ms) | 본문 표 |
| `demo_frame_start/mid/end.png` | 영상 대표 프레임 3장 | 캡처 |
| `demo_summary.json` | 시나리오와 두 결과 수치 | 수치 근거 |
| `demo_timeline_*.json` | 셀별 상태 구간 · 이송 · 완료 사건 (시뮬레이션 초) | 재분석 |
| `kpi_*/` | fdw-ops KPI JSONL · summary | 원본 로그 |

## 다시 만들기

```powershell
# 저장소의 .venv 를 쓴다 (deploy\run_local.ps1 로 만든 것). matplotlib · Pillow · ffmpeg 필요
..\.venv\Scripts\python.exe -X utf8 run_demo.py            # 전부 (영상 포함, 수 분)
..\.venv\Scripts\python.exe -X utf8 run_demo.py --no-video # 이미지·JSON 만 (수 초)
```

시나리오를 바꾸려면 `run_demo.py` 상단의 `CELLS`, `N_JOBS`, `RUSH`, 주문의 `recipe_overrides` 를 고친다.


## Isaac Sim 3D 판 (2026-10-20)

같은 시나리오를 fdw-ops 의 **Isaac 모드(Level 2 USD 시각화)** 로 돌려 3D 영상을 만들었다. 이 PC 에 Isaac Sim 6.1.0 을 설치했다 (`C:\isaacsim\env`, Python 3.12, RTX 5080). 화면은 fdw-ops 의 배치도 1안 장면(소재 랙 3줄 · 캔틸레버 랙 · 천장 크레인 · 성형기 · 벽과 기둥 · 작업대 · FANUC CRX-10iA 로봇팔 · AMR · 경광등)이고, 배정만 라우터가 한다.

| 항목 | 3D 판에서 달라진 점 | 이유 |
|---|---|---|
| 작업대 위치 | 2D 와 같은 상대 배치(용접 5 m · 검사 10 m · 위아래 ±2.5 m)를 소재 랙 오른쪽 빈 구역 (14, 6.5) m 로 평행이동 | fdw-ops 3D 홀은 x 0~36 · y 0~12.4 m 이고 x 4~12 가 랙이다. 오프라인 경로 탐색으로 고른 값 |
| AMR 모델 | fdw-ops 의 기본 상자형(노란 상자+바퀴, 1.2×0.8 m) | 기본 MiR100 은 NVIDIA 6.1 자산 서버에 없다(404). iw_hub 실물 USD 는 반경 1.1 m 라 랙과 작업대 사이에 정차·주차 경로가 안 나오고, Nova Carter 는 6.1 USD 외곽이 홀보다 크게 측정된다. `--real-amr` 로 iw_hub 를 쓸 수는 있다(랙을 끄고 작업대를 (8, 6.5) 로 옮겨야 한다) |
| AMR 이송 | fdw-ops 의 실제 경로 계획 · 도킹 · 통로 점유 (2D 판은 거리 ÷ 1 m/s) | Isaac 모드의 동작 |
| 카메라 | 홀 전체가 들어오는 높은 사선 구도 (`CAM`, 초점거리 20 mm) | fdw-ops 자동 구도는 앞줄 기둥이 작업대를 가린다 |

### 결과

| 지표 | 원본 | 라우터 |
|---|---|---|
| 마지막 주문 완료 | 777 s | 695 s (11 % 단축) |
| 배정 | 용접 8 → WELD_01, 검사 8 → INSP_01 | 용접 4 : 4, 검사 4 : 4 |
| 가동 (용접 1 / 2) | 322 / 0 s | 161 / 161 s |
| 가동 (검사 1 / 2) | 120 / 0 s | 60 / 80 s |
| 완료 시각 | 137, 239, 332, 427, 528, 621, 716, 777 | 228, 266, 387, 432, 575, 624, 638, 695 |

**읽는 법.** 배정은 2D 와 똑같이 둘로 갈라진다(결정 16건, "못 정하겠어요" 알림(2014) 0건). 그런데 단축 폭은 30 % → 11 % 로 줄었다. 3D 판에서는 AMR 이송(경로 · 도킹 · 통로 공유 · 집기/놓기)이 한 건당 100 초 안팎이라 **병목이 작업대가 아니라 이송**이 되기 때문이다. 원본은 약 95 초마다 1건, 라우터는 약 170 초마다 2건을 끝낸다. 라우터는 작업대 병목을 푸는 장치이고 이송 병목은 AMR 대수·속도 또는 비용 의 Transport 항 가중치로 다뤄야 한다는 점을 그대로 보여 준다. 2D(작업대 병목)와 3D(이송 병목)를 나란히 두고 보면 이 차이가 보인다.

### 산출물 (`output/`)

| 파일 | 내용 |
|---|---|
| `demo_3d.mp4` | 나란히 영상, 1920×880, 10 fps, 약 80 초. 왼쪽 원본 · 오른쪽 라우터, 아래에 완료 건수와 가동 작업대 |
| `demo_3d_router_only.mp4` | 라우터 쪽 원본 해상도(1920×1080) 단독 영상 |
| `demo_3d_frame_{start,mid,end}.png` | 대표 프레임 3장 |
| `frames_3d/{original,router}/f_NNNNN.png` | 원본 프레임 (1 장 = 시뮬레이션 1 초) |
| `demo_timeline_{original,router}_3d.json` · `demo_summary_3d.json` | 사건 · 프레임별 상태 · 수치 |
| `isaac_{original,router}.log` | Isaac Sim 실행 로그 |

### 직접 보기 (Isaac Sim 창)

`3D로_보기_2_라우터.bat` 을 더블클릭하면 Isaac Sim 창이 뜨고 라우터 판이 실시간 속도로 돈다 (첫 부팅 40 초 안팎). 주문 8건이 끝나도 창은 남고, 창을 닫거나 Ctrl+C 로 끝낸다. 원본 판은 `3D로_보기_1_원본공장장.bat`. 창에서는 왼쪽 드래그 회전 · 가운데 드래그 이동 · 휠 줌, Stage 패널의 `/World/FDW/Cells` 에 작업대가 있다.

### 다시 만들기

```powershell
# 1) Isaac Sim venv 로 프레임 생성 (원본 · 라우터 각 3~4 분, headless)
C:\isaacsim\env\Scripts\python.exe -X utf8 run_demo_3d.py              # --every 0.5 로 프레임을 촘촘히
# 2) 영상 합성 (fdw-router .venv + ffmpeg)
..dw-router\.venv\Scripts\python.exe -X utf8 compose_3d.py
```

카메라는 `run_demo_3d.py` 의 `CAM` (또는 `--cam-distance`, `--cam-height`, `--cam-focal`), 배치 이동은 `OFFSET_3D` 로 바꾼다. 첫 실행은 셰이더 컴파일과 NVIDIA 자산 다운로드로 1~2 분 더 걸린다.
