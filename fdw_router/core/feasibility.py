# -*- coding: utf-8 -*-
"""실행가능성 필터 — 하드 제약.

하드 제약: process_type 일치, 재료·치구 호환, 공정 파라미터 정격범위 내,
셀 상태 available, 선행 op 완료 — **하나라도 위배하면 후보에서 뺀다.**

배정 알고리즘의 첫 단계이자, 탈락 사유를 남기는 곳이다. 사유를 남기는 이유는
설계 문서가 결정 로그를 **1급 산출물**로 두었기 때문이다 — "왜 그 셀이 아니었나"에
답할 수 없으면 KPI 개선 주장의 근거가 서지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..config import ASSIGNABLE_STATES, PENDING
from .model import Cell, Job, Operation, ShopState


class Reject(str, Enum):
    """탈락 사유. 결정 로그에 그대로 싣는다."""

    PREREQ_NOT_DONE = "선행 작업 미완료"
    NO_CELL_PROCESS_TYPE = "대응 셀 process_type 없음 (요청 6)"
    PROCESS_TYPE_MISMATCH = "process_type 불일치"
    TASK_TYPE_UNSUPPORTED = "supported_task_types 에 step_id 없음"
    PARAM_OUT_OF_RANGE = "공정 파라미터 정격범위 밖"
    STATE_NOT_ASSIGNABLE = "셀 상태가 배정 불가"
    CAPACITY_FULL = "동시 처리 한도 초과"
    NOT_REGISTERED = "노드 사전 미등재 (항목 102)"


@dataclass(frozen=True)
class Rejection:
    cell_id: str
    reason: Reject
    detail: str = ""


@dataclass(frozen=True)
class FeasibleSet:
    """필터 결과. 통과한 셀과 탈락 사유를 함께 돌려준다."""

    op_id: str
    cells: tuple[Cell, ...]
    rejections: tuple[Rejection, ...]

    def __bool__(self) -> bool:
        return bool(self.cells)

    @property
    def is_blocked(self) -> bool:
        """후보가 0개다 — `delegation` 판정의 입력이 된다."""
        return not self.cells


def prerequisites_met(job: Job, op: Operation, shop: ShopState) -> bool:
    return set(op.requires) <= shop.done_of(job.job_id)


def _param_violation(cell: Cell, op: Operation) -> str | None:
    """`process_limits` 위반을 찾는다. 제약이 없는 파라미터는 통과시킨다."""
    for name, value in op.params.items():
        lim = cell.limit_for(name)
        if lim is None:
            continue                      # 셀이 제약을 공지하지 않은 파라미터
        if not lim.accepts(value):
            rng = (
                f"allowed={list(lim.allowed_values)}"
                if lim.allowed_values is not None
                else f"[{lim.min}, {lim.max}]"
            )
            return f"{name}={value} ∉ {rng}"
    return None


def _state_ok(cell: Cell, shop: ShopState) -> Reject | None:
    st = shop.status_of(cell.cell_id)
    if st.state not in ASSIGNABLE_STATES or not st.available:
        return Reject.STATE_NOT_ASSIGNABLE
    if len(st.running_op_ids) >= cell.max_concurrent_tasks:
        return Reject.CAPACITY_FULL
    return None


def feasible_cells(job: Job, op: Operation, shop: ShopState) -> FeasibleSet:
    """작업 하나에 대해 배정 가능한 셀을 고른다."""
    rejections: list[Rejection] = []

    # ① 선행 작업 (DAG). 셀과 무관하므로 먼저 본다.
    if not prerequisites_met(job, op, shop):
        pending = sorted(set(op.requires) - shop.done_of(job.job_id))
        return FeasibleSet(
            op_id=op.op_id,
            cells=(),
            rejections=(Rejection("*", Reject.PREREQ_NOT_DONE, ", ".join(pending)),),
        )

    # ② 이 작업이 셀 배정 대상인가 (요청 6)
    want = op.cell_process_type
    if want is None:
        return FeasibleSet(
            op_id=op.op_id,
            cells=(),
            rejections=(
                Rejection("*", Reject.NO_CELL_PROCESS_TYPE, op.process_type),
            ),
        )

    # ③ 셀별 하드 제약
    passed: list[Cell] = []
    for cell in shop.cells.values():
        if cell.process_type != want:
            rejections.append(
                Rejection(cell.cell_id, Reject.PROCESS_TYPE_MISMATCH,
                          f"{cell.process_type} ≠ {want}")
            )
            continue

        if cell.supported_task_types and op.step_id not in cell.supported_task_types:
            rejections.append(
                Rejection(cell.cell_id, Reject.TASK_TYPE_UNSUPPORTED, op.step_id)
            )
            continue

        violation = _param_violation(cell, op)
        if violation:
            rejections.append(
                Rejection(cell.cell_id, Reject.PARAM_OUT_OF_RANGE, violation)
            )
            continue

        if not cell.registered and not PENDING.assign_before_registration:
            rejections.append(Rejection(cell.cell_id, Reject.NOT_REGISTERED))
            continue

        bad_state = _state_ok(cell, shop)
        if bad_state:
            st = shop.status_of(cell.cell_id)
            rejections.append(
                Rejection(cell.cell_id, bad_state,
                          f"state={st.state} running={len(st.running_op_ids)}/{cell.max_concurrent_tasks}")
            )
            continue

        passed.append(cell)

    # 결정론: 같은 입력이면 같은 순서. 셀 id 로 고정한다.
    passed.sort(key=lambda c: c.cell_id)
    return FeasibleSet(op.op_id, tuple(passed), tuple(rejections))


def capability_exists(op: Operation, shop: ShopState) -> bool:
    """**상태를 무시하고** 이 작업을 할 수 있는 셀이 애초에 있는가.

    `delegation` 판정이 이것을 쓴다 — 자격 셀이 0개면 공정 자체가 막힌 것이라
    라우터가 재배정으로 풀 수 없다. 반대로 자격 셀은 있는데 전부 busy/fault 면
    기다리거나 다른 셀로 돌리면 되므로 위임 대상이 아니다.
    """
    want = op.cell_process_type
    if want is None:
        return False
    return any(
        c.process_type == want
        and (not c.supported_task_types or op.step_id in c.supported_task_types)
        and _param_violation(c, op) is None
        for c in shop.cells.values()
    )
