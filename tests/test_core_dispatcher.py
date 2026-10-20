# -*- coding: utf-8 -*-
"""디스패처 — 이벤트 트리거 재평가 · 타임아웃 2종 · 거절/위임 경로 · S1 완주."""
from __future__ import annotations

import json

from fdw_router.core.decision_log import DecisionLog, read_jsonl
from fdw_router.core.delegation import Reason
from fdw_router.core.dispatcher import Dispatcher, task_id_of
from fdw_router.core.model import Cell, Job, Operation, ShopState
from fdw_router.sim import s1_smoke


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


def weld_cell(cid, cycle=60.0):
    return Cell(cid, "S2", "welding", nominal_cycle_time_s=cycle)


def weld_job(job_id="J1", due=1000.0, weight=1.0, n=1):
    ops = {}
    prev = None
    for i in range(n):
        oid = f"o{i}"
        ops[oid] = Operation(oid, "weld_pass", "welding_arc", requires=(prev,) if prev else (),
                             expected_duration_s=60.0)
        prev = oid
    return Job(job_id, "P", ops, due_s=due, weight=weight)


def make(cells, **kw):
    clock = FakeClock()
    shop = ShopState()
    for c in cells:
        shop.add_cell(c)
    issued, verdicts = [], []
    d = Dispatcher(shop, now=clock.now, on_assign=issued.append, on_delegate=verdicts.append, **kw)
    return clock, shop, d, issued, verdicts


# ── S1 완주 ─────────────────────────────────────────────────────────


def test_s1_completes_all_jobs_without_precedence_violation(tmp_path):
    log = tmp_path / "s1.jsonl"
    r = s1_smoke.run_s1(log)
    assert r.all_done, r.completed_jobs
    assert r.delegations == 0
    assert s1_smoke.check_precedence(r.trace, s1_smoke.s1_jobs()) == []
    rows = read_jsonl(log)
    assert len(rows) == r.decisions == 6          # 작업 6개 = 결정 6건
    assert all(row["outcome"] == "assigned" for row in rows)
    assert all(row["chosen_cell"] and row["cost"] and row["atcs"] for row in rows)


def test_s1_is_deterministic(tmp_path):
    a = s1_smoke.run_s1(tmp_path / "a.jsonl")
    b = s1_smoke.run_s1(tmp_path / "b.jsonl")
    assert a.trace == b.trace
    assert a.log_lines == b.log_lines


def test_s1_rush_job_is_served_first():
    """긴급오더(J3, w=4, 납기 150 s)가 용접 셀을 먼저 받는다."""
    r = s1_smoke.run_s1()
    first_assigns = [w for t, k, w in r.trace if k == "assign"][:3]
    assert first_assigns[0] == "J3/w@welding_cell_01"      # 긴급이 가장 빠른 용접 셀을 먼저
    assert any(w.startswith("J1/w@") for w in first_assigns)


# ── 트리거와 예약 ───────────────────────────────────────────────────


def test_new_order_assigns_and_reserves_cell():
    clock, shop, d, issued, _ = make([weld_cell("w1")])
    d.add_job(weld_job())
    assert len(issued) == 1 and issued[0].cell_id == "w1"
    st = shop.status_of("w1")
    assert st.running_op_ids == (task_id_of("J1", "o0"),) and st.state == "running"
    # 같은 셀에 두 번째 작업은 capacity 때문에 안 나간다
    d.add_job(weld_job("J2"))
    assert len(issued) == 1


def test_completion_advances_dag_and_triggers_next():
    clock, shop, d, issued, _ = make([weld_cell("w1")])
    d.add_job(weld_job(n=2))
    assert [a.op_id for a in issued] == ["o0"]
    d.on_task_accepted(issued[0].task_id)
    clock.t = 60.0
    d.on_task_completed(issued[0].task_id)
    assert [a.op_id for a in issued] == ["o0", "o1"]
    assert shop.last_cell["J1"] == "w1" and shop.last_step["w1"] == "weld_pass"
    assert d.all_done is False
    d.on_task_completed(issued[1].task_id)
    assert d.all_done


