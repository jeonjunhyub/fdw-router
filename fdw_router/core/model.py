# -*- coding: utf-8 -*-
"""J축 도메인 모델.

**이 모듈은 `msg/` 를 import 하지 않는다.** 규격이 개정되어 봉투 모양이 바뀌어도
배정 알고리즘은 그대로 두기 위해서다. 봉투 ↔ 도메인 변환은 어댑터가 맡는다
(`core/adapt.py`). 이 분리가 B1~B3 이 어떻게 확정되든 J축을 지키는 장치다.

값의 출처는 규격이다 — 필드 이름을 규격과 같게 두어 대조가 쉽도록 했다.

    Cell        ← cell_capability.schema.json
    CellStatus  ← cell_state.schema.json
    Operation   ← process_sequence.schema.json  steps[]
    Job         ← production_order + 제품 공정계획(DAG)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ..config import PROCESS_TYPE_MAP

# ── 공정 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Operation:
    """공정계획(DAG)의 한 작업.

    `process_sequence.steps[]` 한 항목에 대응한다. `requires` 가 DAG 의 간선이다.
    """

    op_id: str
    step_id: str                      # 지시에 싣는 실행 단위 (03a_실행단위_step_id)
    process_type: str                 # process_sequence 어휘 (13종)
    requires: tuple[str, ...] = ()
    params: Mapping[str, float | str] = field(default_factory=dict)
    expected_duration_s: float | None = None   # step 1회 수행 (이동 제외)
    transfer_time_s: float = 0.0               # 선행이 하나일 때만 (간선 귀속)

    @property
    def cell_process_type(self) -> str | None:
        """셀 쪽 어휘로 변환. `None` 이면 배정 대상이 아니다 (요청 6)."""
        return PROCESS_TYPE_MAP.get(self.process_type)


@dataclass(frozen=True)
class Job:
    """생산 오더 하나가 만들어 내는 작업 묶음."""

    job_id: str
    product_id: str
    operations: Mapping[str, Operation]
    due_s: float                       # 납기 (시뮬레이션 시각, 초)
    weight: float = 1.0                # w_j — 요청 2 확정 전에는 시나리오가 싣는다
    released_s: float = 0.0            # 투입 시각
    quantity: int = 1

    def __post_init__(self) -> None:
        missing = [
            r for op in self.operations.values() for r in op.requires
            if r not in self.operations
        ]
        if missing:
            raise ValueError(f"{self.job_id}: 존재하지 않는 선행 작업 {sorted(set(missing))}")
        self._check_acyclic()

    def _check_acyclic(self) -> None:
        """DAG 여부를 확인한다. 순환이 있으면 배정이 영원히 끝나지 않는다."""
        WHITE, GREY, BLACK = 0, 1, 2
        color = {k: WHITE for k in self.operations}

        def visit(k: str, path: list[str]) -> None:
            if color[k] == GREY:
                raise ValueError(f"{self.job_id}: 공정계획에 순환 {' → '.join(path + [k])}")
            if color[k] == BLACK:
                return
            color[k] = GREY
            for r in self.operations[k].requires:
                visit(r, path + [k])
            color[k] = BLACK

        for k in self.operations:
            visit(k, [])

    def ready_ops(self, done: Iterable[str]) -> tuple[Operation, ...]:
        """선행이 모두 끝나 **지금 배정 가능한** 작업들."""
        done_set = set(done)
        return tuple(
            op for op in self.operations.values()
            if op.op_id not in done_set and set(op.requires) <= done_set
        )

    def remaining_work_s(self, done: Iterable[str], default_s: float) -> float:
        """남은 작업량 합. 납기 여유(slack) 계산에 쓴다."""
        done_set = set(done)
        return sum(
            (op.expected_duration_s if op.expected_duration_s is not None else default_s)
            for op in self.operations.values() if op.op_id not in done_set
        )


# ── 셀 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcessLimit:
    """`cell_capability.process_limits[]` 한 항목."""

    param_name: str
    min: float | None = None
    max: float | None = None
    allowed_values: tuple[str | float, ...] | None = None
    unit: str | None = None
    param_role: str | None = None

    def accepts(self, value: float | str) -> bool:
        if self.allowed_values is not None:
            return value in self.allowed_values
        if isinstance(value, str):
            return True            # 범위 제약은 수치 파라미터에만 건다
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True


@dataclass(frozen=True)
class Cell:
    """`cell_capability` 가 공지한 셀의 능력. 상태는 `CellStatus` 가 따로 든다."""

    cell_id: str
    owner_subtask: str                 # S1~S6
    process_type: str                  # cell_capability 어휘 (6종)
    supported_task_types: tuple[str, ...] = ()
    max_concurrent_tasks: int = 1
    nominal_cycle_time_s: float = 60.0
    process_limits: tuple[ProcessLimit, ...] = ()
    registered: bool = True            # §3.2b 노드 사전 등재 여부 (항목 102)

    def limit_for(self, param_name: str) -> ProcessLimit | None:
        for lim in self.process_limits:
            if lim.param_name == param_name:
                return lim
        return None

    def duration_for(self, op: Operation) -> float:
        """`p_jc` — 이 셀에서 이 작업의 처리시간.

        공정 계획의 값이 있으면 그것을, 없으면 셀의 정격 사이클을 쓴다.
        (공정 소요시간 실측은 S4 만 있으므로 나머지는 `baseline_example` 범위)
        """
        if op.expected_duration_s is not None:
            return op.expected_duration_s
        return self.nominal_cycle_time_s


@dataclass
class CellStatus:
    """`cell_state` 최신값. 변한다."""

    cell_id: str
    state: str = "idle"                # idle/running/paused/blocked/error/maintenance/offline
    handling_mode: str = "autonomous"
    queue_depth: int = 0
    progress_pct: float = 0.0
    running_op_ids: tuple[str, ...] = ()
    remaining_s: float = 0.0           # 진행 중 작업의 잔여시간
    available: bool = True

    @property
    def load_s(self) -> float:
        """`Wait(c)` — 진행중 잔여 + 대기큐. 대기시간 최소화 항의 입력."""
        return self.remaining_s


# ── 작업장 전체 상태 ────────────────────────────────────────────────


@dataclass
class ShopState:
    """배정 시점의 작업장 스냅샷. 결정론을 위해 이 값만 보고 판단한다."""

    now_s: float = 0.0
    cells: dict[str, Cell] = field(default_factory=dict)
    status: dict[str, CellStatus] = field(default_factory=dict)
    #: job_id -> 완료된 op_id 집합
    done: dict[str, set[str]] = field(default_factory=dict)
    #: cell_id -> 마지막으로 수행한 step_id (셋업 전환 비용 판정용)
    last_step: dict[str, str] = field(default_factory=dict)
    #: job_id -> 직전 작업이 끝난 셀 (이송 비용 판정용)
    last_cell: dict[str, str] = field(default_factory=dict)

    def add_cell(self, cell: Cell, status: CellStatus | None = None) -> None:
        self.cells[cell.cell_id] = cell
        self.status[cell.cell_id] = status or CellStatus(cell_id=cell.cell_id)

    def status_of(self, cell_id: str) -> CellStatus:
        return self.status.setdefault(cell_id, CellStatus(cell_id=cell_id))

    def done_of(self, job_id: str) -> set[str]:
        return self.done.setdefault(job_id, set())

    def mean_duration_s(self, default_s: float = 60.0) -> float:
        """`p_avg` — ATCS 지수의 정규화 상수."""
        vals = [c.nominal_cycle_time_s for c in self.cells.values()]
        return sum(vals) / len(vals) if vals else default_s
