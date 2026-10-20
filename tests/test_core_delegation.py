# -*- coding: utf-8 -*-
"""위임 판정 — 2014 발행 조건 3종 (규격 v2.2 §3.2c) 과 그 밖의 경우."""
from __future__ import annotations

from fdw_router.core import delegation
from fdw_router.core.delegation import Reason
from fdw_router.core.model import Cell, CellStatus, Job, Operation, ShopState


def weld_cell(cid):
    return Cell(cid, "S2", "welding")


def weld_op():
    return Operation("o", "weld_pass", "welding_arc")


def job_of(op):
    return Job("J1", "P", {op.op_id: op}, due_s=100.0)


def shop_with(*cells):
    s = ShopState()
    for c in cells:
        s.add_cell(c)
    return s


def test_no_capable_cell():
    """ⓐ 공정 자체가 막혔다."""
    op = weld_op()
    v = delegation.judge(job_of(op), op, shop_with(Cell("m", "S5", "machining")), task_id="J1/o")
    assert v is not None and v.reason is Reason.NO_CAPABLE_CELL
    assert v.event_code == 2014
    assert v.as_event_detail()["task_id"] == "J1/o"


def test_unassignable_step_type_is_no_capable_cell():
    op = Operation("o", "cool", "cooling")
    v = delegation.judge(job_of(op), op, shop_with(weld_cell("w")), task_id="J1/o")
    assert v.reason is Reason.NO_CAPABLE_CELL


def test_busy_cells_are_not_delegation():
    """자격 셀이 전부 busy/fault 여도 위임하지 않는다 — 라우터가 든다."""
    op = weld_op()
    shop = shop_with(weld_cell("w1"), weld_cell("w2"))
    shop.status["w1"] = CellStatus("w1", state="error")
    shop.status["w2"] = CellStatus("w2", state="running", running_op_ids=("x",))
    assert delegation.judge(job_of(op), op, shop, task_id="J1/o") is None
    assert delegation.is_waiting(job_of(op), op, shop)


def test_candidates_exhausted_only_when_all_capable_rejected():
    """ⓑ 자격 셀 전부가 거절해야 소진이다. 하나라도 남으면 아니다."""
    op = weld_op()
    shop = shop_with(weld_cell("w1"), weld_cell("w2"))
    assert delegation.judge(job_of(op), op, shop, task_id="J1/o", rejected_cells={"w1"}) is None
    v = delegation.judge(job_of(op), op, shop, task_id="J1/o", rejected_cells={"w1", "w2"})
    assert v.reason is Reason.CANDIDATES_EXHAUSTED
    assert v.detail["candidates"] == ["w1", "w2"]


def test_reassign_limit_exceeded():
    """ⓒ 한도 안이면 None, 넘으면 위임."""
    op = weld_op()
    shop = shop_with(weld_cell("w1"))
    assert delegation.judge(job_of(op), op, shop, task_id="J1/o", reassign_count=3, reassign_limit=3) is None
    v = delegation.judge(job_of(op), op, shop, task_id="J1/o", reassign_count=4, reassign_limit=3)
    assert v.reason is Reason.REASSIGN_LIMIT_EXCEEDED
    assert v.detail["reassign_limit"] == 3


def test_condition_order_a_before_b_before_c():
    op = weld_op()
    shop = shop_with(weld_cell("w1"))
    v = delegation.judge(job_of(op), op, shop, task_id="t", rejected_cells={"w1"}, reassign_count=99)
    assert v.reason is Reason.CANDIDATES_EXHAUSTED
    v = delegation.judge(job_of(op), op, shop_with(), task_id="t", rejected_cells={"w1"}, reassign_count=99)
    assert v.reason is Reason.NO_CAPABLE_CELL


def test_reason_text_is_spec_wording():
    for r in Reason:
        assert r in delegation.REASON_TEXT
