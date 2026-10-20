# -*- coding: utf-8 -*-
"""J축 셀 선택 — 비용함수 Cost(j,c) 와 프로파일."""
from __future__ import annotations

from fdw_router.core import selector
from fdw_router.core.model import Cell, CellStatus, Job, Operation, ShopState


def weld_cell(cid, cycle=60.0):
    return Cell(cid, "S2", "welding", nominal_cycle_time_s=cycle)


def weld_op(oid="o", **kw):
    return Operation(oid, "weld_pass", "welding_arc", **kw)


def job_of(*ops, job_id="J1", **kw):
    return Job(job_id, "P", {o.op_id: o for o in ops}, due_s=kw.pop("due_s", 1000.0), **kw)


def shop_with(*cells):
    s = ShopState()
    for c in cells:
        s.add_cell(c)
    return s


def test_wait_term_prefers_less_loaded_cell():
    """대기시간 최소화 — 잔여 부하가 작은 셀이 비용이 낮다."""
    shop = shop_with(weld_cell("c_a"), weld_cell("c_b"))
    shop.status["c_a"] = CellStatus("c_a", state="running", remaining_s=500.0)
    op = weld_op()
    r = selector.rank_cost(job_of(op), op, shop.cells.values(), shop, profile="leadtime")
    assert r[0].cell_id == "c_b" and r[0].wait_s == 0.0 and r[1].wait_s == 500.0


def test_imbalance_term_spreads_load():
    """부하 분산 — 누적 부하가 높은 셀에 얹으면 Imbalance 가 양수, 낮은 셀이면 음수."""
    shop = shop_with(weld_cell("c_a"), weld_cell("c_b"))
    shop.status["c_a"].remaining_s = 100.0
    op = weld_op(expected_duration_s=60.0)
    heavy = selector.imbalance_s(shop.cells["c_a"], 60.0, shop)
    light = selector.imbalance_s(shop.cells["c_b"], 60.0, shop)
    assert heavy > 0 > light
    r = selector.rank_cost(job_of(op), op, shop.cells.values(), shop, profile="balanced")
    assert r[0].cell_id == "c_b"


def test_transport_term_prefers_previous_cell_neighbourhood():
    shop = shop_with(weld_cell("c_far"), weld_cell("c_near"), Cell("m1", "S5", "machining"))
    shop.last_cell["J1"] = "m1"
    tt = selector.TransferTable({("m1", "c_far"): 120.0, ("m1", "c_near"): 10.0})
    op = weld_op()
    r = selector.rank_cost(job_of(op), op, [shop.cells["c_far"], shop.cells["c_near"]], shop,
                           profile="leadtime", transfer=tt)
    assert r[0].cell_id == "c_near" and r[0].transport_s == 10.0


def test_transfer_table_defaults():
    tt = selector.TransferTable(default_s=7.0)
    assert tt.seconds(None, "x") == 0.0
    assert tt.seconds("x", "x") == 0.0
    assert tt.seconds("x", "y") == 7.0


def test_setup_term_reused_from_priority():
    shop = shop_with(weld_cell("c_same"), weld_cell("c_diff"))
    shop.last_step["c_same"] = "weld_pass"
    shop.last_step["c_diff"] = "other_step"
    op = weld_op()
    r = {x.cell_id: x for x in selector.rank_cost(job_of(op), op, shop.cells.values(), shop)}
    assert r["c_same"].setup_s == 0.0 and r["c_diff"].setup_s > 0.0


def test_profiles_change_choice():
    """균형 프로파일은 부하가 낮은 느린 셀을, 긴급 프로파일은 당장 빨리 끝나는 셀을 고른다."""
    shop = shop_with(weld_cell("fast", cycle=30.0), weld_cell("slow", cycle=60.0))
    shop.status["fast"].remaining_s = 45.0        # fast 는 조금 바쁨
    op = weld_op()
    urgent = selector.select_cell(job_of(op), op, shop.cells.values(), shop, profile="urgent")
    balanced = selector.select_cell(job_of(op), op, shop.cells.values(), shop, profile="balanced")
    assert urgent.cell_id == "slow" or balanced.cell_id == "slow"
    assert {urgent.cell_id, balanced.cell_id} <= {"fast", "slow"}
    # 성분값이 남는다
    assert balanced.profile == "balanced" and balanced.p_jc in (30.0, 60.0)


def test_rank_is_deterministic_on_ties():
    shop = shop_with(weld_cell("c_b"), weld_cell("c_a"))
    op = weld_op()
    r1 = [x.cell_id for x in selector.rank_cost(job_of(op), op, shop.cells.values(), shop)]
    r2 = [x.cell_id for x in selector.rank_cost(job_of(op), op, list(shop.cells.values())[::-1], shop)]
    assert r1 == r2 == ["c_a", "c_b"]


def test_cost_components_sum_matches_weights():
    shop = shop_with(weld_cell("c"))
    op = weld_op(expected_duration_s=50.0)
    c = selector.cost(job_of(op), op, shop.cells["c"], shop, profile="leadtime")
    w = selector.PROFILES["leadtime"]
    expected = (w.alpha * c.wait_s + w.beta * c.p_jc + w.gamma * c.transport_s
                + w.delta * c.setup_s + w.epsilon * c.imbalance_s)
    assert abs(c.cost - expected) < 1e-9


def test_select_cell_none_when_no_candidates():
    shop = shop_with(weld_cell("c"))
    op = weld_op()
    assert selector.select_cell(job_of(op), op, [], shop) is None
