# -*- coding: utf-8 -*-
"""봉투 ↔ 도메인 변환 — J축(core)과 M축(msg/transport)을 잇는 **유일한** 자리.

`core/model.py`·`core/dispatcher.py` 는 봉투를 모른다. 여기서 규격 payload 를 도메인 객체로,
배정 결과를 `task_request` payload 로 바꾼다. 규격이 바뀌면 이 파일만 고친다.

    cell_capability.payload  → Cell                 (cell_from_capability)
    cell_status.payload      → Dispatcher.on_cell_state
    production_order.payload → Job (제품 공정계획 plans 로 전개)
    Assignment               → task_request.payload (assignment_payload)
    task_accept/reject/complete/failed → Dispatcher 콜백
    DelegationVerdict        → event 2014 (detail 로 운반)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from ..config import PENDING
from .decision_log import DecisionLog
from .delegation import DelegationVerdict
from .dispatcher import Assignment, Dispatcher
from .model import Cell, Job, Operation, ProcessLimit, ShopState

KST = timezone(timedelta(hours=9))
DEFAULT_EPOCH = datetime(2026, 5, 31, 9, 0, 0, tzinfo=KST)   # msg.build.SIM_EPOCH 와 같은 값 (import 하지 않는다)

RESPONSE_TYPES = ("task_accept", "task_reject", "task_complete", "task_failed")


# ── payload → 도메인 ───────────────────────────────────────────────

def cell_from_capability(payload: Mapping) -> Cell:
    cap = payload.get("capacity") or {}
    limits = tuple(
        ProcessLimit(param_name=str(l.get("param_name")), min=l.get("min"), max=l.get("max"),
                     allowed_values=tuple(l["allowed_values"]) if l.get("allowed_values") else None,
                     unit=l.get("unit"), param_role=l.get("param_role"))
        for l in (payload.get("process_limits") or []) if l.get("param_name")
    )
    return Cell(
        cell_id=str(payload["cell_id"]), owner_subtask=str(payload.get("owner_subtask", "S1")),
        process_type=str(payload["process_type"]),
        supported_task_types=tuple(payload.get("supported_task_types") or ()),
        max_concurrent_tasks=int(cap.get("max_concurrent_tasks", 1) or 1),
        nominal_cycle_time_s=float(cap.get("nominal_cycle_time_s", 60.0) or 60.0),
        process_limits=limits,
    )


def due_seconds(due_date: str, epoch: datetime = DEFAULT_EPOCH) -> float:
    """`production_order.due_date` (date 또는 datetime ISO) → 시뮬레이션 초."""
    try:
        dt = datetime.fromisoformat(due_date)
    except ValueError:
        d = date.fromisoformat(due_date)
        dt = datetime(d.year, d.month, d.day, 18, 0, 0)       # 날짜만 오면 그날 18:00 으로 본다
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=epoch.tzinfo)
    return (dt - epoch).total_seconds()


def weight_of(order_priority: str | None) -> float:
    table = getattr(PENDING, "order_priority_weights", {"standard": 1.0, "expedited": 2.0, "rush": 4.0})
    return float(table.get(order_priority or getattr(PENDING, "order_priority_default", "standard"), 1.0))


def jobs_from_production_order(payload: Mapping, plans: Mapping[str, Sequence[Operation]], *,
                               released_s: float, epoch: datetime = DEFAULT_EPOCH) -> list[Job]:
    """오더 1건 → 수량만큼의 Job. 공정계획은 `plans[product_id]` (D4 `process_sequence` 인스턴스)."""
    product = str(payload["product_id"])
    if product not in plans:
        raise KeyError(f"공정계획 없음: product_id={product}")
    ops = {op.op_id: op for op in plans[product]}
    qty = int(payload.get("quantity", 1) or 1)
    due = due_seconds(str(payload["due_date"]), epoch)
    w = weight_of(payload.get(getattr(PENDING, "order_priority_field", "order_priority")))
    order_id = str(payload["order_id"])
    return [Job(job_id=f"{order_id}#{i + 1}" if qty > 1 else order_id, product_id=product, operations=ops,
                due_s=due, weight=w, released_s=released_s) for i in range(qty)]


# ── 도메인 → payload ───────────────────────────────────────────────

def assignment_payload(a: Assignment) -> dict:
    """`task_request.payload` (공정 셀 지시형). 이송 지시형(from_loc·to_loc·material_id)은 별도."""
    return {"task_id": a.task_id, "step_id": a.step_id, "params": dict(a.params),
            "expected_duration_s": float(a.expected_duration_s)}


def delegation_event_detail(v: DelegationVerdict) -> dict:
    """2014 통지 본문 — 대상 작업은 `detail` 로 운반한다 (규격 §3.2c).

    `event_log.schema.json` 의 `detail` 은 **문자열**이므로 JSON 직렬화해 싣는다.
    수신 측(ops-01)은 `json.loads(payload["detail"])` 로 task_id·reason·후보 이력을 꺼낸다.
    """
    return {"detail": json.dumps(v.as_event_detail(), ensure_ascii=False, sort_keys=True)}


# ── 결합 ───────────────────────────────────────────────────────────

@dataclass
class JAxisBinding:
    """라우터 노드(M축) 위에 디스패처(J축)를 얹는다.

    node 는 `issue_task_request(to_node, payload)` 와 `emit_event(code, detail=)` 를 가진 객체면 된다
    (`transport.router_node.RouterNode`). 수신면은 `on_delivered(envelope, decision)` 로 받는다.
    """

    node: object
    shop: ShopState
    now: Callable[[], float]
    plans: Mapping[str, Sequence[Operation]] = field(default_factory=dict)
    log: DecisionLog | None = None
    profile: str = "balanced"
    epoch: datetime = DEFAULT_EPOCH
    dispatcher: Dispatcher = field(init=False)
    issued: dict[str, Assignment] = field(default_factory=dict)      # task_id → 마지막 발행
    unmatched: list[dict] = field(default_factory=list)              # 대장에 없는 응답 (운영 로그)

    def __post_init__(self) -> None:
        self.dispatcher = Dispatcher(self.shop, now=self.now, on_assign=self._send,
                                     on_delegate=self._delegate, profile=self.profile, log=self.log)
        setattr(self.node, "on_delivered", self.on_delivered)

    # 수신면
    def on_delivered(self, e: dict, decision=None) -> None:
        mtype = e.get("message_type")
        p = e.get("payload") or {}
        if mtype == "cell_capability":
            cell = cell_from_capability(p)
            prev = self.shop.status.get(cell.cell_id)
            self.shop.add_cell(cell, prev)
            if p.get("announce_reason") == "withdraw":
                self.shop.status_of(cell.cell_id).available = False
            self.dispatcher.reevaluate(trigger="cell_capability")
        elif mtype == "cell_status":
            self.dispatcher.on_cell_state(str(p.get("cell_id")), str(p.get("state", "idle")))
        elif mtype == "production_order":
            self.add_order(p)
        elif mtype in RESPONSE_TYPES:
            tid = p.get("task_id")
            if tid not in self.issued:
                self.unmatched.append(e)
                return
            cell = (e.get("from") or {}).get("node_id")
            if mtype == "task_accept":
                self.dispatcher.on_task_accepted(tid)
            elif mtype == "task_reject":
                self.dispatcher.on_task_rejected(tid, cell, str(p.get("reason", "rejected")))
            elif mtype == "task_complete":
                self.dispatcher.on_task_completed(tid)
            else:
                self.dispatcher.on_task_failed(tid, str(p.get("failure_reason", "task_failed")))

    def add_order(self, payload: Mapping) -> list[Job]:
        jobs = jobs_from_production_order(payload, self.plans, released_s=self.now(), epoch=self.epoch)
        rush = (payload.get("order_priority") == "rush")
        if rush:
            for j in jobs:
                self.dispatcher.add_job(j, rush=True)
        else:
            self.dispatcher.add_jobs(jobs)
        return jobs

    def tick(self) -> None:
        self.dispatcher.tick()

    # 발행면
    def _send(self, a: Assignment) -> None:
        self.issued[a.task_id] = a
        self.node.issue_task_request(a.cell_id, assignment_payload(a))

    def _delegate(self, v: DelegationVerdict) -> None:
        self.node.emit_event(v.event_code, detail=delegation_event_detail(v))
