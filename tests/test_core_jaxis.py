# -*- coding: utf-8 -*-
"""J축 코어 — 도메인 모델 · 실행가능성 필터 · ATCS 지수."""
from __future__ import annotations

import math

import pytest

from fdw_router.config import PROCESS_TYPE_MAP
from fdw_router.core import priority
from fdw_router.core.feasibility import Reject, capability_exists, feasible_cells
from fdw_router.core.model import Cell, CellStatus, Job, Operation, ProcessLimit, ShopState


# ── 공통 픽스처 ─────────────────────────────────────────────────────


def weld_cell(cell_id="welding_cell_01", **kw):
    kw.setdefault("process_limits", (
        ProcessLimit("weld_arc_current", min=100.0, max=300.0, unit="A"),
    ))
    return Cell(cell_id=cell_id, owner_subtask="S2", process_type="welding", **kw)


def weld_op(op_id="op1", **kw):
    kw.setdefault("params", {"weld_arc_current": 200.0})
    return Operation(op_id=op_id, step_id="weld_arc_pass1",
                     process_type="welding_arc", **kw)


def shop_with(*cells, now_s=0.0):
    s = ShopState(now_s=now_s)
    for c in cells:
        s.add_cell(c)
    return s


def one_job(ops, due_s=1000.0, weight=1.0):
    return Job(job_id="J1", product_id="P1",
               operations={o.op_id: o for o in ops}, due_s=due_s, weight=weight)


# ── 구조: J축은 메시지 계층을 모른다 ────────────────────────────────


def test_core_does_not_import_msg():
    """J축 내부 모델을 메시지 계층과 분리한다.

    이것이 B1~B3 이 어떻게 확정되든 J축을 지키는 장치다.
    """
    import ast
    import pathlib

    core = pathlib.Path(__file__).resolve().parents[1] / "fdw_router" / "core"
    offenders = []
    for py in core.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = ""
            if isinstance(node, ast.ImportFrom):
                mod = ("." * node.level) + (node.module or "")
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if "msg" in mod.split(".") or "envelope" in mod:
                offenders.append(f"{py.name}: {mod}")
    assert not offenders, f"J축이 메시지 계층을 import 했습니다: {offenders}"


# ── 모델: DAG ───────────────────────────────────────────────────────


def test_dag_cycle_is_rejected():
    a = Operation("a", "s", "machining", requires=("b",))
    b = Operation("b", "s", "machining", requires=("a",))
    with pytest.raises(ValueError, match="순환"):
        one_job([a, b])


def test_missing_prerequisite_is_rejected():
    a = Operation("a", "s", "machining", requires=("nope",))
    with pytest.raises(ValueError, match="존재하지 않는"):
        one_job([a])


def test_ready_ops_follows_dag():
    a = Operation("a", "s1", "welding_arc")
    b = Operation("b", "s2", "machining", requires=("a",))
    c = Operation("c", "s3", "machining", requires=("a",))
    job = one_job([a, b, c])
    assert {o.op_id for o in job.ready_ops(set())} == {"a"}
    # 제품2 처럼 순서 무관 op 는 동시에 준비된다 (병렬 분기 활용)
    assert {o.op_id for o in job.ready_ops({"a"})} == {"b", "c"}
    assert job.ready_ops({"a", "b", "c"}) == ()


# ── 요청 6: process_type 두 어휘 ────────────────────────────────────


@pytest.mark.parametrize("seq_type,cell_type", [
    ("welding_arc", "welding"), ("welding_laser", "welding"),
    ("amr_transfer", "transport"), ("transfer", "transport"),
    ("handling", "transport"),          # v2.2 잠정 대응 (잠정)
    ("qa", "inspection"), ("machining", "machining"),
])
def test_process_type_mapping(seq_type, cell_type):
    assert Operation("o", "s", seq_type).cell_process_type == cell_type


@pytest.mark.parametrize("seq_type", ["cooling", "fixture", "exception", "control"])
def test_unmapped_types_are_not_assignable(seq_type):
    """셀 배정 대상이 아닌 4종(가이드라인 표 9) — 임의로 셀에 붙이지 않는다."""
    op = Operation("o", "s", seq_type)
    assert op.cell_process_type is None
    fs = feasible_cells(one_job([op]), op, shop_with(weld_cell()))
    assert fs.is_blocked
    assert fs.rejections[0].reason is Reject.NO_CELL_PROCESS_TYPE


