# -*- coding: utf-8 -*-
"""자체해결 / 위임 판정 — 이벤트 `2014` 의 발행 조건.

규격 v2.2 `T2_guideline` §3.2c 가 조건을 **셋으로 고정**했다. 그 밖의 사유로는 2014 를
내지 않는다 (조건을 늘리면 규격 개정이 필요하다).

    ⓐ 해당 step 의 process_type 을 수행할 가용 셀이 없음      → no_capable_cell
    ⓑ 후보 셀이 모두 거절하여 후보가 소진됨                  → candidates_exhausted
    ⓒ 수락 응답 미수신으로 재배정 한도를 초과함               → reassign_limit_exceeded

**busy 는 위임 사유가 아니다.** 자격 셀이 있는데 전부 running/fault 면 기다리거나 다른
셀로 돌리면 되므로 라우터가 자체 해결한다 (`feasibility.capability_exists`).

2014 는 **조치 요청이 아니라 사건 통지**다. 오케스트레이션 계층이 이 통지를 근거로
자기 권한으로 `replanning_request` 를 발행한다. 대상 작업은 `detail` 로 운반한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from ..config import PENDING
from .feasibility import capability_exists, feasible_cells
from .model import Job, Operation, ShopState


class Reason(str, Enum):
    NO_CAPABLE_CELL = "no_capable_cell"
    CANDIDATES_EXHAUSTED = "candidates_exhausted"
    REASSIGN_LIMIT_EXCEEDED = "reassign_limit_exceeded"


#: 2014 `detail` 에 싣는 사유 문구 (한국어, 규격 §3.2c 문장 그대로)
REASON_TEXT = {
    Reason.NO_CAPABLE_CELL: "해당 step 의 process_type 을 수행할 가용 셀이 없음",
    Reason.CANDIDATES_EXHAUSTED: "후보 셀이 모두 거절하여 후보가 소진됨",
    Reason.REASSIGN_LIMIT_EXCEEDED: "수락 응답 미수신으로 재배정 한도를 초과함",
}


@dataclass(frozen=True)
class DelegationVerdict:
    """2014 발행 판정. `reason` 이 셋 중 하나이며, `detail` 이 통지 본문이 된다."""

    reason: Reason
    task_id: str
    detail: dict = field(default_factory=dict)

    @property
    def event_code(self) -> int:
        return getattr(PENDING, "delegation_event_code", 2014)

    def as_event_detail(self) -> dict:
        """`event.detail` 로 운반할 형태. 대상 작업·사유·후보 이력을 담는다."""
        return {
            "task_id": self.task_id,
            "reason": self.reason.value,
            "reason_text": REASON_TEXT[self.reason],
            **self.detail,
        }


def capable_cell_ids(op: Operation, shop: ShopState) -> tuple[str, ...]:
    """상태를 무시하고 이 작업을 할 수 있는 셀 전부 (정렬)."""
    want = op.cell_process_type
    if want is None:
        return ()
    from .feasibility import _param_violation  # 같은 판정 기준을 공유한다
    return tuple(sorted(
        c.cell_id for c in shop.cells.values()
        if c.process_type == want
        and (not c.supported_task_types or op.step_id in c.supported_task_types)
        and _param_violation(c, op) is None
    ))


def judge(job: Job, op: Operation, shop: ShopState, *, task_id: str,
          rejected_cells: Iterable[str] = (),
          reassign_count: int = 0,
          reassign_limit: int | None = None) -> DelegationVerdict | None:
    """위임해야 하는가. 아니면 `None` (라우터가 계속 든다 — 대기 또는 재배정).

    판정 순서는 규격 조건 순서(ⓐ→ⓑ→ⓒ)다. 둘 이상 겹치면 앞 조건으로 통지한다.
    """
    limit = reassign_limit if reassign_limit is not None else getattr(PENDING, "reassign_limit", 3)
    rejected = set(rejected_cells)

    # ⓐ 공정 자체가 막혔다 — 재배정으로 못 푼다
    if not capability_exists(op, shop):
        return DelegationVerdict(
            Reason.NO_CAPABLE_CELL, task_id,
            {"op_id": op.op_id, "step_id": op.step_id, "process_type": op.process_type,
             "cell_process_type": op.cell_process_type,
             "cells_known": sorted(shop.cells)},
        )

    # ⓑ 자격 셀은 있는데 전부 거절했다
    capable = capable_cell_ids(op, shop)
    if rejected and set(capable) <= rejected:
        return DelegationVerdict(
            Reason.CANDIDATES_EXHAUSTED, task_id,
            {"op_id": op.op_id, "step_id": op.step_id,
             "candidates": list(capable), "rejected": sorted(rejected)},
        )

    # ⓒ 수락이 안 와서 다시 배정하기를 한도만큼 했다
    if reassign_count > limit:
        return DelegationVerdict(
            Reason.REASSIGN_LIMIT_EXCEEDED, task_id,
            {"op_id": op.op_id, "step_id": op.step_id,
             "reassign_count": reassign_count, "reassign_limit": limit,
             "candidates": list(capable), "rejected": sorted(rejected)},
        )

    return None


def is_waiting(job: Job, op: Operation, shop: ShopState) -> bool:
    """위임 사유는 없지만 지금 당장 줄 셀이 없는 상태 (busy/fault) — 라우터가 든다."""
    return (not feasible_cells(job, op, shop)) and capability_exists(op, shop)
