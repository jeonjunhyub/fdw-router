# -*- coding: utf-8 -*-
"""DES 셀 런타임이 내는 봉투가 규격을 지키는지.

이 시험이 통과해야 "Isaac Sim 이 있다고 가정하고 만든다"가 성립한다.
DES 가 규격 봉투를 내면, 나중에 Isaac 이 같은 봉투를 내도록 만들기만 하면 되고
라우터는 손대지 않는다.
"""
from __future__ import annotations

import pytest

from fdw_router.adapters.des_cell import CellSpec, DesCellRuntime
from fdw_router.adapters.isaac_cell import IsaacCellRuntime, IsaacNotAvailable
from fdw_router.msg import envelope, rules
from fdw_router.ports.cell_runtime import CellRuntime

CELLS = [
    CellSpec("welding_cell_01", "S2", "welding", nominal_cycle_time_s=30.0),
    CellSpec("machining_cell_01", "S5", "machining", nominal_cycle_time_s=45.0),
]


def _run(seconds=120.0, **kw):
    rt = DesCellRuntime(list(CELLS), **kw)
    out = []
    t = 0.0
    while t < seconds:
        t += 10.0
        rt.step(until=t)
        out.extend(rt.poll())
    return rt, out


def test_des_satisfies_port():
    rt = DesCellRuntime(list(CELLS))
    assert isinstance(rt, CellRuntime), "DES 런타임이 CellRuntime 포트를 만족하지 않습니다"


def test_isaac_satisfies_same_port():
    """Isaac 어댑터는 구현이 없어도 **같은 포트**를 만족해야 한다."""
    rt = IsaacCellRuntime()
    assert isinstance(rt, CellRuntime), "Isaac 어댑터가 포트를 벗어났습니다"


def test_isaac_fails_loudly_not_silently():
    """미도입 상태에서 조용히 빈 값을 돌려주면 안 된다 — 명확히 실패해야 한다."""
    rt = IsaacCellRuntime()
    with pytest.raises(IsaacNotAvailable):
        rt.send({})
    with pytest.raises(IsaacNotAvailable):
        list(rt.poll())


def test_every_emitted_envelope_is_spec_valid():
    _, out = _run()
    assert out, "DES 가 봉투를 하나도 내지 않았습니다"
    bad = []
    for e in out:
        res = envelope.validate(e)
        if not res.ok:
            bad.append((e["message_type"], res.errors[:2]))
    assert not bad, f"규격을 어긴 봉투 {len(bad)}건: {bad[:5]}"


def test_task_lifecycle():
    """지시 → 수락 → 완료가 봉투로 관측된다."""
    rt = DesCellRuntime(list(CELLS), run_id="lifecycle")
    task = rt.factory.make(
        message_type="task_request",
        frm=("fdw-ops", "ops-01"),
        to=("cell", "welding_cell_01"),
        payload={
            "task_id": rt.factory.task_id("t1"),
            "expected_duration_s": 20.0,
            "step_id": "weld_arc_pass1",
            "params": {"weld_arc_current": 200},
        },
        at=0.0,
        correlation_id=rt.factory.correlation_id("t1"),
    )
    assert envelope.validate(task).ok, envelope.validate(task).errors

    rt.send(task)
    seen, t = [], 0.0
    while t < 60.0:
        t += 5.0
        rt.step(until=t)
        seen.extend(m["message_type"] for m in rt.poll())

    assert "task_accept" in seen
    assert "task_complete" in seen


def test_determinism_same_seed_same_envelopes():
    """동일 시드 → 동일 봉투. 재현성이 KPI 주장의 근거다."""
    _, a = _run(run_id="det", seed=7)
    _, b = _run(run_id="det", seed=7)
    assert [e["envelope_id"] for e in a] == [e["envelope_id"] for e in b]
    assert [e["timestamp_iso"] for e in a] == [e["timestamp_iso"] for e in b]


def test_status_and_heartbeat_go_to_router():
    """`cell_status`·`heartbeat` 는 라우팅 계층으로 간다 (방향표 5행·20행)."""
    _, out = _run()
    for mt in ("cell_status", "heartbeat"):
        msgs = [e for e in out if e["message_type"] == mt]
        assert msgs, f"{mt} 봉투가 없습니다"
        assert all(e["to"]["system"] == "router" for e in msgs)


def test_router_relays_cell_status_to_orchestrator():
    """DES 가 낸 cell_status 를 라우터가 규격대로 중계하는지 — 포트 연결 확인."""
    _, out = _run()
    status = next(e for e in out if e["message_type"] == "cell_status")
    d = rules.evaluate(status)
    assert d.relay, "cell_status 가 중계되지 않았습니다"
    assert d.relay_to == {"system": "fdw-ops", "node_id": "ops-01"}
    assert d.relayed_envelope["relayed_by"] == "nuc-01"
    assert envelope.validate(d.relayed_envelope).ok, "중계본이 규격을 어겼습니다"


def test_fault_injection_reports_failure():
    """S3·S7 전제 — 고장 주입 시 셀이 실패를 알린다."""
    cells = [CellSpec("welding_cell_01", "S2", "welding", nominal_cycle_time_s=60.0, fault_at=10.0)]
    rt = DesCellRuntime(cells, run_id="fault")
    rt.send(
        rt.factory.make(
            message_type="task_request",
            frm=("fdw-ops", "ops-01"),
            to=("cell", "welding_cell_01"),
            payload={"task_id": rt.factory.task_id("f1"), "expected_duration_s": 60.0,
                     "step_id": "weld_arc_pass1", "params": {"weld_arc_current": 200}},
            at=0.0,
        )
    )
    seen, t = [], 0.0
    while t < 120.0:
        t += 5.0
        rt.step(until=t)
        seen.extend(rt.poll())
    types = [e["message_type"] for e in seen]
    assert "task_failed" in types, "고장 뒤에도 실패 통지가 없습니다"
    assert "event" in types, "고장 이벤트가 발행되지 않았습니다"
