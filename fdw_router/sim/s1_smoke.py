# -*- coding: utf-8 -*-
"""시나리오 S1 연기 시험 — 혼류 3 job × 3 셀, 디스패처만으로 완주.

`adapters/des_cell.py` 는 봉투(`msg/`)로만 말하므로 디스패처를 바로 꽂으려면
`core/adapt.py`(봉투 ↔ 도메인)가 필요하다. 그 전까지 이 하니스는 **봉투 없는 셀 스텁**과
작은 이산사건 시계로 디스패처의 거동만 검증한다.

TODO(adapt.py): `Assignment` → `task_request` 봉투, `task_accept/complete/reject` 봉투 →
`Dispatcher.on_task_*` 변환이 생기면 이 하니스를 `DesCellRuntime` 위로 옮긴다.
`des_cell.py` 자체는 바꿀 것이 없다 — 이미 수락·완료·거절을 봉투로 돌려준다.

실행: uv run --python 3.11 python -I -m fdw_router.sim.s1_smoke [--log 경로]
"""
from __future__ import annotations

import argparse
import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.decision_log import DecisionLog
from ..core.dispatcher import Assignment, Dispatcher
from ..core.model import Cell, Job, Operation, ProcessLimit, ShopState


class EventClock:
    """아주 작은 이산사건 시계. (시각, 순번, 콜백) 힙 — 순번으로 결정론을 보장한다."""

    def __init__(self) -> None:
        self.t = 0.0
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = 0

    def now(self) -> float:
        return self.t

    def at(self, delay_s: float, fn: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (self.t + delay_s, self._seq, fn))

    def run(self, until: float = float("inf")) -> None:
        while self._heap and self._heap[0][0] <= until:
            t, _, fn = heapq.heappop(self._heap)
            self.t = t
            fn()

    @property
    def empty(self) -> bool:
        return not self._heap


@dataclass
class StubCells:
    """셀 스텁 — 배정을 받으면 `accept_delay_s` 뒤 수락, 그 뒤 `expected_duration_s` 뒤 완료."""

    clock: EventClock
    dispatcher: Dispatcher
    accept_delay_s: float = 0.5
    reject_cells: set[str] = field(default_factory=set)     # 항상 거절하는 셀 (시험용)
    silent_cells: set[str] = field(default_factory=set)     # 응답 없는 셀 (타임아웃 시험용)
    trace: list[tuple[float, str, str]] = field(default_factory=list)

    def on_assign(self, a: Assignment) -> None:
        self.trace.append((self.clock.now(), "assign", f"{a.task_id}@{a.cell_id}"))
        if a.cell_id in self.silent_cells:
            return
        if a.cell_id in self.reject_cells:
            self.clock.at(self.accept_delay_s, lambda: self._reject(a))
            return
        self.clock.at(self.accept_delay_s, lambda: self._accept(a))

    def _accept(self, a: Assignment) -> None:
        self.trace.append((self.clock.now(), "accept", a.task_id))
        self.dispatcher.on_task_accepted(a.task_id)
        self.clock.at(a.expected_duration_s, lambda: self._complete(a))

    def _complete(self, a: Assignment) -> None:
        self.trace.append((self.clock.now(), "complete", a.task_id))
        self.dispatcher.on_task_completed(a.task_id)

    def _reject(self, a: Assignment) -> None:
        self.trace.append((self.clock.now(), "reject", a.task_id))
        self.dispatcher.on_task_rejected(a.task_id, a.cell_id, reason="stub_reject")


# ── S1 정의: 제품 1~3 혼류, 셀 3기 ─────────────────────────────────


def s1_cells() -> list[Cell]:
    lim = (ProcessLimit("weld_arc_current", min=100.0, max=300.0, unit="A"),)
    return [
        Cell("welding_cell_01", "S2", "welding", ("weld_pass",), nominal_cycle_time_s=60.0, process_limits=lim),
        Cell("welding_cell_02", "S2", "welding", ("weld_pass",), nominal_cycle_time_s=80.0, process_limits=lim),
        Cell("machining_cell_01", "S5", "machining", ("cnc_op_1", "final_op"), nominal_cycle_time_s=90.0),
    ]


