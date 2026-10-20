# -*- coding: utf-8 -*-
"""이벤트 트리거 재평가 루프 — J축의 실행기.

재평가 트리거: 작업완료 / 셀 idle 전이 / 신규 오더 / 셀 fault / 긴급오더 삽입.
트리거마다 **실행가능성 → 우선순위(ATCS) → 셀 선택(Cost)** 를 돌려 배정을 낸다.

런타임을 모른다 — SimPy 도, 봉투도 여기 없다. 시각은 `now()` 로 받고, 결과는
콜백(`on_assign`·`on_delegate`)으로 내보낸다. 봉투로 바꾸는 일은 `core/adapt.py` 몫이다.

타임아웃은 둘이다 (규격 협의 결과):
  * 수락 대기   — 지시 발행 → task_accept/reject 까지. 고정 초.
  * 완료 대기   — 수락 → task_complete/failed 까지. `expected_duration_s × 배수`.
초과하면 재배정하고, 재배정 한도를 넘으면 위임(2014 ⓒ). 거절이 쌓여 후보가 소진되면 2014 ⓑ.

결정론: job 은 `job_id`, op 는 `op_id`, 셀은 `cell_id` 순으로 본다. 난수 없음.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from ..config import PENDING
from . import priority
from .decision_log import DecisionLog, make_record
from .delegation import DelegationVerdict, judge
from .feasibility import feasible_cells
from .model import Job, Operation, ShopState
from .selector import DEFAULT_PROFILE, NO_TRANSFER, TransferTable, rank_cost


@dataclass(frozen=True)
class Assignment:
    """라우터의 배정 결과 하나. `adapt.py` 가 이것을 `task_request` 봉투로 만든다."""

    task_id: str
    job_id: str
    op_id: str
    cell_id: str
    step_id: str
    expected_duration_s: float
    params: Mapping[str, float | str]
    issued_at_s: float
    attempt: int = 1


@dataclass
class _Pending:
    assignment: Assignment
    accepted_at_s: float | None = None

    @property
    def cell_id(self) -> str:
        return self.assignment.cell_id


def task_id_of(job_id: str, op_id: str) -> str:
    """작업 식별자. 재배정해도 같은 `task_id` 를 유지한다 — 응답·결과 대조의 공통 키."""
    return f"{job_id}/{op_id}"


class Dispatcher:
    """작업장 스냅샷(`ShopState`) 위에서 배정을 내는 루프.

    사용법 (런타임 쪽에서):
        d = Dispatcher(shop, now=rt.now, on_assign=send_task_request, on_delegate=emit_2014)
        d.add_job(job)                    # 신규 오더
        ...
        d.on_task_accepted(task_id)       # 셀 응답을 받을 때마다
        d.on_task_completed(task_id)
        d.on_task_rejected(task_id, cell_id)
        d.on_cell_state(cell_id, "idle")  # cell_status 수신 시
        d.tick()                          # 주기적으로 — 타임아웃 검사 + 재평가
    """

    def __init__(self, shop: ShopState, *, now: Callable[[], float],
                 on_assign: Callable[[Assignment], None] | None = None,
                 on_delegate: Callable[[DelegationVerdict], None] | None = None,
                 profile: str = DEFAULT_PROFILE,
                 transfer: TransferTable = NO_TRANSFER,
                 atcs: priority.AtcsParams = priority.DEFAULT,
                 accept_timeout_s: float | None = None,
                 complete_timeout_factor: float | None = None,
                 reassign_limit: int | None = None,
                 log: DecisionLog | None = None):
        self.shop = shop
        self.now = now
        self.on_assign = on_assign or (lambda a: None)
        self.on_delegate = on_delegate or (lambda v: None)
        self.profile = profile
        self.transfer = transfer
        self.atcs = atcs
        self.accept_timeout_s = (accept_timeout_s if accept_timeout_s is not None
                                 else getattr(PENDING, "accept_timeout_s", 5.0))
        self.complete_timeout_factor = (complete_timeout_factor if complete_timeout_factor is not None
                                        else getattr(PENDING, "complete_timeout_factor", 1.5))
        self.reassign_limit = (reassign_limit if reassign_limit is not None
                               else getattr(PENDING, "reassign_limit", 3))
        self.log = log if log is not None else DecisionLog()

        self.jobs: dict[str, Job] = {}
        self.pending: dict[str, _Pending] = {}        # task_id → 발행했고 아직 안 끝남
        self.rejected: dict[str, set[str]] = {}       # task_id → 거절한 셀
        self.attempts: dict[str, int] = {}            # task_id → 발행 횟수
        self.delegated: set[str] = set()              # 2014 를 낸 작업 — 라우터는 손 뗀다
        self.failed: dict[str, str] = {}              # task_id → 마지막 실패 사유
        self.assignments: list[Assignment] = []       # 발행 이력 (결정 지연·KPI 용)
        self.verdicts: list[DelegationVerdict] = []
        self.timeouts: list[tuple[str, str, float]] = []   # (task_id, kind, at)

    # ── 트리거 ──────────────────────────────────────────────────────

    def add_job(self, job: Job, *, rush: bool = False) -> list[Assignment]:
        """신규 오더 / 긴급오더 삽입. `rush` 는 `w_j` 가 이미 반영된 job 을 바로 재평가한다."""
        self.jobs[job.job_id] = job
        self.shop.done_of(job.job_id)
        return self.reevaluate(trigger="rush_order" if rush else "new_order")

    def add_jobs(self, jobs: Iterable[Job]) -> list[Assignment]:
        """동시 투입 — 전부 등록한 뒤 한 번만 재평가한다 (혼류 투입 시나리오)."""
        for job in jobs:
            self.jobs[job.job_id] = job
            self.shop.done_of(job.job_id)
        return self.reevaluate(trigger="new_orders")

    def on_cell_state(self, cell_id: str, state: str, *, remaining_s: float | None = None,
                      available: bool | None = None) -> list[Assignment]:
        """`cell_status` 수신. idle 전이·fault 가 재평가 트리거다."""
        st = self.shop.status_of(cell_id)
        prev = st.state
        st.state = state
        if remaining_s is not None:
            st.remaining_s = remaining_s
        if available is not None:
            st.available = available
        if state == "error":
            return self.on_cell_fault(cell_id)
        if state == "idle" and prev != "idle":
            return self.reevaluate(trigger="cell_idle")
        return []

    def on_cell_fault(self, cell_id: str) -> list[Assignment]:
        """셀 fault — 그 셀에 걸린 작업은 거절로 보고 재배정한다."""
        st = self.shop.status_of(cell_id)
        st.state = "error"
        for task_id, p in list(self.pending.items()):
            if p.cell_id == cell_id:
                self._release(task_id)
                self.rejected.setdefault(task_id, set()).add(cell_id)
                self.failed[task_id] = "cell_fault"
        return self.reevaluate(trigger="cell_fault")

    def on_task_accepted(self, task_id: str) -> None:
        p = self.pending.get(task_id)
        if p is not None and p.accepted_at_s is None:
            p.accepted_at_s = self.now()

    def on_task_rejected(self, task_id: str, cell_id: str | None = None,
                         reason: str = "") -> list[Assignment]:
        """셀 거절 — 그 셀을 후보에서 빼고 다시 배정한다. 전부 거절하면 2014 ⓑ."""
        p = self.pending.get(task_id)
        cell = cell_id or (p.cell_id if p else None)
        if cell:
            self.rejected.setdefault(task_id, set()).add(cell)
        if p is not None:
            self._release(task_id)
        self.failed[task_id] = reason or "rejected"
        return self.reevaluate(trigger="task_rejected")

    def on_task_completed(self, task_id: str) -> list[Assignment]:
        """작업완료 — DAG 를 전진시키고 재평가한다."""
        p = self.pending.pop(task_id, None)
        if p is None:
            return []
        a = p.assignment
        self.shop.done_of(a.job_id).add(a.op_id)
        self.shop.last_cell[a.job_id] = a.cell_id
        self.shop.last_step[a.cell_id] = a.step_id
        st = self.shop.status_of(a.cell_id)
        st.running_op_ids = tuple(x for x in st.running_op_ids if x != task_id)
        st.remaining_s = max(0.0, st.remaining_s - a.expected_duration_s)
        if not st.running_op_ids:
            st.state = "idle"
            st.remaining_s = 0.0
        return self.reevaluate(trigger="task_completed")

    def on_task_failed(self, task_id: str, reason: str = "task_failed") -> list[Assignment]:
        """실행 중 실패 — 완료 대기 초과와 같은 경로(재배정 → 한도 초과 시 2014 ⓒ)."""
        p = self.pending.get(task_id)
        if p is None:
            return []
        self._release(task_id)
        self.failed[task_id] = reason
        return self.reevaluate(trigger="task_failed")

    def tick(self) -> list[Assignment]:
        """주기 호출 — 타임아웃 검사 후 재평가."""
        now = self.now()
        changed = False
        for task_id, p in list(self.pending.items()):
            a = p.assignment
            if p.accepted_at_s is None:
                if now - a.issued_at_s > self.accept_timeout_s:
                    self.timeouts.append((task_id, "accept", now))
                    self._release(task_id)
                    self.failed[task_id] = "accept_timeout"
                    changed = True
            else:
                limit = a.expected_duration_s * self.complete_timeout_factor
                if now - p.accepted_at_s > limit:
                    self.timeouts.append((task_id, "complete", now))
                    self._release(task_id)
                    self.failed[task_id] = "complete_timeout"
                    changed = True
        return self.reevaluate(trigger="timeout" if changed else "tick")

    # ── 재평가 ─────────────────────────────────────────────────────

    def reevaluate(self, trigger: str = "manual") -> list[Assignment]:
        """배정 가능한 것이 없어질 때까지 (작업, 셀) 을 하나씩 낸다."""
        issued: list[Assignment] = []
        while True:
            a = self._assign_one(trigger)
            if a is None:
                break
            issued.append(a)
        return issued

    def _open_ops(self) -> list[tuple[Job, Operation]]:
        """지금 배정 대상인 (job, op): 선행 완료, 미완료, 미발행, 미위임."""
        out = []
        for job_id in sorted(self.jobs):
            job = self.jobs[job_id]
            done = self.shop.done_of(job_id)
            for op in sorted(job.ready_ops(done), key=lambda o: o.op_id):
                tid = task_id_of(job_id, op.op_id)
                if tid in self.pending or tid in self.delegated:
                    continue
                out.append((job, op))
        return out

    def _assign_one(self, trigger: str) -> Assignment | None:
        t0 = self.now()
        best = None          # (ranked_top, job, op, feasible_set)
        for job, op in self._open_ops():
            tid = task_id_of(job.job_id, op.op_id)
            fs = feasible_cells(job, op, self.shop)
            cells = [c for c in fs.cells if c.cell_id not in self.rejected.get(tid, ())]
            if not cells:
                verdict = judge(job, op, self.shop, task_id=tid,
                                rejected_cells=self.rejected.get(tid, ()),
                                reassign_count=self.attempts.get(tid, 0),
                                reassign_limit=self.reassign_limit)
                if verdict is not None:
                    self._delegate(verdict, job, op, fs, t0)
                continue
            top = priority.rank(job, op, cells, self.shop, self.atcs)[0]
            if best is None or _better(top, best[0]):
                best = (top, job, op, fs, cells)
        if best is None:
            return None

        top, job, op, fs, cells = best
        tid = task_id_of(job.job_id, op.op_id)
        # 재배정 한도 — 발행 **전에** 검사한다 (ⓒ)
        attempt = self.attempts.get(tid, 0) + 1
        if attempt > self.reassign_limit + 1:
            verdict = judge(job, op, self.shop, task_id=tid,
                            rejected_cells=self.rejected.get(tid, ()),
                            reassign_count=attempt - 1, reassign_limit=self.reassign_limit)
            if verdict is None:   # judge 가 ⓐ/ⓑ 를 먼저 보므로 ⓒ 가 아닐 수도 있다
                from .delegation import DelegationVerdict as _V, Reason as _R
                verdict = _V(_R.REASSIGN_LIMIT_EXCEEDED, tid,
                             {"op_id": op.op_id, "reassign_count": attempt - 1,
                              "reassign_limit": self.reassign_limit})
            self._delegate(verdict, job, op, fs, t0)
            return self._assign_one(trigger)

        atcs_ranked = priority.rank(job, op, cells, self.shop, self.atcs)
        cost_ranked = rank_cost(job, op, cells, self.shop, profile=self.profile,
                                transfer=self.transfer, atcs=self.atcs)
        chosen = cost_ranked[0]
        cell = self.shop.cells[chosen.cell_id]
        a = Assignment(
            task_id=tid, job_id=job.job_id, op_id=op.op_id, cell_id=cell.cell_id,
            step_id=op.step_id, expected_duration_s=cell.duration_for(op),
            params=dict(op.params), issued_at_s=self.now(), attempt=attempt,
        )
        # 예약 — 같은 라운드에서 같은 셀에 중복 배정하지 않도록 상태를 먼저 바꾼다
        st = self.shop.status_of(cell.cell_id)
        st.running_op_ids = st.running_op_ids + (tid,)
        st.remaining_s += a.expected_duration_s
        if st.state == "idle":
            st.state = "running"
        self.pending[tid] = _Pending(a)
        self.attempts[tid] = attempt
        self.assignments.append(a)

        self.log.write(make_record(
            task_id=tid, job_id=job.job_id, op_id=op.op_id, outcome="assigned",
            decided_at_s=t0, duration_ms=(self.now() - t0) * 1000.0, profile=self.profile,
            candidates=[c.cell_id for c in cells], chosen_cell=cell.cell_id,
            rejections=fs.rejections, atcs=atcs_ranked, cost=cost_ranked,
            note=trigger, attempt=attempt,
        ))
        self.on_assign(a)
        return a

    # ── 내부 ────────────────────────────────────────────────────────

    def _release(self, task_id: str) -> None:
        """발행 취소 — 셀 예약을 되돌린다."""
        p = self.pending.pop(task_id, None)
        if p is None:
            return
        a = p.assignment
        st = self.shop.status_of(a.cell_id)
        st.running_op_ids = tuple(x for x in st.running_op_ids if x != task_id)
        st.remaining_s = max(0.0, st.remaining_s - a.expected_duration_s)
        if not st.running_op_ids and st.state == "running":
            st.state = "idle"

    def _delegate(self, verdict: DelegationVerdict, job: Job, op: Operation, fs, t0: float) -> None:
        if verdict.task_id in self.delegated:
            return
        self.delegated.add(verdict.task_id)
        self.verdicts.append(verdict)
        self.log.write(make_record(
            task_id=verdict.task_id, job_id=job.job_id, op_id=op.op_id, outcome="delegated",
            decided_at_s=t0, duration_ms=(self.now() - t0) * 1000.0, profile=self.profile,
            candidates=[], chosen_cell=None, rejections=fs.rejections,
            note=verdict.reason.value, attempt=self.attempts.get(verdict.task_id, 0),
        ))
        self.on_delegate(verdict)

    # ── 조회 ────────────────────────────────────────────────────────

    def is_job_done(self, job_id: str) -> bool:
        job = self.jobs[job_id]
        return set(job.operations) <= self.shop.done_of(job_id)

    @property
    def all_done(self) -> bool:
        return all(self.is_job_done(j) for j in self.jobs)


def _better(a: priority.Ranked, b: priority.Ranked) -> bool:
    """a 가 b 보다 먼저 배정되어야 하는가. 지수가 크면 먼저, 동점이면 (job_id, op_id) 오름차순."""
    if a.index != b.index:
        return a.index > b.index
    return (a.job_id, a.op_id) < (b.job_id, b.op_id)