def test_mapping_covers_every_sequence_enum_value():
    """규격 enum(v2.2: 14종)이 모두 대응표에 있어야 한다 — 누락되면 조용히 배정 실패한다."""
    from fdw_router import spec
    enum = set(
        spec.schemas()["process_sequence.schema.json"]
        ["properties"]["steps"]["items"]["properties"]["process_type"]["enum"]
    )
    assert enum <= set(PROCESS_TYPE_MAP), f"대응표 누락: {sorted(enum - set(PROCESS_TYPE_MAP))}"


def test_mapping_targets_are_valid_cell_types():
    """대응표의 도착지가 `cell_capability` enum 안에 있어야 한다."""
    from fdw_router import spec
    valid = set(spec.schemas()["cell_capability.schema.json"]
                ["properties"]["process_type"]["enum"])
    bad = {k: v for k, v in PROCESS_TYPE_MAP.items() if v is not None and v not in valid}
    assert not bad, f"셀 enum 에 없는 대응: {bad}"


# ── 실행가능성 필터: 하드 제약 전건 ─────────────────────────────────


def test_prerequisite_blocks_assignment():
    a = Operation("a", "s1", "welding_arc")
    b = Operation("b", "weld_arc_pass1", "welding_arc", requires=("a",))
    job = one_job([a, b])
    fs = feasible_cells(job, b, shop_with(weld_cell()))
    assert fs.is_blocked and fs.rejections[0].reason is Reject.PREREQ_NOT_DONE


def test_process_type_mismatch():
    op = weld_op()
    shop = shop_with(Cell("machining_cell_01", "S5", "machining"))
    fs = feasible_cells(one_job([op]), op, shop)
    assert fs.is_blocked
    assert fs.rejections[0].reason is Reject.PROCESS_TYPE_MISMATCH


def test_supported_task_types_filter():
    op = weld_op()
    shop = shop_with(weld_cell(supported_task_types=("weld_arc_pass2",)))
    fs = feasible_cells(one_job([op]), op, shop)
    assert fs.is_blocked
    assert fs.rejections[0].reason is Reject.TASK_TYPE_UNSUPPORTED


def test_process_limit_out_of_range():
    op = weld_op(params={"weld_arc_current": 999.0})
    fs = feasible_cells(one_job([op]), op, shop_with(weld_cell()))
    assert fs.is_blocked
    r = fs.rejections[0]
    assert r.reason is Reject.PARAM_OUT_OF_RANGE and "999" in r.detail


def test_unconstrained_param_passes():
    """셀이 제약을 공지하지 않은 파라미터는 막지 않는다."""
    op = weld_op(params={"weld_torch_angle": 45.0})
    assert feasible_cells(one_job([op]), op, shop_with(weld_cell()))


def test_allowed_values_limit():
    cell = weld_cell(process_limits=(
        ProcessLimit("shield_gas", allowed_values=("Ar", "CO2")),
    ))
    ok = weld_op(params={"shield_gas": "Ar"})
    ng = weld_op(params={"shield_gas": "He"})
    assert feasible_cells(one_job([ok]), ok, shop_with(cell))
    assert not feasible_cells(one_job([ng]), ng, shop_with(cell))


@pytest.mark.parametrize("state", ["paused", "blocked", "error", "maintenance", "offline"])
def test_non_assignable_states(state):
    """§3.3a — `error` 는 복구 후 `idle` 을 거쳐야 하므로 직접 배정하지 않는다."""
    op = weld_op()
    shop = shop_with(weld_cell())
    shop.status["welding_cell_01"] = CellStatus("welding_cell_01", state=state)
    fs = feasible_cells(one_job([op]), op, shop)
    assert fs.is_blocked
    assert fs.rejections[0].reason is Reject.STATE_NOT_ASSIGNABLE


def test_capacity_limit():
    op = weld_op()
    shop = shop_with(weld_cell(max_concurrent_tasks=1))
    shop.status["welding_cell_01"] = CellStatus(
        "welding_cell_01", state="running", running_op_ids=("other",))
    fs = feasible_cells(one_job([op]), op, shop)
    assert fs.rejections[0].reason is Reject.CAPACITY_FULL


def test_concurrent_capacity_allows_second_task():
    op = weld_op()
    shop = shop_with(weld_cell(max_concurrent_tasks=2))
    shop.status["welding_cell_01"] = CellStatus(
        "welding_cell_01", state="running", running_op_ids=("other",))
    assert feasible_cells(one_job([op]), op, shop)


def test_capability_exists_ignores_state():
    """위임 판정용 — 자격 셀이 있으나 busy 인 것과, 자격 셀이 0인 것을 가른다."""
    op = weld_op()
    shop = shop_with(weld_cell())
    shop.status["welding_cell_01"] = CellStatus("welding_cell_01", state="error")
    assert not feasible_cells(one_job([op]), op, shop)      # 지금은 못 준다
    assert capability_exists(op, shop)                       # 그러나 공정은 안 막혔다
    assert not capability_exists(op, shop_with(Cell("m1", "S5", "machining")))