def s1_jobs() -> list[Job]:
    weld = lambda oid, **kw: Operation(oid, "weld_pass", "welding_arc",
                                       params={"weld_arc_current": 200.0}, **kw)
    mill = lambda oid, **kw: Operation(oid, "cnc_op_1", "machining", **kw)
    fin = lambda oid, **kw: Operation(oid, "final_op", "machining", **kw)
    return [
        # 제품1: 용접 → 가공
        Job("J1", "P1", {"w": weld("w", expected_duration_s=60.0),
                         "m": mill("m", requires=("w",), expected_duration_s=90.0)},
            due_s=600.0, weight=1.0),
        # 제품2: 가공 → 용접 → 마무리 (순서 무관 분기 없음)
        Job("J2", "P2", {"m": mill("m", expected_duration_s=90.0),
                         "w": weld("w", requires=("m",), expected_duration_s=60.0),
                         "f": fin("f", requires=("w",), expected_duration_s=30.0)},
            due_s=700.0, weight=2.0),
        # 제품3: 용접만, 긴급(rush)
        Job("J3", "P3", {"w": weld("w", expected_duration_s=60.0)}, due_s=150.0, weight=4.0),
    ]


@dataclass
class S1Result:
    completed_jobs: list[str]
    decisions: int
    delegations: int
    makespan_s: float
    trace: list[tuple[float, str, str]]
    log_lines: list[str]

    @property
    def all_done(self) -> bool:
        return len(self.completed_jobs) == 3


def run_s1(log_path: str | Path | None = None, *, profile: str = "balanced",
           reject_cells: set[str] | None = None, silent_cells: set[str] | None = None,
           tick_s: float = 1.0, horizon_s: float = 5000.0) -> S1Result:
    clock = EventClock()
    shop = ShopState()
    for c in s1_cells():
        shop.add_cell(c)
    log = DecisionLog(log_path)
    stub_holder: dict = {}
    d = Dispatcher(shop, now=clock.now, profile=profile, log=log,
                   on_assign=lambda a: stub_holder["s"].on_assign(a))
    stub = StubCells(clock, d, reject_cells=reject_cells or set(), silent_cells=silent_cells or set())
    stub_holder["s"] = stub

    shop.now_s = clock.now()
    d.add_jobs(s1_jobs())          # 혼류 동시 투입 — 긴급(J3)이 ATCS 로 먼저 잡힌다

    def tick() -> None:
        shop.now_s = clock.now()
        d.tick()
        if not d.all_done and clock.now() < horizon_s and (d.pending or not clock.empty or
                                                            any(not d.is_job_done(j) and j not in _delegated_jobs(d) for j in d.jobs)):
            clock.at(tick_s, tick)

    clock.at(tick_s, tick)
    clock.run(until=horizon_s)
    shop.now_s = clock.now()

    done = [j for j in sorted(d.jobs) if d.is_job_done(j)]
    return S1Result(
        completed_jobs=done, decisions=len(log), delegations=len(d.verdicts),
        makespan_s=max((t for t, k, _ in stub.trace if k == "complete"), default=0.0),
        trace=list(stub.trace), log_lines=log.lines(),
    )


def _delegated_jobs(d: Dispatcher) -> set[str]:
    return {tid.split("/")[0] for tid in d.delegated}


def check_precedence(trace: list[tuple[float, str, str]], jobs: list[Job]) -> list[str]:
    """완료 순서가 DAG 를 어기면 위반 목록을 돌려준다."""
    done_at: dict[str, float] = {}
    for t, kind, what in trace:
        if kind == "complete":
            done_at[what] = t
    bad = []
    for job in jobs:
        for op in job.operations.values():
            me = f"{job.job_id}/{op.op_id}"
            for r in op.requires:
                pre = f"{job.job_id}/{r}"
                if me in done_at and (pre not in done_at or done_at[pre] > done_at[me]):
                    bad.append(f"{me} 가 {pre} 보다 먼저 끝남")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/s1_decisions.jsonl")
    ap.add_argument("--profile", default="balanced")
    ns = ap.parse_args()
    r = run_s1(ns.log, profile=ns.profile)
    print(f"S1 완료 job={r.completed_jobs} 결정={r.decisions} 위임={r.delegations} "
          f"makespan={r.makespan_s:.1f}s 제약위반={check_precedence(r.trace, s1_jobs())}")
    for t, k, w in r.trace:
        print(f"  {t:8.1f}  {k:8s} {w}")
    print(f"결정 로그 → {ns.log}")


if __name__ == "__main__":
    main()
