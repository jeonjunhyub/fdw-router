# -*- coding: utf-8 -*-
"""SimPy 이산사건 셀 런타임 — 1차년도 검증 환경.

`IsaacCellRuntime` 과 **같은 포트**(`CellRuntime`)를 구현한다. 라우터 쪽에서 보면
둘은 구분되지 않는다. 그것이 2차년도 Isaac Sim 도입을 어댑터 교체로 끝내는 조건이다.

여기서 흉내 내는 것은 **셀의 관측 가능한 거동**뿐이다 — 지시를 받고, 수락하고,
일정 시간 뒤 완료를 알리고, 주기적으로 상태를 올린다. 공정 물리는 흉내 내지 않는다.
그건 2차년도 Isaac Sim 의 몫이고, 그래서 J축 알고리즘은 물리에 기대지 않는다.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable

import simpy

from ..msg.build import EnvelopeFactory


@dataclass
class CellSpec:
    """DES 안의 셀 하나. 값은 규격 `cell_capability` 에서 채운다."""

    node_id: str
    owner_subtask: str
    process_type: str
    supported_task_types: tuple[str, ...] = ()
    max_concurrent_tasks: int = 1
    nominal_cycle_time_s: float = 60.0
    #: 소요시간 변동 (0.1 = ±10%). 확정 실측이 없으므로 baseline_example 범위를 쓴다.
    duration_jitter: float = 0.0
    #: 고장 주입용. 시나리오 S3·S7 에서 쓴다.
    fault_at: float | None = None
    recipient_binding: dict = field(default_factory=lambda: {"cell_status": "ops-01"})


class DesCellRuntime:
    """SimPy 위에서 여러 셀을 돌리는 런타임.

    포트 계약:
      * `send`  — 라우터가 준 봉투를 대상 셀에 넣는다
      * `poll`  — 셀이 만든 봉투를 모아 돌려준다
      * `step`  — 다음 이산사건까지 가상 시간을 진행시킨다
      * `now`   — **가상** 시각(초). 라우터의 타임아웃은 이 값을 쓴다
    """

    name = "des"

    def __init__(
        self,
        cells: list[CellSpec],
        *,
        run_id: str = "run-0",
        seed: int = 0,
        status_period_s: float = 1.0,
        heartbeat_period_s: float = 5.0,
    ):
        self.env = simpy.Environment()
        self.cells = {c.node_id: c for c in cells}
        self.factory = EnvelopeFactory(run_id=run_id)
        self.rng = random.Random(seed)
        self.status_period_s = status_period_s
        self.heartbeat_period_s = heartbeat_period_s

        self._outbox: list[dict] = []
        self._state: dict[str, str] = {n: "idle" for n in self.cells}
        self._queues: dict[str, simpy.Store] = {
            n: simpy.Store(self.env) for n in self.cells
        }
        self._progress: dict[str, int] = {n: 0 for n in self.cells}
        self._faulted: set[str] = set()

        for node_id in self.cells:
            self.env.process(self._cell_worker(node_id))
            self.env.process(self._status_ticker(node_id))
            self.env.process(self._heartbeat_ticker(node_id))
            if self.cells[node_id].fault_at is not None:
                self.env.process(self._fault_injector(node_id))

    # ── 포트 구현 ──────────────────────────────────────────────────
    def now(self) -> float:
        return float(self.env.now)

    def send(self, envelope: dict) -> None:
        node_id = (envelope.get("to") or {}).get("node_id")
        if node_id not in self._queues:
            # 모르는 노드는 조용히 버리지 않는다 — 라우터가 재배정할 수 있게 알린다
            self._emit_task_reject(envelope, reason="unknown_node")
            return
        if node_id in self._faulted:
            self._emit_task_reject(envelope, reason="cell_faulted")
            return
        self._queues[node_id].put(envelope)

    def poll(self) -> Iterable[dict]:
        out, self._outbox = self._outbox, []
        return out

    def step(self, until: float | None = None) -> None:
        if until is None:
            self.env.step() if self.env.peek() < float("inf") else None
        else:
            self.env.run(until=until)

    @property
    def finished(self) -> bool:
        return self.env.peek() == float("inf")

    # ── 내부: 셀 거동 ──────────────────────────────────────────────
    def _cell_worker(self, node_id: str):
        cell = self.cells[node_id]
        while True:
            req = yield self._queues[node_id].get()
            if node_id in self._faulted:
                self._emit_task_reject(req, reason="cell_faulted")
                continue

            payload = req.get("payload") or {}
            task_id = payload.get("task_id")
            duration = float(payload.get("expected_duration_s") or cell.nominal_cycle_time_s)
            if cell.duration_jitter:
                duration *= 1.0 + self.rng.uniform(-cell.duration_jitter, cell.duration_jitter)
            # v2.2 — 응답 4종의 최종 수신자는 **원 지시의 발행 주체**(직접 회신 구성)
            reply_to = self._reply_target(req)

            self._emit_to(
                node_id,
                "task_accept",
                {
                    "task_id": task_id,
                    "accepted": True,
                    "estimated_start_iso": self.factory.iso(self.env.now),
                    "estimated_finish_iso": self.factory.iso(self.env.now + duration),
                },
                to=reply_to,
                correlation_id=req.get("correlation_id"),
            )

            self._state[node_id] = "running"
            start = self.env.now
            # 진행률 갱신을 위해 10 등분해서 진행한다
            for i in range(10):
                yield self.env.timeout(duration / 10.0)
                self._progress[node_id] = (i + 1) * 10
                if node_id in self._faulted:
                    break

            if node_id in self._faulted:
                self._emit_to(
                    node_id,
                    "task_failed",
                    {
                        "task_id": task_id,
                        "failure_reason": "cell fault during execution",
                        "recoverable": True,
                        "failed_step": payload.get("step_id", "unknown"),
                    },
                    to=reply_to,
                    correlation_id=req.get("correlation_id"),
                )
            else:
                self._emit_to(
                    node_id,
                    "task_complete",
                    {
                        "task_id": task_id,
                        "kpi_summary": {"actual_duration_s": round(self.env.now - start, 3)},
                        "output_artifact_id": f"ART-{node_id}-{task_id[:8] if task_id else '0'}",
                    },
                    to=reply_to,
                    correlation_id=req.get("correlation_id"),
                )
            self._state[node_id] = "idle"
            self._progress[node_id] = 0

    def _status_ticker(self, node_id: str):
        cell = self.cells[node_id]
        while True:
            yield self.env.timeout(self.status_period_s)
            self._emit_to(
                node_id,
                "cell_status",
                {
                    "cell_id": node_id,
                    "owner_subtask": cell.owner_subtask,
                    "state": self._state[node_id],
                    "handling_mode": "autonomous",
                    "timestamp_iso": self.factory.iso(self.env.now),
                    "progress_pct": self._progress[node_id],
                    "queue_depth": len(self._queues[node_id].items),
                },
                to=("router", "nuc-01"),
            )

    def _heartbeat_ticker(self, node_id: str):
        while True:
            yield self.env.timeout(self.heartbeat_period_s)
            self._emit_to(
                node_id,
                "heartbeat",
                {
                    "node_id": node_id,
                    "uptime_s": int(self.env.now),
                    "health_state": "degraded" if node_id in self._faulted else "ok",
                },
                to=("router", "nuc-01"),
            )

    def _fault_injector(self, node_id: str):
        yield self.env.timeout(self.cells[node_id].fault_at)
        self._faulted.add(node_id)
        self._state[node_id] = "error"
        self._emit(
            node_id,
            "event",
            {
                "timestamp": self.factory.iso(self.env.now),
                "equip_id": node_id,
                "code": 4099,
                "severity": "ERROR",
                "meaning": "셀 고장 (시나리오 주입)",
                "category": "S2_welding",
                "source": "simulated",
            },
        )

    # ── 내부: 봉투 발행 ────────────────────────────────────────────
    def _emit(self, node_id: str, message_type: str, payload: dict, **kw) -> None:
        self._emit_to(node_id, message_type, payload, to=("fdw-ops", "ops-01"), **kw)

    def _emit_to(
        self, node_id: str, message_type: str, payload: dict, *, to, **kw
    ) -> None:
        self._outbox.append(
            self.factory.make(
                message_type=message_type,
                frm=("cell", node_id),
                to=to,
                payload=payload,
                at=self.env.now,
                **kw,
            )
        )

    @staticmethod
    def _reply_target(req: dict) -> tuple[str, str | None]:
        """응답은 원 지시의 `from` 으로 보낸다 (직접 회신 구성). 없으면 ops-01."""
        src = req.get("from") or {}
        if src.get("system") and src.get("node_id"):
            return (src["system"], src["node_id"])
        return ("fdw-ops", "ops-01")

    def _emit_task_reject(self, req: dict, *, reason: str) -> None:
        node_id = (req.get("to") or {}).get("node_id") or "unknown"
        payload = req.get("payload") or {}
        self._outbox.append(
            self.factory.make(
                message_type="task_reject",
                frm=("cell", node_id if node_id in self.cells else "welding_cell_01"),
                to=self._reply_target(req),
                payload={
                    "task_id": payload.get("task_id"),
                    "accepted": False,
                    "reason": reason,
                },
                at=self.env.now,
                correlation_id=req.get("correlation_id"),
            )
        )