def test_candidate_order_is_deterministic():
    shop = shop_with(weld_cell("welding_cell_02"), weld_cell("welding_cell_01"))
    op = weld_op()
    ids = [c.cell_id for c in feasible_cells(one_job([op]), op, shop).cells]
    assert ids == sorted(ids)


# ── ATCS: 세 요소가 실제로 작동하는가 ──────────────────────────


def test_priority_term_higher_weight_wins():
    """**우선순위** — `w_j` 가 크면 지수가 크다."""
    shop = shop_with(weld_cell())
    op = weld_op()
    lo = priority.atcs_index(one_job([op], weight=1.0), op, shop.cells["welding_cell_01"], shop)
    hi = priority.atcs_index(one_job([op], weight=5.0), op, shop.cells["welding_cell_01"], shop)
    assert hi > lo


def test_urgency_term_tight_due_wins():
    """**긴급도** — 납기가 임박할수록 지수가 크다."""
    shop = shop_with(weld_cell())
    op = weld_op(expected_duration_s=60.0)
    cell = shop.cells["welding_cell_01"]
    loose = priority.atcs_index(one_job([op], due_s=10_000.0), op, cell, shop)
    tight = priority.atcs_index(one_job([op], due_s=100.0), op, cell, shop)
    assert tight > loose


def test_urgency_saturates_when_overdue():
    """이미 납기를 넘겼으면 여유는 0으로 잘린다 — 지수가 발산하지 않는다."""
    shop = shop_with(weld_cell())
    op = weld_op(expected_duration_s=60.0)
    cell = shop.cells["welding_cell_01"]
    a = priority.atcs_index(one_job([op], due_s=0.0), op, cell, shop)
    b = priority.atcs_index(one_job([op], due_s=-9999.0), op, cell, shop)
    assert math.isclose(a, b)


def test_changeover_term_same_setup_wins():
    """**공정조건** — 직전과 같은 `step_id` 면 전환 비용이 없어 지수가 크다."""
    op = weld_op()
    shop = shop_with(weld_cell())
    cell = shop.cells["welding_cell_01"]
    shop.last_step["welding_cell_01"] = "weld_arc_pass1"
    same = priority.atcs_index(one_job([op]), op, cell, shop)
    shop.last_step["welding_cell_01"] = "weld_arc_pass9"
    diff = priority.atcs_index(one_job([op]), op, cell, shop)
    assert same > diff


def test_shorter_processing_time_wins():
    shop = shop_with(weld_cell("c_fast", nominal_cycle_time_s=10.0),
                     weld_cell("c_slow", nominal_cycle_time_s=100.0))
    op = weld_op()
    job = one_job([op])
    ranked = priority.rank(job, op, shop.cells.values(), shop)
    assert ranked[0].cell_id == "c_fast"


def test_rank_is_deterministic_on_ties():
    shop = shop_with(weld_cell("c_b"), weld_cell("c_a"))
    op = weld_op()
    job = one_job([op])
    r1 = [x.cell_id for x in priority.rank(job, op, shop.cells.values(), shop)]
    r2 = [x.cell_id for x in priority.rank(job, op, list(shop.cells.values())[::-1], shop)]
    assert r1 == r2 == ["c_a", "c_b"]


def test_ranked_carries_decision_components():
    """결정 로그가 1급 산출물이다 — 성분이 남아야 "왜 그 선택이었나"에 답한다."""
    shop = shop_with(weld_cell())
    op = weld_op(expected_duration_s=42.0)
    r = priority.rank(one_job([op], weight=3.0), op, shop.cells.values(), shop)[0]
    assert r.w == 3.0 and r.p_jc == 42.0
    assert r.slack_s != 0.0


def test_select_next_op_picks_global_best():
    shop = shop_with(weld_cell(), Cell("machining_cell_01", "S5", "machining"))
    urgent = Job("J_urgent", "P1", {"o": weld_op("o")}, due_s=10.0, weight=9.0)
    relaxed = Job("J_relaxed", "P2",
                  {"o": Operation("o", "mill_1", "machining")}, due_s=9_000.0, weight=1.0)
    best = priority.select_next_op([relaxed, urgent], shop)
    assert best is not None and best.job_id == "J_urgent"


def test_select_next_op_returns_none_when_nothing_feasible():
    shop = shop_with(weld_cell())
    shop.status["welding_cell_01"] = CellStatus("welding_cell_01", state="offline")
    op = weld_op()
    assert priority.select_next_op([one_job([op])], shop) is None
