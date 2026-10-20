# -*- coding: utf-8 -*-
"""통합 지점 ① — fdw-ops 브리지와 RouterOrchestrator.

fdw-ops 소스(`external/fdw-ops`)가 없으면 전부 skip 한다 (vendor 코드는 저장소에 넣지 않는다).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

_SPEC_REL = Path("FDW_Y1_DataDefinition_v2.2")
# 저장소 두 단계 위 또는 한 단계 위에서 규격 폴더를 찾는다.
_V22 = next((p / _SPEC_REL for p in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parents[2])
             if (p / _SPEC_REL).is_dir()), None)
if "FDW_SPEC_ROOT" not in os.environ and _V22 is not None:
    os.environ["FDW_SPEC_ROOT"] = str(_V22)

from fdw_router.adapters import fdwops_cell as fc  # noqa: E402
from fdw_router.msg import envelope  # noqa: E402
from fdw_router.msg.build import EnvelopeFactory  # noqa: E402
from fdw_router.ports.cell_runtime import CellRuntime  # noqa: E402

try:
    fc.ensure_fdw_ops_importable()
    from fdw_sim.cells.base.cell_base import CellConfig  # type: ignore
    from fdw_sim.cells.inspection.inspection_cell import InspectionCell  # type: ignore
    from fdw_sim.cells.material.material_cell import MaterialCell  # type: ignore
    from fdw_sim.cells.welding.welding_cell import WeldingCell  # type: ignore
    from fdw_sim.messaging.bus import InMemoryBus, Topics  # type: ignore
    from fdw_sim.messaging.schemas import (  # type: ignore
        BufferStatus, CellState, CellStatusMessage, CellType, DispatchCommand, JobSpec,
        MaterialTransferCommand,
    )
    from fdw_sim.orchestrator.orchestrator import OrchestratorConfig  # type: ignore
    from fdw_sim.simulation.manager import SimulationConfig, SimulationManager  # type: ignore
    HAVE_FDW_OPS = True
except Exception:  # noqa: BLE001
    HAVE_FDW_OPS = False

pytestmark = pytest.mark.skipif(not HAVE_FDW_OPS, reason="external/fdw-ops 가 없다")


# ── 순수 변환 ───────────────────────────────────────────────────────

def test_state_mapping_table_covers_every_fdw_sim_state():
    """fdw_sim 의 7 상태가 전부 규격 7 상태 어휘로 간다."""
    spec_states = {"idle", "running", "paused", "blocked", "error", "maintenance", "offline"}
    for s in CellState:
        assert s.value in fc.STATE_MAP, s
        assert fc.STATE_MAP[s.value] in spec_states
    assert fc.STATE_MAP["IDLE"] == "idle" and fc.STATE_MAP["READY"] == "idle"
    assert fc.STATE_MAP["PROCESSING"] == "running"
    assert fc.STATE_MAP["FAULT"] == "error"
    assert fc.STATE_MAP["RECOVERY"] == "maintenance"


def test_node_id_matches_spec_pattern():
    import re
    pat = re.compile(r"^[a-z][a-z0-9_]+$")
    for raw in ("WELDING_CELL_01", "Nova-01", "S4_WAAM", "01bad"):
        assert pat.match(fc.node_id_of(raw)), raw


def test_task_id_roundtrip():
    tid = fc.make_task_id("JOB_00001", 1, "inspection")
    assert fc.parse_task_id(tid) == ("JOB_00001", 1, "inspection")


@pytest.mark.parametrize("state", list(CellState))
def test_cell_status_envelope_is_spec_valid(state):
    """fdw_sim 상태 메시지가 v2.2 `cell_status` 봉투(cell_state.schema.json)로 통과한다."""
    msg = CellStatusMessage(
        cell_id="WELDING_CELL_01", cell_type=CellType.WELDING, state=state,
        current_job_id="JOB_00001" if state == CellState.PROCESSING else None,
        input_buffer=BufferStatus(occupied=True, part_id="PART_A_001", queue_length=1),
        estimated_remaining_time_sec=12.5,
        current_command_id="CMD_x" if state == CellState.PROCESSING else None,
    )
    env = fc.cell_status_envelope(msg, 42.0, EnvelopeFactory(run_id="t"))
    res = envelope.validate(env)
    assert res.ok, res.errors
    assert env["from"] == {"system": "cell", "node_id": "welding_cell_01"}
    assert env["to"]["system"] == "router"
    assert env["payload"]["owner_subtask"] == "S2"
    assert env["payload"]["source"] == "simulated"


def test_task_request_to_dispatch_and_transfer():
    f = EnvelopeFactory(run_id="t")
    node_to_cell = {"welding_cell_01": "WELDING_CELL_01", "material_cell_01": "MATERIAL_CELL_01"}
    tid = fc.make_task_id("JOB_00001", 0, "welding")
    disp = f.make(message_type="task_request", frm=("router", "nuc-01"), to=("cell", "welding_cell_01"),
                  payload={"task_id": tid, "expected_duration_s": 18.0, "step_id": "START_WELDING",
                           "params": {"speed": 0.1}, "part_id": "PART_A_001"}, at=1.0)
    assert envelope.validate(disp).ok, envelope.validate(disp).errors
    cmd = fc.task_request_to_command(disp, node_to_cell)
    assert isinstance(cmd, DispatchCommand)
    assert cmd.target_cell == "WELDING_CELL_01" and cmd.job_id == "JOB_00001"
    assert cmd.operation == "START_WELDING" and cmd.recipe == {"speed": 0.1} and cmd.part_id == "PART_A_001"

    move = f.make(message_type="task_request", frm=("router", "nuc-01"), to=("fleet", "material_cell_01"),
                  payload={"task_id": fc.make_task_id("JOB_00001", 0, "transport"), "expected_duration_s": 30.0,
                           "from_loc": "material_cell_01", "to_loc": "welding_cell_01", "material_id": "PART_A_001"},
                  at=1.0)
    assert envelope.validate(move).ok, envelope.validate(move).errors
    mcmd = fc.task_request_to_command(move, node_to_cell)
    assert isinstance(mcmd, MaterialTransferCommand)
    assert (mcmd.from_cell, mcmd.to_cell, mcmd.part_id) == ("MATERIAL_CELL_01", "WELDING_CELL_01", "PART_A_001")


def test_runtime_satisfies_port_and_bridges_status():
    bus = InMemoryBus()
    clock = {"t": 5.0}
    rt = fc.FdwOpsCellRuntime(bus, now_fn=lambda: clock["t"], run_id="port")
    assert isinstance(rt, CellRuntime)
    bus.publish(Topics.CELL_STATUS, CellStatusMessage(cell_id="INSPECTION_CELL_01", cell_type=CellType.INSPECTION))
    out = list(rt.poll())
    assert len(out) == 1 and out[0]["message_type"] == "cell_status"
    assert envelope.validate(out[0]).ok
    assert list(rt.poll()) == []
    got = []
    bus.subscribe(Topics.DISPATCH_COMMAND, got.append)
    rt.send(EnvelopeFactory(run_id="port").make(
        message_type="task_request", frm=("router", "nuc-01"), to=("cell", "inspection_cell_01"),
        payload={"task_id": fc.make_task_id("J", 1, "inspection"), "expected_duration_s": 6.0,
                 "step_id": "START_INSPECTION", "params": {}}, at=5.0))
    assert got and got[0].target_cell == "INSPECTION_CELL_01"


# ── RouterOrchestrator (fdw-ops discrete) ───────────────────────────

def _build(tmp_path, route=("welding", "inspection")):
    import random
    import fdw_sim.simulation.manager as manager
    from fdw_router.integrations.fdwops import RouterOrchestrator

    random.seed(0)
    bus = InMemoryBus()
    original = manager.FDWOrchestrator
    manager.FDWOrchestrator = RouterOrchestrator
    try:
        sim = SimulationManager(
            SimulationConfig(mode="discrete", physics_hz=60.0, max_sim_time_sec=300.0, log_dir=tmp_path / "kpi"),
            bus=bus,
            orchestrator_config=OrchestratorConfig(material_cell_id="MAT_01",
                                                   process_to_cell={"welding": "WELD_01", "inspection": "INSP_01"}),
        )
    finally:
        manager.FDWOrchestrator = original
    assert isinstance(sim.orchestrator, RouterOrchestrator)
    material = MaterialCell(CellConfig(cell_id="MAT_01", cell_type=CellType.MATERIAL, input_capacity=8,
                                       output_capacity=8), bus=bus, num_amrs=2)
    welding = WeldingCell(CellConfig(cell_id="WELD_01", cell_type=CellType.WELDING), bus=bus, default_cycle_time=5.0)
    inspection = InspectionCell(CellConfig(cell_id="INSP_01", cell_type=CellType.INSPECTION), bus=bus,
                                default_cycle_time=2.0)
    welding.gap_agent.predict_gap_mm = lambda _p: 0.2
    sim.register_material_cell(material, location=(0.0, 0.0))
    sim.register_cell(welding, location=(2.0, 0.0))
    sim.register_cell(inspection, location=(4.0, 0.0))
    faults = []
    bus.subscribe(Topics.FAULT, faults.append)
    sim.start()
    for i in range(3):
        sim.submit_job(JobSpec(job_id=f"JOB_{i}", part_id=f"PART_{i}", part_type="frame",
                               process_route=list(route), priority="high" if i == 2 else "normal",
                               recipe_overrides={"welding": {"speed": 0.10, "power": 1500.0}}))
    sim.run_until_done(all_jobs_done=True, extra_idle_sec=1.0)
    sim.stop()
    return sim, faults


def test_router_orchestrator_completes_jobs_and_logs_decisions(tmp_path):
    sim, faults = _build(tmp_path)
    orch = sim.orchestrator
    orch.close()
    assert len(orch.completed_jobs) + len(orch.quarantined_jobs) == 3
    assert not orch.active_jobs
    assert orch.stats.decisions >= 6           # 3 job × 2 공정
    assert orch.stats.replanning_events == 0
    assert not [f for f in faults if isinstance(f, dict) and f.get("code") == 2014]
    assert orch.stats.log_path and orch.stats.log_path.is_file()
    rows = [l for l in orch.stats.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == orch.stats.decisions
    import json
    first = json.loads(rows[0])
    for k in ("job_id", "task_id", "candidates", "chosen", "excluded", "cost_components", "sim_time"):
        assert k in first, k
    assert first["chosen"] in ("WELD_01",)
    # 첫 결정은 투입(submit) 시점에 즉시 내려진다 — 원본과 같은 순서. 세 job 모두 결정 기록이 있다.
    assert {json.loads(r)["job_id"] for r in rows} == {"JOB_0", "JOB_1", "JOB_2"}
    assert all(json.loads(r)["chosen_by"] != "none" for r in rows)


def test_router_orchestrator_emits_2014_when_no_capable_cell(tmp_path):
    """기계가공 셀이 없는데 machining 을 요구하면 2014 를 내고 Job 은 활성으로 남긴다. 죽지 않는다."""
    sim, faults = _build(tmp_path, route=("machining", "inspection"))
    orch = sim.orchestrator
    orch.close()
    codes = [f for f in faults if isinstance(f, dict) and f.get("code") == 2014]
    assert codes, "2014 통지가 없다"
    assert orch.stats.replanning_events == 3          # job 마다 1회 (반복 억제)
    assert "2014-a" in codes[0]["reason"]
    assert len(orch.active_jobs) == 3 and not orch.completed_jobs


# ── 다중 셀: 같은 틱에 들어온 오더를 두 셀에 나눠 배정한다 (2026-10-19 수정 회귀) ───────────

def _build_two_welders(tmp_path, n_jobs=4):
    import random
    import fdw_sim.simulation.manager as manager
    from fdw_router.integrations.fdwops import RouterOrchestrator

    random.seed(0)
    bus = InMemoryBus()
    original = manager.FDWOrchestrator
    manager.FDWOrchestrator = RouterOrchestrator
    try:
        sim = SimulationManager(
            SimulationConfig(mode="discrete", physics_hz=60.0, max_sim_time_sec=600.0, log_dir=tmp_path / "kpi"),
            bus=bus,
            orchestrator_config=OrchestratorConfig(material_cell_id="MAT_01",
                                                   process_to_cell={"welding": "WELD_01", "inspection": "INSP_01"}),
        )
    finally:
        manager.FDWOrchestrator = original
    material = MaterialCell(CellConfig(cell_id="MAT_01", cell_type=CellType.MATERIAL, input_capacity=8,
                                       output_capacity=8), bus=bus, num_amrs=2)
    sim.register_material_cell(material, location=(0.0, 0.0))
    for cid, loc in (("WELD_01", (3.0, 1.5)), ("WELD_02", (3.0, -1.5))):
        w = WeldingCell(CellConfig(cell_id=cid, cell_type=CellType.WELDING), bus=bus, default_cycle_time=20.0)
        w.gap_agent.predict_gap_mm = lambda _p: 0.2
        sim.register_cell(w, location=loc)
    sim.register_cell(InspectionCell(CellConfig(cell_id="INSP_01", cell_type=CellType.INSPECTION), bus=bus,
                                     default_cycle_time=2.0), location=(6.0, 0.0))
    sim.start()
    for i in range(n_jobs):      # 같은 틱에 전부 투입
        sim.submit_job(JobSpec(job_id=f"JOB_{i}", part_id=f"PART_{i}", part_type="frame",
                               process_route=["welding", "inspection"],
                               recipe_overrides={"welding": {"speed": 0.10, "power": 1500.0, "path_length_mm": 2000.0}}))
    sim.run_until_done(all_jobs_done=True, extra_idle_sec=1.0)
    sim.stop()
    return sim


def test_router_spreads_same_tick_orders_over_two_welders(tmp_path):
    """오더 4건이 같은 틱에 오면 라우터 자신의 미도착 배정이 Wait 에 더해져 두 용접 셀에 번갈아 배정된다."""
    sim = _build_two_welders(tmp_path)
    orch = sim.orchestrator
    orch.close()
    chosen = [d.chosen for d in orch.decisions if d.process == "welding"]
    assert len(chosen) == 4
    assert set(chosen) == {"WELD_01", "WELD_02"}, chosen
    assert chosen.count("WELD_01") == 2 and chosen.count("WELD_02") == 2, chosen
    assert len(orch.completed_jobs) == 4
    # 결정 로그에는 후보 전원의 비용 성분이 남는다
    first = [d for d in orch.decisions if d.process == "welding"][0]
    assert set(first.cost_components) == {"WELD_01", "WELD_02"}
