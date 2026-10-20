# -*- coding: utf-8 -*-
"""봉투 ↔ 도메인 결합 — 라우터 노드 위에서 J축이 지시를 내고 응답을 받아 작업을 끝낸다 (브로커 없음)."""
from fdw_router.core.adapt import JAxisBinding, cell_from_capability, due_seconds, jobs_from_production_order
from fdw_router.core.model import Operation, ShopState
from fdw_router.integrations.orchestrator_stub import OrchestratorStub
from fdw_router.msg import build, envelope
from fdw_router.transport.mqtt import InProcessTransport
from fdw_router.transport.router_node import RouterNode


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


PLANS = {
    "PBV-BRACKET-A": (
        Operation("w", "weld_arc_pass1", "welding_arc", params={"weld_arc_current": 220}, expected_duration_s=30.0),
        Operation("m", "cnc_op_1", "machining", requires=("w",), expected_duration_s=20.0),
    ),
    "NO-CELL-PRODUCT": (Operation("f", "bend_execute", "forming", expected_duration_s=10.0),),
}


class AutoCell:
    """셀 흉내: task_request 를 받으면 즉시 accept, clock 이 흐르면 complete 를 보낸다."""

    def __init__(self, bus, cell_id, clock):
        self.bus, self.cell_id, self.clock = bus, cell_id, clock
        self.f = build.EnvelopeFactory(run_id=f"cell-{cell_id}")
        self.inbox = []
        bus.subscribe(f"fdw/v1/cell/{cell_id}/#", self._on)

    def _on(self, topic, e):
        self.inbox.append(e)
        tid = e["payload"]["task_id"]
        self.bus.publish(self.f.make(message_type="task_accept", frm=("cell", self.cell_id),
                                     to=(e["from"]["system"], e["from"]["node_id"]), at=self.clock(),
                                     correlation_id=e.get("correlation_id"),
                                     payload={"task_id": tid, "accepted": True}))

    def complete_all(self):
        for e in list(self.inbox):
            self.inbox.remove(e)
            self.bus.publish(self.f.make(message_type="task_complete", frm=("cell", self.cell_id),
                                         to=(e["from"]["system"], e["from"]["node_id"]), at=self.clock(),
                                         correlation_id=e.get("correlation_id"),
                                         payload={"task_id": e["payload"]["task_id"],
                                                  "kpi_summary": {"actual_duration_s": 1.0},
                                                  "output_artifact_id": f"ART-{self.cell_id}"}))

    def announce(self, process_type, owner="S2", step_types=()):
        self.bus.publish(self.f.make(message_type="cell_capability", frm=("cell", self.cell_id),
                                     to=("router", "nuc-01"), at=self.clock(),
                                     payload={"cell_id": self.cell_id, "owner_subtask": owner, "announce_reason": "join",
                                              "process_type": process_type, "supported_task_types": list(step_types),
                                              "timestamp_iso": self.f.iso(self.clock()),
                                              "capacity": {"max_concurrent_tasks": 1, "nominal_cycle_time_s": 30}}))
        self.bus.publish(self.f.make(message_type="cell_status", frm=("cell", self.cell_id), to=("router", "nuc-01"),
                                     at=self.clock(), payload={"cell_id": self.cell_id, "owner_subtask": owner,
                                                               "state": "idle", "handling_mode": "autonomous",
                                                               "timestamp_iso": self.f.iso(self.clock())}))


def _world():
    clock = Clock()
    bus = InProcessTransport()
    node = RouterNode(bus, now=clock, run_id="adapt")
    stub = OrchestratorStub(bus, now=clock, run_id="adapt-stub")
    node.start(); stub.start()
    binding = JAxisBinding(node=node, shop=ShopState(), now=clock, plans=PLANS)
    weld = AutoCell(bus, "welding_cell_01", clock)
    mach = AutoCell(bus, "machining_cell_01", clock)
    weld.announce("welding", "S2", ("weld_arc_pass1",))
    mach.announce("machining", "S5", ("cnc_op_1",))
    return clock, bus, node, stub, binding, weld, mach


def test_capability_and_due_parsing():
    c = cell_from_capability({"cell_id": "x", "owner_subtask": "S4", "process_type": "waam",
                              "supported_task_types": ["a"], "capacity": {"max_concurrent_tasks": 2},
                              "process_limits": [{"param_name": "waam_current", "min": 46, "max": 270}]})
    assert c.max_concurrent_tasks == 2 and c.limit_for("waam_current").max == 270
    assert due_seconds("2026-05-31T10:00:00+09:00") == 3600.0
    assert due_seconds("2026-06-01") > 0


def test_order_expands_to_jobs_with_weight():
    jobs = jobs_from_production_order({"order_id": "PO-1", "product_id": "PBV-BRACKET-A", "quantity": 2,
                                       "due_date": "2026-06-01", "order_priority": "rush"}, PLANS, released_s=0.0)
    assert [j.job_id for j in jobs] == ["PO-1#1", "PO-1#2"] and jobs[0].weight >= 2.0


def test_end_to_end_order_to_completion_over_envelopes():
    clock, bus, node, stub, binding, weld, mach = _world()
    assert set(binding.shop.cells) == {"welding_cell_01", "machining_cell_01"}
    binding.add_order({"order_id": "PO-1", "product_id": "PBV-BRACKET-A", "quantity": 1, "due_date": "2026-06-01"})
    # 지시 1건 발행 → 셀이 accept → 완료 → 다음 공정 발행 → 완료
    reqs = [e for t, e in bus.published if e["message_type"] == "task_request"]
    assert len(reqs) == 1 and reqs[0]["from"]["system"] == "router" and envelope.validate(reqs[0]).ok, \
        envelope.validate(reqs[0]).errors
    clock.t = 31.0; weld.complete_all()
    reqs = [e for t, e in bus.published if e["message_type"] == "task_request"]
    assert len(reqs) == 2 and reqs[1]["to"]["node_id"] == "machining_cell_01"
    clock.t = 52.0; mach.complete_all()
    assert binding.dispatcher.all_done
    assert len(binding.dispatcher.log.records) == 2
    assert all(envelope.validate(e).ok for t, e in bus.published), "모든 봉투가 규격을 지켜야 한다"


def test_no_capable_cell_emits_2014_to_ops01():
    clock, bus, node, stub, binding, weld, mach = _world()
    binding.add_order({"order_id": "PO-2", "product_id": "NO-CELL-PRODUCT", "quantity": 1, "due_date": "2026-06-01"})
    codes = [e["payload"]["code"] for e in stub.log.events]
    assert codes == [2014], (codes, node.log[-3:])
    ev = stub.log.events[0]
    assert envelope.validate(ev).ok, envelope.validate(ev).errors
    import json
    assert json.loads(ev["payload"]["detail"])["reason"] == "no_capable_cell"   # detail 은 문자열 (event_log 스키마)
