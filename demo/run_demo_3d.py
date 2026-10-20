# -*- coding: utf-8 -*-
"""라우터 시연 — Isaac Sim 3D 판. run_demo.py 와 같은 시나리오(S6-데모)를 fdw-ops 의 Isaac 모드로 돌리면서
뷰포트를 1 시뮬레이션-초마다 PNG 로 저장한다. 원본(고정 표)과 라우터(J축)를 각각 별도 프로세스로 돌린다
(SimulationApp 은 프로세스당 한 번만 띄울 수 있다).

실행 (Isaac Sim 전용 venv, Python 3.12):
  C:\\isaacsim\\env\\Scripts\\python.exe -X utf8 run_demo_3d.py              # original · router 둘 다 (자식 프로세스 2개)
  C:\\isaacsim\\env\\Scripts\\python.exe -X utf8 run_demo_3d.py --label router --router   # 한 번만
  옵션: --gui (Isaac Sim 창을 띄워 실시간으로 본다; --every 0 이면 캡처 없이 보기만) · 기본은 headless · --every 1.0 (프레임 간격, 시뮬레이션 초) · --max-frames N (시험용)
        --placeholder (NVIDIA 자산 USD 대신 상자 placeholder — 자산 다운로드가 안 될 때)

출력: output/ (환경변수 FDW_DEMO_OUT 으로 바꿀 수 있다) frames_3d/<label>/f_00000.png ... (1 장 = 캡처 1 회) 과 output/demo_timeline_<label>_3d.json
      영상 합성은 compose_3d.py (fdw-router .venv, ffmpeg) 가 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
os.environ.setdefault("PRIVACY_CONSENT", "Y")
os.environ.setdefault("ACCEPT_EULA", "Y")

sys.path.insert(0, str(HERE))
import run_demo  # noqa: E402  (시나리오 · build · 경로 설정을 그대로 쓴다)

from fdw_sim.messaging.bus import Topics  # noqa: E402
from fdw_sim.messaging.schemas import JobSpec  # noqa: E402
from fdw_sim.simulation.manager import SimulationConfig  # noqa: E402

OUT = run_demo.OUT
FRAMES = OUT / "frames_3d"

# fdw-ops 의 3D 작업장(배치도 1안)은 x 0~36.1 m, y 0~12.4 m 홀이고 x 4~12 에 소재 랙 3줄, 14.5~18 에 캔틸레버 랙, 26~29 에 성형기가 있다.
# 2D 시연의 상대 배치(용접 5 m, 검사 10 m, 위아래 ±2.5 m)는 그대로 두고 랙 오른쪽 빈 구역으로 평행이동한다 (오프라인 경로 탐색으로 고른 값).
OFFSET_3D = (14.0, 6.5)
CAM = {"distance": 19.0, "height": 23.0, "focal": 20.0}   # --cam-distance / --cam-height 로 조정 (랙부터 검사대까지 홀 전체가 들어오는 값)
AMR_ASSET_3D = "iw_hub"          # 실 USD 를 쓸 때의 모델. 기본값은 placeholder(노란 상자+바퀴, 1.2×0.8 m): fdw-ops 기본 mir100 은 NVIDIA 6.1 자산 서버에 없고(404),
                                 # iw_hub 는 커서(반경 1.1 m) 랙과 같이 두면 AMR 정차·주차 경로가 안 나오며, nova_carter 는 6.1 USD 의 외곽이 홀보다 크게 측정된다.
REAL_AMR_3D = False              # --real-amr 로 켠다


def _make_config_factory(headless: bool, placeholder: bool, width: int, height: int):
    """run_demo.build 가 만드는 SimulationConfig 를 Isaac 모드로 바꿔치기한다."""
    def factory(**kw):
        kw.update(mode="isaac", headless=headless, enable_visualization=True,
                  transfer_anim_duration_sec=2.0,
                  motion_execution="schematic",          # fdw-ops 운영 데모 기본 (실 파지 검증은 별도)
                  amr_asset_name=AMR_ASSET_3D, use_real_amr=REAL_AMR_3D, show_workshop_layout=True,
                  realtime=not headless,                 # 창을 띄우면 실시간 속도로 재생한다
                  use_real_inspection_cam=False,         # 카메라 USD 도 자산 서버에 없다 (placeholder 자동)
                  isaac_app_kwargs={"width": width, "height": height})
        if placeholder:
            kw.update(use_real_robot=False, use_real_amr=False, use_real_smart_rack=False,
                      use_real_forming_arm=False, use_real_inspection_cam=False)
        else:
            kw.update(use_real_robot=True)
        return SimulationConfig(**kw)
    return factory


def run_one(label: str, use_router: bool, headless: bool, every: float, max_frames: int,
            placeholder: bool, width: int, height: int) -> dict:
    run_demo.SimulationConfig = _make_config_factory(headless, placeholder, width, height)
    ox, oy = OFFSET_3D
    run_demo.CELLS = {cid: (ctype, cyc, (x + ox, y + oy)) for cid, (ctype, cyc, (x, y)) in run_demo.CELLS.items()}
    sim, bus, cells = run_demo.build(use_router, label + "_3d")
    frame_dir = FRAMES / label
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in list(frame_dir.glob("rgb_*.png")) + list(frame_dir.glob("f_*.png")):
        old.unlink()

    events: list[dict] = []
    frames: list[dict] = []
    state = {"next": 0.0, "n": 0}

    bus.subscribe(Topics.DISPATCH_COMMAND, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "dispatch", "cell": m.target_cell, "job": m.job_id}))
    bus.subscribe(Topics.MATERIAL_TRANSFER, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "transfer", "from": m.from_cell, "to": m.to_cell, "part": m.part_id}))
    bus.subscribe(Topics.JOB_COMPLETED, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "completed", "job": (m.get("job_id") if isinstance(m, dict) else getattr(m, "job_id", None))}))

    sim.start()                                           # 여기서 SimulationApp · World · USD 장면이 만들어진다
    import omni.kit.viewport.utility as vp_util  # type: ignore
    import omni.replicator.core as rep  # type: ignore
    # 카메라: fdw-ops 자동 프레이밍은 앞줄 기둥이 작업대를 가린다 — 더 높고 가파른 각도로 다시 잡는다
    cx, cy = OFFSET_3D[0] + 2.5, OFFSET_3D[1] + 0.8
    sim.visualizer.scene.frame_viewport_to_scene(center=(cx, cy, 0.5), distance=CAM["distance"], height=CAM["height"], set_viewport_camera=True)
    from pxr import UsdGeom  # type: ignore
    import omni.usd  # type: ignore
    cam_prim = omni.usd.get_context().get_stage().GetPrimAtPath("/World/FDW/SceneCamera")
    if cam_prim:
        UsdGeom.Camera(cam_prim).GetFocalLengthAttr().Set(float(CAM["focal"]))   # 더 넓은 화각
    vp = vp_util.get_active_viewport()
    app = sim._isaac_app
    # Replicator BasicWriter 로 동기 캡처한다 (capture_viewport_to_file 은 비동기라 프레임이 밀린다).
    # 카메라는 fdw-ops 가 장면을 맞춰 둔 뷰포트 활성 카메라를 그대로 쓴다.
    rp = rep.create.render_product(vp.camera_path, (width, height))
    writer = rep.WriterRegistry.get("BasicWriter")
    writer.initialize(output_dir=str(frame_dir), rgb=True)
    writer.attach([rp])

    def capture(t: float):
        # headless 에서는 world.step(render=False) 라 화면이 갱신되지 않는다 — 캡처 때만 렌더한다.
        # BasicWriter 는 비동기라 한 step 에 파일이 둘 이상 나올 때가 있다 (라우터 실행에서 708 캡처 → 1,604 파일 실측).
        # 그래서 step 마다 끝날 때까지 기다리고, 새로 생긴 파일 중 마지막 것만 f_NNNNN.png 로 이름을 바꿔 남긴다.
        before = set(frame_dir.glob("rgb_*.png"))
        rep.orchestrator.step(rt_subframes=2, pause_timeline=True)
        rep.orchestrator.wait_until_complete()
        new = sorted(set(frame_dir.glob("rgb_*.png")) - before)
        if new:
            new[-1].replace(frame_dir / f"f_{state['n']:05d}.png")
            for extra in new[:-1]:
                extra.unlink()
        else:
            print(f"WARN no frame written at t={t:.1f}", flush=True)
        frames.append({"n": state["n"], "t": round(t, 2),
                       "cells": {cid: c.fsm.state.value for cid, c in cells.items()},
                       "completed": len(sim.orchestrator.completed_jobs)})
        state["n"] += 1

    def hook(dt: float, t: float):
        if every <= 0:
            return                                        # 캡처 없이 보기만 한다 (--every 0)
        if t + 1e-9 >= state["next"] and (max_frames <= 0 or state["n"] < max_frames):
            capture(t)
            state["next"] += every
    sim.add_step_hook(hook)

    for i in range(run_demo.N_JOBS):
        jid = f"JOB_{i}"
        sim.submit_job(JobSpec(job_id=jid, part_id=f"PART_{i}", part_type="frame", process_route=["welding", "inspection"],
                               priority="high" if jid in run_demo.RUSH else "normal",
                               recipe_overrides={"welding": {"speed": 0.10, "power": 1500.0, "path_length_mm": 4000.0}}))
    t_wall = time.time()
    sim.run_until_done(all_jobs_done=True, extra_idle_sec=2.0)
    if every > 0:
        capture(sim.sim_time)                             # 마지막 상태 한 장 더
    writer.detach()
    for leftover in frame_dir.glob("rgb_*.png"):
        leftover.unlink()
    t_end = sim.sim_time
    orch = sim.orchestrator
    result = {"label": label, "use_router": use_router, "sim_time_s": round(t_end, 1),
              "completed": len(orch.completed_jobs), "quarantined": len(orch.quarantined_jobs),
              "last_completion_s": round(max([e["t"] for e in events if e["kind"] == "completed"] or [t_end]), 1),
              "frames": state["n"], "wall_s": round(time.time() - t_wall, 1), "headless": headless, "placeholder": placeholder}
    if use_router:
        orch.close()
        result.update({"router_decisions": orch.stats.decisions, "replan_2014_events": orch.stats.replanning_events})
    (OUT / f"demo_timeline_{label}_3d.json").write_text(json.dumps({"result": result, "events": events, "frames": frames,
                                                                    "cells": {cid: {"type": v[0], "cycle_s": v[1], "xy": v[2]} for cid, v in run_demo.CELLS.items()}},
                                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
    if not headless:
        print("창을 닫거나 Ctrl+C 를 누르면 끝난다.", flush=True)
        sim.keep_viewer_alive()                           # 다 끝나도 창을 유지한다
    sim.stop()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", choices=["original", "router"], default=None, help="지정하면 그 한 번만 이 프로세스에서 돌린다")
    ap.add_argument("--router", action="store_true")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--every", type=float, default=1.0)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--placeholder", action="store_true")
    ap.add_argument("--real-amr", action="store_true", help="placeholder 대신 AMR_ASSET_3D 의 실 USD")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--cam-distance", type=float, default=CAM["distance"])
    ap.add_argument("--cam-height", type=float, default=CAM["height"])
    ap.add_argument("--cam-focal", type=float, default=CAM["focal"])
    args = ap.parse_args()
    global REAL_AMR_3D
    REAL_AMR_3D = REAL_AMR_3D or args.real_amr
    CAM.update(distance=args.cam_distance, height=args.cam_height, focal=args.cam_focal)

    if args.label:
        run_one(args.label, args.router or args.label == "router", headless=not args.gui, every=args.every,
                max_frames=args.max_frames, placeholder=args.placeholder, width=args.width, height=args.height)
        return

    results = {}
    for label in ("original", "router"):
        cmd = [sys.executable, "-X", "utf8", str(HERE / "run_demo_3d.py"), "--label", label, "--every", str(args.every),
               "--max-frames", str(args.max_frames), "--width", str(args.width), "--height", str(args.height),
               "--cam-distance", str(args.cam_distance), "--cam-height", str(args.cam_height), "--cam-focal", str(args.cam_focal)]
        if label == "router": cmd.append("--router")
        if args.gui: cmd.append("--gui")
        if args.placeholder: cmd.append("--placeholder")
        if args.real_amr: cmd.append("--real-amr")
        print(">>", label, flush=True)
        log = OUT / f"isaac_{label}.log"
        with log.open("w", encoding="utf-8", errors="replace") as fh:
            rc = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(HERE))
        txt = log.read_text(encoding="utf-8", errors="replace")
        line = [l for l in txt.splitlines() if l.startswith("RESULT ")]
        results[label] = json.loads(line[-1][7:]) if line else {"error": f"rc={rc}", "log": str(log)}
        print(label, results[label], flush=True)
    (OUT / "demo_summary_3d.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
