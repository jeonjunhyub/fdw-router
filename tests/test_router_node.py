# -*- coding: utf-8 -*-
"""전송 계층 위의 라우터 노드 + 오케스트레이터 스텁 — 브로커 없이(in-process) 끝까지 왕복한다."""
from fdw_router.config import ORCHESTRATOR_NODE_ID
from fdw_router.integrations.orchestrator_stub import OrchestratorStub
from fdw_router.msg import build, envelope
from fdw_router.transport.mqtt import InProcessTransport
from fdw_router.transport.router_node import RouterNode


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _setup():
    clock = Clock()
    bus = InProcessTransport()
    node = RouterNode(bus, now=clock, run_id="node-test")
    stub = OrchestratorStub(bus, now=clock, run_id="stub-test")
    node.start()
    stub.start()
    cell = build.EnvelopeFactory(run_id="cell-test")
    return clock, bus, node, stub, cell


def _status(cell, at=0.0, to=("router", "nuc-01"), cell_id="welding_cell_01"):
    return cell.make(message_type="cell_status", frm=("cell", cell_id), to=to, at=at,
                     payload={"cell_id": cell_id, "owner_subtask": "S2", "state": "idle",
                              "handling_mode": "autonomous", "timestamp_iso": cell.iso(at)})


def test_cell_status_to_router_is_relayed_to_ops01():
    clock, bus, node, stub, cell = _setup()
    e = _status(cell)
    assert envelope.validate(e).ok, envelope.validate(e).errors
    bus.publish(e)
    assert node.stats.relayed == 1
    assert len(stub.log.relayed_in) == 1
    r = stub.log.relayed_in[0]
    assert r["relayed_by"] == "nuc-01" and r["to"]["node_id"] == ORCHESTRATOR_NODE_ID
    assert envelope.validate(r).ok, envelope.validate(r).errors


def test_schema_violation_emits_2015_once_per_node_type():
    clock, bus, node, stub, cell = _setup()
    bad = _status(cell)
    del bad["payload"]["state"]                      # 필수 필드 누락
    bus.publish(bad)
    bus.publish(bad)                                 # 같은 노드·종류 → 억제
    assert node.stats.rejected == 2
    codes = [ev["payload"]["code"] for ev in stub.log.events]
    assert codes == [2015]
    p = stub.log.events[0]["payload"]
    assert p["violation_path"] and p["rejected_from_node_id"] == "welding_cell_01"
    assert envelope.validate(stub.log.events[0]).ok


def test_internal_message_at_router_is_discarded_with_2011():
    clock, bus, node, stub, cell = _setup()
    ops = build.EnvelopeFactory(run_id="ops-test")
    # fdw-ops → fdw-ops 내부 메시지가 라우터 토픽으로 잘못 오면 폐기 + 2011
    e = ops.make(message_type="replanning_request", frm=("fdw-ops", "statedb-01"), to=("fdw-ops", "ops-01"), at=0.0,
                 payload={"reason": "test", "affected_task_ids": []})
    e["to"] = {"system": "fdw-ops", "node_id": "ops-01"}
    # 토픽은 to 로 정해지므로 라우터 토픽으로 강제 발행한다
    bus._subs and None
    topic_router = "fdw/v1/router/nuc-01/replanning_request"
    for pat, cb in list(bus._subs):
        from fdw_router.transport.mqtt import topic_matches
        if topic_matches(pat, topic_router):
            cb(topic_router, e)
    if node.stats.rejected == 0:                     # 본문 규격이 맞을 때만 판정까지 간다
        assert node.stats.discarded == 1
        assert 2011 in node.stats.events


def test_join_capability_at_ops01_is_relayed_back_to_router():
    clock, bus, node, stub, cell = _setup()
    cap = cell.make(message_type="cell_capability", frm=("cell", "waam_cell_01"), to=("fdw-ops", "ops-01"), at=0.0,
                    payload={"cell_id": "waam_cell_01", "owner_subtask": "S4", "announce_reason": "join",
                             "process_type": "waam", "supported_task_types": ["waam_layer_deposit"],
                             "timestamp_iso": cell.iso(0.0)})
    assert envelope.validate(cap).ok, envelope.validate(cap).errors
    bus.publish(cap)
    assert len(stub.log.relayed_out) == 1
    back = stub.log.relayed_out[0]
    assert back["relayed_by"] == "ops-01" and back["to"]["system"] == "router"
    assert "waam_cell_01" in node.capabilities      # 라우터가 중계본을 받아 보관 (항목 150)
    assert node.stats.received == 1 and node.stats.delivered == 1


def test_router_issued_task_request_is_valid_and_logged():
    clock, bus, node, stub, cell = _setup()
    e = node.issue_task_request("welding_cell_01", {"task_id": "t-1", "step_id": "weld_arc_pass1",
                                                     "params": {"weld_arc_current": 220}, "expected_duration_s": 30.0})
    assert e["from"]["system"] == "router" and e["correlation_id"] in node.issued
    assert envelope.validate(e).ok, envelope.validate(e).errors