def test_reject_reassigns_to_other_cell_then_exhausts():
    clock, shop, d, issued, verdicts = make([weld_cell("w1"), weld_cell("w2", cycle=90.0)])
    d.add_job(weld_job())
    first = issued[0]
    d.on_task_rejected(first.task_id, first.cell_id)
    assert len(issued) == 2 and issued[1].cell_id != first.cell_id
    d.on_task_rejected(issued[1].task_id, issued[1].cell_id)
    assert len(issued) == 2
    assert len(verdicts) == 1 and verdicts[0].reason is Reason.CANDIDATES_EXHAUSTED
    assert d.delegated == {first.task_id}
    # 위임한 작업은 셀이 비어도 다시 내지 않는다
    d.on_cell_state("w1", "idle")
    assert len(issued) == 2


def test_accept_timeout_reassigns_then_delegates():
    clock, shop, d, issued, verdicts = make([weld_cell("w1")], accept_timeout_s=5.0, reassign_limit=1)
    d.add_job(weld_job())
    assert len(issued) == 1
    clock.t = 6.0
    d.tick()                      # 수락 안 옴 → 재배정 (attempt 2)
    assert len(issued) == 2 and issued[1].attempt == 2
    assert d.timeouts[0][1] == "accept"
    clock.t = 12.0
    d.tick()                      # 또 안 옴 → 한도(1) 초과 → 2014 ⓒ
    assert len(issued) == 2
    assert verdicts and verdicts[0].reason is Reason.REASSIGN_LIMIT_EXCEEDED


def test_complete_timeout_uses_factor_of_expected_duration():
    clock, shop, d, issued, verdicts = make([weld_cell("w1")], complete_timeout_factor=1.5, reassign_limit=5)
    d.add_job(weld_job())
    d.on_task_accepted(issued[0].task_id)
    clock.t = 80.0                # 60 × 1.5 = 90 보다 작다 → 아직
    d.tick()
    assert len(issued) == 1
    clock.t = 91.0
    d.tick()
    assert len(issued) == 2 and d.timeouts[-1][1] == "complete"


def test_cell_fault_moves_task_to_other_cell():
    clock, shop, d, issued, _ = make([weld_cell("w1"), weld_cell("w2", cycle=70.0)])
    d.add_job(weld_job())
    assert issued[0].cell_id == "w1"
    d.on_cell_state("w1", "error")
    assert len(issued) == 2 and issued[1].cell_id == "w2"
    assert shop.status_of("w1").running_op_ids == ()


def test_idle_transition_triggers_waiting_work():
    clock, shop, d, issued, _ = make([weld_cell("w1")])
    shop.status_of("w1").state = "maintenance"
    d.add_job(weld_job())
    assert issued == []           # 기다린다, 위임하지 않는다
    assert not d.verdicts
    d.on_cell_state("w1", "idle")
    assert len(issued) == 1


def test_no_capable_cell_delegates_immediately():
    clock, shop, d, issued, verdicts = make([Cell("m1", "S5", "machining")])
    d.add_job(weld_job())
    assert issued == [] and verdicts[0].reason is Reason.NO_CAPABLE_CELL


def test_decision_log_rows_carry_components(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    clock, shop, d, issued, _ = make([weld_cell("w1"), weld_cell("w2")], log=log)
    d.add_job(weld_job(weight=3.0))
    row = json.loads(log.lines()[0])
    assert row["task_id"] == "J1/o0" and row["chosen_cell"] in ("w1", "w2")
    assert row["candidates"] == ["w1", "w2"]
    assert row["atcs"][0]["w"] == 3.0
    assert {"wait_s", "p_jc", "transport_s", "setup_s", "imbalance_s"} <= set(row["cost"][0])
    assert row["decided_at_s"] == 0.0 and row["duration_ms"] >= 0.0
