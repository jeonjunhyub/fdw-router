# -*- coding: utf-8 -*-
"""라우터 시연 — 공정당 작업대가 둘일 때 원본 오케스트레이터(고정 표)와 라우터(작업대 고르기)를 같은 조건으로 나란히 돌려
영상(MP4)·이미지(PNG)·수치(JSON)를 만든다. 연구보고서용.

시나리오 S6-데모 (우리가 정한 상황):
  작업대   MAT_01(소재, AMR 3대) · WELD_01 · WELD_02 (용접 40 s, 같은 성능) · INSP_01 (검사 15 s) · INSP_02 (검사 20 s)
  주문     8건, 공정 순서 용접 → 검사. JOB_5 는 긴급(high)
  원본     process_to_cell 고정 표 → 용접은 전부 WELD_01, 검사는 전부 INSP_01 (두 번째 작업대는 논다)
  라우터   할 수 있는 작업대 거르기 → 급한 순서 점수 → 작업대 비용 로 두 작업대에 나눠 배정

실행: <fdw-router>\\.venv\\Scripts\\python.exe -X utf8 run_demo.py [--no-video]
출력: output/
  demo_timeline_<label>.json   셀별 상태 구간 · 이송 · 완료 사건 (시뮬레이션 초)
  demo_summary.json            KPI 나란히
  demo_gantt.png               최종 간트 비교 (원본 vs 라우터)
  demo_kpi.png                 KPI 막대 비교
  demo_decisions.png           라우터 결정 로그 표 (첫 10건)
  demo_frame_*.png             영상 대표 프레임 3장
  demo.mp4                     나란히 재생 영상 (1920x1080, 10 fps)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent             # fdw-router\demo
REPO = HERE.parent                                  # fdw-router (저장소)
DEV = REPO.parent                                   # 저장소 한 단계 위 (external/ 이 여기 있다)
PROJECT = DEV.parent                                # 두 단계 위
sys.path.insert(0, str(REPO))
for _b in (DEV, PROJECT):
    if (_b / "FDW_Y1_DataDefinition_v2.2").is_dir():
        os.environ.setdefault("FDW_SPEC_ROOT", str(_b / "FDW_Y1_DataDefinition_v2.2")); break
os.environ.setdefault("FDW_OPS_ROOT", str(DEV / "external" / "fdw-ops"))

from fdw_router.adapters.fdwops_cell import ensure_fdw_ops_importable  # noqa: E402
ensure_fdw_ops_importable()

from fdw_sim.messaging.bus import InMemoryBus, Topics  # noqa: E402
from fdw_sim.messaging.schemas import CellType, JobSpec  # noqa: E402
from fdw_sim.cells.base.cell_base import CellConfig  # noqa: E402
from fdw_sim.cells.material.material_cell import MaterialCell  # noqa: E402
from fdw_sim.cells.welding.welding_cell import WeldingCell  # noqa: E402
from fdw_sim.cells.inspection.inspection_cell import InspectionCell  # noqa: E402
from fdw_sim.orchestrator.orchestrator import OrchestratorConfig  # noqa: E402
from fdw_sim.simulation.manager import SimulationManager, SimulationConfig  # noqa: E402
import fdw_sim.simulation.manager as manager  # noqa: E402
from fdw_router.integrations.fdwops import RouterOrchestrator  # noqa: E402

OUT = Path(os.environ.get("FDW_DEMO_OUT") or HERE / "output"); OUT.mkdir(parents=True, exist_ok=True)   # 환경변수 FDW_DEMO_OUT 으로 바꿀 수 있다

# ── 시나리오 ────────────────────────────────────────────────────────
CELLS = {   # cell_id: (type, cycle_s, (x, y))
    "MAT_01":  ("material",   None, (0.0, 0.0)),
    "WELD_01": ("welding",    40.0, (5.0, 2.5)),
    "WELD_02": ("welding",    40.0, (5.0, -2.5)),
    "INSP_01": ("inspection", 15.0, (10.0, 2.5)),
    "INSP_02": ("inspection", 20.0, (10.0, -2.5)),
}
N_JOBS = 8
RUSH = {"JOB_5"}
STATIC_MAP = {"welding": "WELD_01", "inspection": "INSP_01"}


def build(use_router: bool, label: str):
    random.seed(0)
    bus = InMemoryBus()
    original = manager.FDWOrchestrator
    if use_router:
        manager.FDWOrchestrator = RouterOrchestrator
    try:
        sim = SimulationManager(
            SimulationConfig(mode="discrete", physics_hz=60.0, max_sim_time_sec=900.0, headless=True,
                             log_dir=OUT / f"kpi_{label}", run_name=label),
            bus=bus,
            orchestrator_config=OrchestratorConfig(material_cell_id="MAT_01", process_to_cell=dict(STATIC_MAP)),
        )
    finally:
        manager.FDWOrchestrator = original
    cells = {}
    for cid, (ctype, cyc, loc) in CELLS.items():
        if ctype == "material":
            c = MaterialCell(CellConfig(cell_id=cid, cell_type=CellType.MATERIAL, input_capacity=16, output_capacity=16), bus=bus, num_amrs=3)
            sim.register_material_cell(c, location=loc)
        elif ctype == "welding":
            c = WeldingCell(CellConfig(cell_id=cid, cell_type=CellType.WELDING), bus=bus, default_cycle_time=cyc)
            c.gap_agent.predict_gap_mm = lambda _p: 0.2      # 결정론적 합격 (시연은 배정 효과에 집중)
            sim.register_cell(c, location=loc)
        else:
            c = InspectionCell(CellConfig(cell_id=cid, cell_type=CellType.INSPECTION), bus=bus, default_cycle_time=cyc)
            sim.register_cell(c, location=loc)
        cells[cid] = c
    return sim, bus, cells


def record_run(use_router: bool, label: str) -> dict:
    sim, bus, cells = build(use_router, label)
    seg: dict[str, list] = {cid: [] for cid in cells}          # cell → [ {state, job, t0, t1} ]
    cur: dict[str, tuple] = {cid: (None, None, 0.0) for cid in cells}
    events: list[dict] = []
    buffers: dict[str, list] = {cid: [] for cid in cells}       # cell → [ {t, in, out} ] (변화 시점만)
    last_buf: dict[str, tuple] = {cid: (None, None) for cid in cells}

    def hook(dt: float, t: float):
        for cid, c in cells.items():
            st = c.fsm.state.value
            part = getattr(c, "current_part_id", None) or (c.input_buffer.part_id if c.input_buffer.occupied else None)
            job = c.current_job.job_id if c.current_job is not None else (("JOB_" + str(part).split("_")[-1]) if (part and st == "PROCESSING") else None)
            if (st, job) != cur[cid][:2]:
                ps, pj, t0 = cur[cid]
                if ps is not None:
                    seg[cid].append({"state": ps, "job": pj, "t0": round(t0, 2), "t1": round(t, 2)})
                cur[cid] = (st, job, t)
            b = (c.input_buffer.part_id if c.input_buffer.occupied else None,
                 c.output_buffer.part_id if c.output_buffer.occupied else None)
            if b != last_buf[cid]:
                buffers[cid].append({"t": round(t, 2), "in": b[0], "out": b[1]}); last_buf[cid] = b

    sim.add_step_hook(hook)
    bus.subscribe(Topics.MATERIAL_TRANSFER, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "transfer", "from": m.from_cell, "to": m.to_cell, "part": m.part_id, "carrier": m.carrier}))
    bus.subscribe(Topics.DISPATCH_COMMAND, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "dispatch", "cell": m.target_cell, "job": m.job_id}))
    bus.subscribe(Topics.JOB_COMPLETED, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "completed", "job": (m.get("job_id") if isinstance(m, dict) else getattr(m, "job_id", None))}))
    bus.subscribe(Topics.JOB_QUARANTINED, lambda m: events.append({"t": round(sim.sim_time, 2), "kind": "quarantined", "job": (m.get("job_id") if isinstance(m, dict) else getattr(m, "job_id", None))}))

    sim.start()
    for i in range(N_JOBS):
        jid = f"JOB_{i}"
        sim.submit_job(JobSpec(job_id=jid, part_id=f"PART_{i}", part_type="frame", process_route=["welding", "inspection"],
                               priority="high" if jid in RUSH else "normal", recipe_overrides={"welding": {"speed": 0.10, "power": 1500.0, "path_length_mm": 4000.0}}))   # 4000 mm / 0.10 m/s = 40 s
    sim.run_until_done(all_jobs_done=True, extra_idle_sec=2.0)
    sim.stop()
    t_end = sim.sim_time
    for cid, (ps, pj, t0) in cur.items():
        if ps is not None:
            seg[cid].append({"state": ps, "job": pj, "t0": round(t0, 2), "t1": round(t_end, 2)})

    orch = sim.orchestrator
    summ = sim.kpi.summary()
    busy = {cid: round(sum(s["t1"] - s["t0"] for s in seg[cid] if s["state"] == "PROCESSING"), 1) for cid in cells if cid != "MAT_01"}
    result = {
        "label": label, "use_router": use_router, "sim_time_s": round(t_end, 1),
        "completed": len(orch.completed_jobs), "quarantined": len(orch.quarantined_jobs), "active": len(orch.active_jobs),
        "makespan_avg_s": round(summ.get("job_makespan_sec", {}).get("avg", float("nan")), 1),
        "makespan_max_s": round(summ.get("job_makespan_sec", {}).get("max", float("nan")), 1),
        "busy_s": busy, "last_completion_s": round(max([e["t"] for e in events if e["kind"] in ("completed", "quarantined")] or [t_end]), 1),
    }
    decisions = []
    if use_router:
        orch.close()
        result.update({"router_decisions": orch.stats.decisions, "replan_2014_events": orch.stats.replanning_events, "decision_log": str(orch.stats.log_path)})
        for line in Path(orch.stats.log_path).read_text(encoding="utf-8").splitlines():
            if line.strip(): decisions.append(json.loads(line))
    data = {"result": result, "segments": seg, "buffers": buffers, "events": events, "decisions": decisions,
            "cells": {cid: {"type": v[0], "cycle_s": v[1], "xy": v[2]} for cid, v in CELLS.items()}, "n_jobs": N_JOBS, "rush": sorted(RUSH)}
    (OUT / f"demo_timeline_{label}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ── 그림 ────────────────────────────────────────────────────────────
def setup_mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    for cand in ["Malgun Gothic", "NanumGothic", "Gulim"]:
        if any(f.name == cand for f in fm.fontManager.ttflist):
            plt.rcParams["font.family"] = cand; break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


JOB_COLORS = ["#2563EB", "#4F46E5", "#059669", "#D97706", "#DC2626", "#0891B2", "#7C3AED", "#DB2777"]
STATE_FC = {"IDLE": "#F8FAFC", "READY": "#EFF6FF", "PROCESSING": "#DBEAFE", "BLOCKED": "#FEF3C7", "FAULT": "#FEE2E2", "OFFLINE": "#E5E7EB", "RECOVERY": "#FEF3C7"}
STATE_KO = {"IDLE": "대기", "READY": "준비", "PROCESSING": "작업 중", "BLOCKED": "막힘", "FAULT": "고장", "OFFLINE": "꺼짐", "RECOVERY": "복구"}
INK, SLATE, LINE = "#1E3A5F", "#64748B", "#CBD5E1"


def job_color(job): return JOB_COLORS[int(job.split("_")[-1]) % len(JOB_COLORS)] if job else "#94A3B8"


def state_at(segs, t):
    for s in segs:
        if s["t0"] <= t < s["t1"]: return s["state"], s["job"]
    return (segs[-1]["state"], segs[-1]["job"]) if segs and t >= segs[-1]["t1"] else ("IDLE", None)


def buffer_at(bufs, t):
    cur = (None, None)
    for b in bufs:
        if b["t"] <= t: cur = (b["in"], b["out"])
        else: break
    return cur


def transfers_at(events, t):
    """진행 중인 이송 (출발했고 아직 도착 안 함). 도착은 목적지 입력 버퍼에 부품이 들어오는 시각으로 본다 — 호출자가 전달."""
    return [e for e in events if e["kind"] == "transfer" and e["t"] <= t < e.get("t_arrive", e["t"] + 1e9)]


def annotate_arrivals(data):
    """이송 사건에 도착 시각을 붙인다: 목적지 셀 입력 버퍼에 해당 부품이 나타나는 첫 시각.
    출발~도착 사이는 (1) 출발지에서 AMR 을 기다리다 (2) 마지막 travel_s 동안 이동하는 것으로 그린다 (거리 / 1 m/s + 2 s)."""
    import math
    cells = data["cells"]
    for e in data["events"]:
        if e["kind"] != "transfer": continue
        dst = e["to"]; part = e["part"]
        arr = [b["t"] for b in data["buffers"].get(dst, []) if b["t"] >= e["t"] and b["in"] == part]
        (x1, y1), (x2, y2) = cells[e["from"]]["xy"], cells[dst]["xy"]
        e["travel_s"] = math.hypot(x2 - x1, y2 - y1) / 1.0 + 2.0
        e["t_arrive"] = min(arr) if arr else e["t"] + 1e9        # 끝까지 도착 못 한 이송은 출발지 대기로 그린다


def draw_map(ax, data, t, title):
    ax.clear(); ax.set_xlim(-2.6, 12.6); ax.set_ylim(-5.0, 5.0); ax.set_aspect("equal"); ax.axis("off")
    ax.set_title(title, fontsize=15, weight="bold", color=INK, loc="left", pad=8)
    from matplotlib.patches import FancyBboxPatch, Circle
    cells = data["cells"]
    for cid, info in cells.items():
        x, y = info["xy"]; st, job = state_at(data["segments"][cid], t)
        w, h = (3.3, 2.3)
        ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.35", fc=STATE_FC.get(st, "#fff"), ec="#94A3B8", lw=1.2))
        name = {"material": "소재 창고", "welding": "용접대", "inspection": "검사대"}[info["type"]]
        ax.text(x, y + 0.68, f"{name} {cid.split('_')[-1]}", ha="center", va="center", fontsize=11, weight="bold", color=INK)
        sub = f"{info['cycle_s']:.0f} s/개" if info["cycle_s"] else "AMR 3대"
        ax.text(x, y + 0.2, sub, ha="center", va="center", fontsize=8.5, color=SLATE)
        label = STATE_KO.get(st, st) + (f" · {job}" if job else "")
        ax.text(x, y - 0.45, label, ha="center", va="center", fontsize=9, color=job_color(job) if job else SLATE, weight="bold" if job else "normal")
        bi, bo = buffer_at(data["buffers"][cid], t)
        for part, px, lab in ((bi, x - w / 2 + 0.45, "입력"), (bo, x + w / 2 - 0.45, "출력")):
            if not part: continue
            ax.add_patch(Circle((px, y - h / 2 + 0.45), 0.28, fc=job_color("JOB_" + part.split("_")[-1]), ec="white", lw=1, zorder=4))
            ax.text(px, y - h / 2 + 0.45, part.split("_")[-1], ha="center", va="center", fontsize=7, color="white", weight="bold", zorder=5)
            ax.text(px, y - h / 2 + 0.95, lab, ha="center", fontsize=6.5, color=SLATE)
    # 통로
    for a, b in (("MAT_01", "WELD_01"), ("MAT_01", "WELD_02"), ("WELD_01", "INSP_01"), ("WELD_02", "INSP_02"), ("WELD_01", "INSP_02"), ("WELD_02", "INSP_01")):
        (x1, y1), (x2, y2) = cells[a]["xy"], cells[b]["xy"]
        ax.plot([x1, x2], [y1, y2], color=LINE, lw=1, ls=(0, (4, 4)), zorder=0)
    # 이송 중 부품: 출발지 아래 대기 열 → 마지막 travel_s 동안 선을 따라 이동
    waiting: dict = {}
    for e in transfers_at(data["events"], t):
        (x1, y1), (x2, y2) = cells[e["from"]]["xy"], cells[e["to"]]["xy"]
        num = e["part"].split("_")[-1]
        t_go = e["t_arrive"] - e["travel_s"]
        if t < t_go:
            k = waiting.setdefault(e["from"], 0); waiting[e["from"]] = k + 1
            px, py = x1 - w / 2 + 0.45 + 0.7 * k, y1 - h / 2 - 0.75
            ax.add_patch(Circle((px, py), 0.3, fc=job_color("JOB_" + num), ec="white", lw=1.2, zorder=5))
            ax.text(px, py, num, ha="center", va="center", fontsize=7, color="white", weight="bold", zorder=6)
            if k == 0: ax.text(x1 - w / 2 + 0.1, y1 - h / 2 - 1.25, "AMR 대기", ha="left", va="center", fontsize=7, color=SLATE)
        else:
            r = min(max((t - t_go) / max(e["travel_s"], 0.1), 0.0), 1.0)
            px, py = x1 + (x2 - x1) * r, y1 + (y2 - y1) * r
            ax.add_patch(Circle((px, py), 0.4, fc=job_color("JOB_" + num), ec="white", lw=1.5, zorder=5))
            ax.text(px, py, num, ha="center", va="center", fontsize=8, color="white", weight="bold", zorder=6)
    # 창고 재고·완료
    done = [ev for ev in data["events"] if ev["kind"] in ("completed", "quarantined") and ev["t"] <= t]
    ax.text(-2.4, 4.5, f"완료 {len(done)} / {data['n_jobs']}", fontsize=12, weight="bold", color="#059669" if len(done) == data["n_jobs"] else INK)


def draw_gantt(ax, data, t, t_max, title=None, final=False):
    ax.clear()
    cells = [c for c in data["cells"] if c != "MAT_01"]
    ax.set_xlim(0, t_max); ax.set_ylim(-0.6, len(cells) - 0.4); ax.invert_yaxis()
    ax.set_yticks(range(len(cells))); ax.set_yticklabels([f"{ {'welding': '용접대', 'inspection': '검사대'}[data['cells'][c]['type']]} {c.split('_')[-1]} ({data['cells'][c]['cycle_s']:.0f} s)" for c in cells], fontsize=10)
    ax.set_xlabel("시뮬레이션 시간 (초)", fontsize=10, color=SLATE)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    ax.spines["left"].set_color(LINE); ax.spines["bottom"].set_color(LINE); ax.tick_params(colors=SLATE)
    ax.grid(axis="x", color="#EEF2F7", lw=0.8); ax.set_axisbelow(True)
    for i, cid in enumerate(cells):
        for s in data["segments"][cid]:
            if s["state"] != "PROCESSING" or s["t0"] >= t: continue
            t1 = min(s["t1"], t)
            ax.barh(i, t1 - s["t0"], left=s["t0"], height=0.62, color=job_color(s["job"]), edgecolor="white", lw=0.8)
            if t1 - s["t0"] > 6: ax.text(s["t0"] + (t1 - s["t0"]) / 2, i, s["job"].replace("JOB_", "J"), ha="center", va="center", fontsize=8, color="white", weight="bold")
    if not final: ax.axvline(t, color="#DC2626", lw=1.2)
    if title: ax.set_title(title, fontsize=12, weight="bold", color=INK, loc="left")
    busy = data["result"]["busy_s"]
    for i, cid in enumerate(cells):
        b = sum(min(s["t1"], t) - s["t0"] for s in data["segments"][cid] if s["state"] == "PROCESSING" and s["t0"] < t)
        ax.text(t_max * 0.995, i, f"가동 {b / max(t, 1e-9) * 100:4.0f}%", ha="right", va="center", fontsize=8.5, color=SLATE)


def render(orig, rout, make_video=True):
    plt = setup_mpl()
    import matplotlib.animation as animation
    for d in (orig, rout): annotate_arrivals(d)
    t_max = max(orig["result"]["sim_time_s"], rout["result"]["sim_time_s"])
    t_max_g = (int(t_max / 20) + 1) * 20

    def frame_fig():
        fig = plt.figure(figsize=(19.2, 10.8), dpi=100, facecolor="white")
        gs = fig.add_gridspec(2, 2, width_ratios=[1.0, 1.25], left=0.03, right=0.985, top=0.9, bottom=0.07, hspace=0.42, wspace=0.12)
        axes = {"om": fig.add_subplot(gs[0, 0]), "og": fig.add_subplot(gs[0, 1]), "rm": fig.add_subplot(gs[1, 0]), "rg": fig.add_subplot(gs[1, 1])}
        return fig, axes

    def draw(fig, axes, t):
        draw_map(axes["om"], orig, t, "원본 오케스트레이터 — 고정 표 (용접은 1번 작업대, 검사는 1번 작업대만)")
        draw_map(axes["rm"], rout, t, "우리 라우터 — 할 수 있는 곳 → 급한 순서 → 가장 싼 작업대")
        draw_gantt(axes["og"], orig, t, t_max_g, "작업대 점유 (원본)")
        draw_gantt(axes["rg"], rout, t, t_max_g, "작업대 점유 (라우터)")
        fig.suptitle(f"AI 라우터 시연 — 같은 주문 8건, 같은 작업대, 같은 seed      시뮬레이션 시각 {t:6.1f} s", fontsize=16, weight="bold", color=INK, x=0.03, ha="left", y=0.965)
        fig.text(0.985, 0.965, f"완료까지  원본 {orig['result']['last_completion_s']:.0f} s   vs   라우터 {rout['result']['last_completion_s']:.0f} s", ha="right", va="center", fontsize=13, color="#059669", weight="bold")

    # 대표 프레임 3장
    fig, axes = frame_fig()
    for name, t in (("start", 12.0), ("mid", t_max * 0.45), ("end", t_max)):
        draw(fig, axes, t); fig.savefig(OUT / f"demo_frame_{name}.png", dpi=100)
    plt.close(fig)

    # 최종 간트 비교
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(14, 6.5), dpi=150, sharex=True, facecolor="white")
    draw_gantt(a1, orig, t_max, t_max_g, f"원본 오케스트레이터 (고정 표) — 마지막 완료 {orig['result']['last_completion_s']:.0f} s", final=True)
    draw_gantt(a2, rout, t_max, t_max_g, f"우리 라우터 (작업대 고르기) — 마지막 완료 {rout['result']['last_completion_s']:.0f} s", final=True)
    a1.set_xlabel(""); fig.suptitle("작업대 점유 비교 — 주문 8건, 용접대 2 · 검사대 2, 같은 seed", fontsize=14, weight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95)); fig.savefig(OUT / "demo_gantt.png"); plt.close(fig)

    # KPI 막대
    fig, axs = plt.subplots(1, 3, figsize=(14, 4.2), dpi=150, facecolor="white")
    labels = ["원본", "라우터"]; cols = ["#94A3B8", "#2563EB"]
    for ax, (title, vals, unit) in zip(axs, [
        ("마지막 주문 완료 시각", [orig["result"]["last_completion_s"], rout["result"]["last_completion_s"]], "s"),
        ("평균 makespan (주문당)", [orig["result"]["makespan_avg_s"], rout["result"]["makespan_avg_s"]], "s"),
        ("두 번째 작업대 가동 시간 (WELD_02 + INSP_02)", [orig["result"]["busy_s"]["WELD_02"] + orig["result"]["busy_s"]["INSP_02"], rout["result"]["busy_s"]["WELD_02"] + rout["result"]["busy_s"]["INSP_02"]], "s"),
    ]):
        bars = ax.bar(labels, vals, color=cols, width=0.55)
        for b, v in zip(bars, vals): ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.0f} {unit}", ha="center", va="bottom", fontsize=11, weight="bold", color=INK)
        ax.set_title(title, fontsize=11, weight="bold", color=INK)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color(LINE); ax.spines["bottom"].set_color(LINE); ax.tick_params(colors=SLATE); ax.set_ylim(0, max(vals) * 1.25 + 1)
    fig.tight_layout(); fig.savefig(OUT / "demo_kpi.png"); plt.close(fig)

    # 결정 로그 표
    rows = []
    for d in rout["decisions"][:10]:
        comp = d.get("cost_components", {})
        cand = " / ".join(f"{c}: {comp[c]['cost']:.1f}" for c in d["candidates"] if c in comp)
        rows.append([f"{d['sim_time']:.1f}", d["job_id"], d["process"], cand, d["chosen"], f"{d['decision_ms']:.2f}"])
    fig, ax = plt.subplots(figsize=(14, 0.5 + 0.42 * (len(rows) + 1)), dpi=150, facecolor="white"); ax.axis("off")
    tb = ax.table(cellText=rows, colLabels=["시각 (s)", "주문", "공정", "후보 작업대와 비용 (초)", "선택", "판단 (ms)"], loc="center", cellLoc="left", colWidths=[0.08, 0.08, 0.1, 0.5, 0.1, 0.1])
    tb.auto_set_font_size(False); tb.set_fontsize(9); tb.scale(1, 1.5)
    for (r, c), cell in tb.get_celld().items():
        cell.set_edgecolor("#E2E8F0")
        if r == 0: cell.set_facecolor(INK); cell.get_text().set_color("white"); cell.get_text().set_weight("bold")
        elif r % 2 == 0: cell.set_facecolor("#F8FAFC")
    ax.set_title("라우터 결정 로그 (첫 10건) — 후보마다 비용을 초로 계산해 가장 작은 작업대를 고른다", fontsize=11, weight="bold", color=INK, loc="left")
    fig.tight_layout(); fig.savefig(OUT / "demo_decisions.png"); plt.close(fig)

    if make_video:
        fig, axes = frame_fig()
        ts = [i * 1.0 for i in range(int(t_max) + 1)] + [t_max] * 20          # 1 s/프레임, 끝에 2 초 정지
        writer = animation.FFMpegWriter(fps=10, bitrate=4000, metadata={"title": "FDW-Ops AI router demo"})
        with writer.saving(fig, str(OUT / "demo.mp4"), dpi=100):
            for t in ts:
                draw(fig, axes, t); writer.grab_frame()
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--no-video", action="store_true"); args = ap.parse_args()
    t0 = time.time()
    orig = record_run(False, "original"); print("original:", json.dumps(orig["result"], ensure_ascii=False))
    rout = record_run(True, "router"); print("router:  ", json.dumps(rout["result"], ensure_ascii=False))
    (OUT / "demo_summary.json").write_text(json.dumps({"scenario": {"cells": CELLS, "n_jobs": N_JOBS, "rush": sorted(RUSH), "static_map": STATIC_MAP},
                                                        "original": orig["result"], "router": rout["result"]}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    render(orig, rout, make_video=not args.no_video)
    print(f"done in {time.time() - t0:.0f}s →", OUT)


if __name__ == "__main__":
    main()
