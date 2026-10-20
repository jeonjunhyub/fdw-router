# -*- coding: utf-8 -*-
"""RouterOrchestrator — fdw_sim `FDWOrchestrator` 의 자리에 라우터 J축을 꽂는다.

fdw-ops 의 룰 기반 오케스트레이터는 "이후 PoC-2/3에서 동일 인터페이스를 유지한 채
AI 라우팅 모듈로 교체 가능"하도록 슬롯을 남겨 두었다. 이 클래스가 그 교체물이다.

바꾸는 것 — **딱 하나**: 다음 공정을 수행할 셀을 정적 표(`process_to_cell`)에서
찾는 대신, 살아 있는 `CellStatusMessage` 로 후보를 만들고

    실행가능성 필터(core.feasibility) → ATCS 지수(core.priority) → 셀 선택(selector 또는 최소부하)

로 고른다. 결정마다 JSON Lines 결정 로그를 남긴다(`logs/decisions_<run>.jsonl`).

바꾸지 않는 것: 격리(quarantine)·최종 출하(final takeout)·KPI 발행·Job 추적 의미.
KPI 를 원본과 나란히 비교할 수 있어야 하므로 그대로 상속한다.

배정이 불가능하면(가용 셀 없음 / 후보 소진 / 재배정 한도 초과 — v2.2 §3.2c 의 2014 조건 3종)
`Topics.FAULT` 에 코드 2014 통지를 발행하고 Job 은 활성 상태로 둔다. 죽지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...adapters.fdwops_cell import (
    CELL_PROCESS_TYPE,
    ORDER_PRIORITY_WEIGHT,
    PRIORITY_TO_ORDER_PRIORITY,
    ROUTE_TO_STEP_PROCESS_TYPE,
    STATE_MAP,
    ensure_fdw_ops_importable,
    make_task_id,
)
from ...config import PROCESS_TYPE_MAP
from ...core import feasibility, priority
from ...core.model import Cell, CellStatus, Job, Operation, ShopState

ensure_fdw_ops_importable()

from fdw_sim.messaging.bus import MessageBus, Topics  # type: ignore  # noqa: E402
from fdw_sim.messaging.schemas import (  # type: ignore  # noqa: E402
    CellState,
    CellStatusMessage,
    DispatchCommand,
    JobSpec,
    KPIRecord,
    MaterialTransferCommand,
)
from fdw_sim.orchestrator.orchestrator import (  # type: ignore  # noqa: E402
    FDWOrchestrator,
    JobTracker,
    OrchestratorConfig,
)

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).resolve().parent / "logs"

# 라우터 J축 셀 선택기 — 다른 작업자가 동시에 만드는 중이므로 있으면 쓰고 없으면 내장 비용으로 간다.
try:  # pragma: no cover - 존재 여부는 실행 시점에 결정
    from ...core import selector as _selector  # type: ignore
except Exception:  # noqa: BLE001
    _selector = None


# ── 결정 기록 ────────────────────────────────────────────────────────


@dataclass
class Decision:
    job_id: str
    task_id: str
    route_index: int
    process: str
    sim_time: float
    candidates: List[str]
    excluded: Dict[str, str]
    chosen: Optional[str]
    chosen_by: str                      # "selector" | "min_load" | "none"
    cost_components: Dict[str, Dict[str, float]]
    atcs: Dict[str, float]
    reason: str = ""
    decision_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "task_id": self.task_id, "route_index": self.route_index,
            "process": self.process, "sim_time": round(self.sim_time, 3),
            "candidates": self.candidates, "excluded": self.excluded,
            "chosen": self.chosen, "chosen_by": self.chosen_by,
            "cost_components": self.cost_components, "atcs": self.atcs,
            "reason": self.reason, "decision_ms": round(self.decision_ms, 3),
        }


@dataclass
class RouterOrchestratorStats:
    decisions: int = 0
    replanning_events: int = 0          # 2014 발행 수
    selector_used: int = 0
    min_load_used: int = 0
    log_path: Optional[Path] = None


# ── 오케스트레이터 ───────────────────────────────────────────────────


class RouterOrchestrator(FDWOrchestrator):
    """`FDWOrchestrator` 드롭인. 셀 선택만 라우터 J축으로 바꾼다."""

    REPLAN_CODE = 2014

    def __init__(self, bus: MessageBus, config: Optional[OrchestratorConfig] = None, *,
                 decide: Optional[Callable[["RouterOrchestrator", JobTracker, str], Optional[str]]] = None,
                 run_name: Optional[str] = None, log_dir: Path = LOG_DIR,
                 max_reassign: int = 3) -> None:
        super().__init__(bus, config)
        self._decide_override = decide
        self.stats = RouterOrchestratorStats()
        self.decisions: List[Decision] = []
        self.max_reassign = max_reassign
        # (job_id, route_index) → 배정된 셀. 정적 표를 대신한다.
        self._assigned: Dict[tuple, str] = {}
        self._reassign_count: Dict[tuple, int] = {}
        self._replanned: set = set()
        self._cell_types: Dict[str, str] = {}          # cell_id → cell_type.value
        self._cell_cycle: Dict[str, float] = {}         # cell_id → 공지된 사이클 (있으면)
        log_dir.mkdir(parents=True, exist_ok=True)
        run = run_name or time.strftime("run_%Y%m%d_%H%M%S")
        self.stats.log_path = log_dir / f"decisions_{run}.jsonl"
        self._log_fh = open(self.stats.log_path, "a", encoding="utf-8")

    # ── 셀 등록: 능력 공지 대신 셀 타입을 기억한다 ─────────────────
    def register_cell(self, cell: Any) -> None:
        super().register_cell(cell)
        ctype = getattr(getattr(cell, "cell_type", None), "value", None) or str(getattr(cell, "cell_type", ""))
        self._cell_types[cell.cell_id] = ctype
        cyc = getattr(cell, "default_cycle_time", None)
        if isinstance(cyc, (int, float)) and cyc > 0:
            self._cell_cycle[cell.cell_id] = float(cyc)

    def _on_cell_status(self, msg: CellStatusMessage) -> None:
        super()._on_cell_status(msg)
        self._cell_types.setdefault(msg.cell_id, msg.cell_type.value)

    # ── 정적 표 대신 배정 결과를 조회한다 ──────────────────────────
    def _target_for(self, tracker: JobTracker, route_index: int) -> Optional[str]:
        return self._assigned.get((tracker.job.job_id, route_index))

    def _lookup_next_cell(self, tracker: JobTracker) -> Optional[str]:
        next_idx = tracker.route_index + 1
        if next_idx >= len(tracker.job.process_route):
            return None
        return self._assigned.get((tracker.job.job_id, next_idx))

    # ── 의사결정 루프 (원본과 같은 순서, 대상 셀만 배정 결과로) ───────
    def _tick_decisions(self) -> None:
        finished: List[str] = []

        # 우선순위 반영: 가중치 큰 작업이 먼저 자원을 잡는다 (동점은 투입 순서, job_id).
        order = sorted(self.active_jobs.items(),
                       key=lambda kv: (-self._weight(kv[1].job), kv[1].started_at, kv[0]))
        for job_id, tracker in order:
            if tracker.is_done:
                tracker.completed_at = self._sim_time
                tracker.disposition = "shipped"
                self.completed_jobs.append(tracker)
                finished.append(job_id)
                self._publish_job_completed(tracker)
                continue

            current_proc = tracker.next_process
            target_cell_id = self._target_for(tracker, tracker.route_index)
            if target_cell_id is None:
                # 아직 배정되지 않았다 (2014 뒤 재시도 포함). 다시 시도한다.
                self._issue_next_transfer(tracker)
                target_cell_id = self._target_for(tracker, tracker.route_index)
                if target_cell_id is None:
                    continue

            target_state = self.cell_state_cache.get(target_cell_id)

            # 1) 부품이 target cell 의 input_buffer 에 도착했으면 dispatch
            if target_state and target_state.input_buffer.occupied \
                    and target_state.input_buffer.part_id == tracker.job.part_id \
                    and target_state.placement_ready \
                    and target_state.state in (CellState.READY, CellState.IDLE):
                if self._dispatched_jobs_at_cell.get(target_cell_id) != job_id:
                    if self._issue_dispatch(tracker, target_cell_id, current_proc):
                        self._dispatched_jobs_at_cell[target_cell_id] = job_id
                continue

            # 2) target cell 이 작업을 끝내고 output_buffer 에 부품이 있으면 → 다음 단계로 이송
            if target_state and target_state.output_buffer.occupied \
                    and target_state.output_buffer.part_id == tracker.job.part_id:
                tracker.last_step_completed = current_proc
                tracker.history.append(current_proc)
                tracker.current_location = target_cell_id
                self._dispatched_jobs_at_cell.pop(target_cell_id, None)

                is_inspection = (current_proc == "inspection"
                                 or self._cell_types.get(target_cell_id) == "inspection")
                if is_inspection:
                    verdict = target_state.inspection_verdict
                    if target_state.verdict_part_id != tracker.job.part_id:
                        verdict = None
                    if verdict != "PASS":
                        reason = target_state.verdict_reason or verdict or "missing_or_mismatched_verdict"
                        if self._quarantine(tracker, target_cell_id, reason):
                            finished.append(job_id)
                        else:
                            tracker.history.pop()
                        continue

                if tracker.route_index + 1 == len(tracker.job.process_route):
                    if not self._final_takeout(target_cell_id, tracker.job.part_id):
                        tracker.history.pop()
                        continue
                tracker.route_index += 1
                if not tracker.is_done:
                    self._issue_next_transfer(tracker)

        for jid in finished:
            self.active_jobs.pop(jid, None)

    # ── 이송 발행: 여기서 라우터가 셀을 고른다 ─────────────────────
    def _issue_next_transfer(self, tracker: JobTracker) -> None:
        if tracker.is_done:
            return
        next_proc = tracker.next_process
        key = (tracker.job.job_id, tracker.route_index)
        target_cell = self._assigned.get(key)
        if target_cell is None:
            target_cell = self._decide(tracker, next_proc)
            if target_cell is None:
                return                      # 2014 발행됨. Job 은 활성 유지, 다음 틱에 재시도.
            self._assigned[key] = target_cell

        from_cell = tracker.current_location or self.config.material_cell_id
        tkey = (tracker.job.job_id, tracker.route_index, from_cell, target_cell)
        if tkey in self._transferred_keys:
            return
        self._transferred_keys.add(tkey)

        cmd = MaterialTransferCommand(
            from_cell=from_cell,
            to_cell=target_cell,
            part_id=tracker.job.part_id,
            carrier="AUTO",
            priority=tracker.job.priority,
            command_id=f"MOVE_{make_task_id(tracker.job.job_id, tracker.route_index, next_proc)}",
        )
        self.bus.publish(Topics.MATERIAL_TRANSFER, cmd)
        logger.info("[ROUTER] transfer issued: %s %s -> %s (job=%s)",
                    cmd.command_id, from_cell, target_cell, tracker.job.job_id)

    def _issue_dispatch(self, tracker: JobTracker, cell_id: str, process: str) -> bool:
        cell = self.cell_registry.get(cell_id)
        if cell is not None and not cell.is_part_ready(tracker.job.part_id):
            return False
        operation = self.config.process_to_operation.get(process, "START")
        recipe = tracker.job.recipe_overrides.get(process, {})
        cmd = DispatchCommand(
            target_cell=cell_id,
            job_id=tracker.job.job_id,
            operation=operation,
            recipe=recipe,
            next_cell=self._lookup_next_cell(tracker),
            priority=tracker.job.priority,
            part_id=tracker.job.part_id,
            command_id=f"CMD_{make_task_id(tracker.job.job_id, tracker.route_index, process)}",
        )
        self.bus.publish(Topics.DISPATCH_COMMAND, cmd)
        if cell is not None and cell.current_command is not cmd:
            return False
        logger.info("[ROUTER] dispatch issued: %s -> %s op=%s (job=%s)",
                    cmd.command_id, cell_id, operation, tracker.job.job_id)
        return True

    # ── J축: 후보 → 필터 → 순위 → 선택 ─────────────────────────────
    @staticmethod
    def _weight(job: JobSpec) -> float:
        op = PRIORITY_TO_ORDER_PRIORITY.get(str(job.priority).lower(), "standard")
        return ORDER_PRIORITY_WEIGHT[op]

    def _shop_state(self, process: str) -> ShopState:
        """살아 있는 셀 상태로 J축 입력(ShopState)을 만든다."""
        shop = ShopState(now_s=self._sim_time)
        for cell_id, ctype in sorted(self._cell_types.items()):
            want = CELL_PROCESS_TYPE.get(ctype, ctype)
            cell = Cell(
                cell_id=cell_id,
                owner_subtask={"welding": "S2", "forming": "S3", "waam": "S4",
                               "machining": "S5", "inspection": "S5", "material": "S6"}.get(ctype, "S1"),
                process_type=want,
                nominal_cycle_time_s=self._cell_cycle.get(cell_id, 60.0),
            )
            st = self.cell_state_cache.get(cell_id)
            if st is None:
                # 아직 상태 통지를 한 번도 받지 못한 셀 (투입 직후). 원본 오케스트레이터와 같이
                # 등록된 셀 객체의 상태머신을 본다 — 부팅된 셀은 idle 이다.
                cell_obj = self.cell_registry.get(cell_id)
                fsm = getattr(cell_obj, "fsm", None)
                raw = getattr(getattr(fsm, "state", None), "value", None) if fsm is not None else None
                state = STATE_MAP.get(raw, "idle") if raw else "idle"
                status = CellStatus(cell_id=cell_id, state=state, available=state not in ("error", "offline"))
            else:
                state = STATE_MAP.get(st.state.value, "error")
                running = (st.current_job_id,) if st.state == CellState.PROCESSING and st.current_job_id else ()
                status = CellStatus(
                    cell_id=cell_id, state=state,
                    queue_depth=int(getattr(st.input_buffer, "queue_length", 0) or 0),
                    running_op_ids=tuple(running),
                    remaining_s=float(st.estimated_remaining_time_sec or 0.0),
                    available=state not in ("error", "offline"),
                )
            # 라우터가 이미 그 셀에 배정했지만 아직 도착·착수하지 않은 작업도 부하다.
            # (같은 틱에 여러 오더를 배정할 때 셀 상태 통지만 보면 전부 "비어 있음"으로 보인다.)
            pending = self._pending_assigned(cell_id, st)
            if pending:
                status = CellStatus(cell_id=status.cell_id, state=status.state, queue_depth=status.queue_depth + pending,
                                    running_op_ids=status.running_op_ids, remaining_s=status.remaining_s, available=status.available)
            shop.add_cell(cell, status)
        return shop

    def _pending_assigned(self, cell_id: str, st: Any) -> int:
        """`cell_id` 에 배정됐지만 아직 그 셀에서 돌고 있지 않은 (job, step) 수."""
        running = getattr(st, "current_job_id", None) if st is not None else None
        n = 0
        for (job_id, idx), cid in self._assigned.items():
            if cid != cell_id:
                continue
            tracker = self.active_jobs.get(job_id)
            if tracker is None or tracker.is_done or idx < tracker.route_index:
                continue                      # 끝났거나 이미 지난 단계
            if idx == tracker.route_index and running == job_id:
                continue                      # 지금 그 셀에서 돌고 있다 — 상태 통지가 이미 센다
            n += 1
        return n

    def _operation(self, tracker: JobTracker, process: str) -> Operation:
        """fdw-ops 공정 이름을 규격 어휘의 `Operation` 으로."""
        want_cell_type = CELL_PROCESS_TYPE.get(process, process)
        # PROCESS_TYPE_MAP 에서 원하는 셀 어휘로 가는 키를 찾는다 (config 가 바뀌어도 따라간다)
        step_pt = ROUTE_TO_STEP_PROCESS_TYPE.get(process, process)
        if PROCESS_TYPE_MAP.get(step_pt) != want_cell_type:
            for k, v in PROCESS_TYPE_MAP.items():
                if v == want_cell_type:
                    step_pt = k
                    break
        return Operation(
            op_id=make_task_id(tracker.job.job_id, tracker.route_index, process),
            step_id=self.config.process_to_operation.get(process, f"START_{process.upper()}"),
            process_type=step_pt,
            params=dict(tracker.job.recipe_overrides.get(process, {})),
            expected_duration_s=None,
        )

    def _job(self, tracker: JobTracker) -> Job:
        ops: Dict[str, Operation] = {}
        prev = None
        for i, proc in enumerate(tracker.job.process_route):
            op_id = make_task_id(tracker.job.job_id, i, proc)
            step_pt = ROUTE_TO_STEP_PROCESS_TYPE.get(proc, proc)
            ops[op_id] = Operation(op_id=op_id, step_id=self.config.process_to_operation.get(proc, "START"),
                                   process_type=step_pt, requires=(prev,) if prev else (),
                                   params=dict(tracker.job.recipe_overrides.get(proc, {})))
            prev = op_id
        n = max(1, len(tracker.job.process_route))
        return Job(job_id=tracker.job.job_id, product_id=tracker.job.part_type, operations=ops,
                   due_s=tracker.started_at + 180.0 * n,      # 납기 정보가 fdw-ops 에 없다 — 공정당 3분 잠정
                   weight=self._weight(tracker.job), released_s=tracker.started_at)

    def _decide(self, tracker: JobTracker, process: str) -> Optional[str]:
        if self._decide_override is not None:
            return self._decide_override(self, tracker, process)

        t0 = time.perf_counter()
        key = (tracker.job.job_id, tracker.route_index)
        task_id = make_task_id(tracker.job.job_id, tracker.route_index, process)
        shop = self._shop_state(process)
        job = self._job(tracker)
        op = self._operation(tracker, process)
        done = {make_task_id(tracker.job.job_id, i, p) for i, p in enumerate(tracker.job.process_route)
                if i < tracker.route_index}
        shop.done[job.job_id] = done
        want = CELL_PROCESS_TYPE.get(process, process)

        # ① 자격 셀 (상태 무시) — 2014 조건 ⓐ 판정용
        capable = [c for c in shop.cells.values() if c.process_type == want]
        # ② 하드 제약 통과 셀
        fs = feasibility.feasible_cells(job, op, shop)
        excluded = {r.cell_id: f"{r.reason.value}: {r.detail}".strip(": ") for r in fs.rejections}
        strict = list(fs.cells)
        # ③ 완화 집합: 자격 셀 중 error/offline 이 아닌 것 (부품을 보내 두고 기다릴 수 있는 셀)
        relaxed = [c for c in capable if shop.status_of(c.cell_id).available]

        atcs: Dict[str, float] = {}
        if strict:
            for r in priority.rank(job, op, strict, shop):
                atcs[r.cell_id] = r.index
        pool = strict or relaxed
        chosen, chosen_by, components = None, "none", {}
        if pool:
            chosen, chosen_by, components = self._select(job, op, pool, shop)

        reason = ""
        if chosen is None:
            if not capable:
                reason = f"2014-a 가용 셀 없음: process_type={want} 를 수행할 셀이 없음"
            elif not relaxed:
                reason = f"2014-a 가용 셀 없음: 자격 셀 {[c.cell_id for c in capable]} 전부 error/offline"
            else:  # pragma: no cover - pool 이 있으면 chosen 이 있다
                reason = "2014-b 후보 소진"
            self._reassign_count[key] = self._reassign_count.get(key, 0) + 1
            if self._reassign_count[key] > self.max_reassign:
                reason += f" / 2014-c 재배정 한도 초과({self.max_reassign})"
            self._emit_replan(tracker, task_id, process, reason, excluded)

        dec = Decision(
            job_id=tracker.job.job_id, task_id=task_id, route_index=tracker.route_index,
            process=process, sim_time=self._sim_time,
            candidates=[c.cell_id for c in pool], excluded=excluded, chosen=chosen, chosen_by=chosen_by,
            cost_components=components, atcs=atcs, reason=reason,
            decision_ms=(time.perf_counter() - t0) * 1000.0,
        )
        self._record(dec)
        return chosen

    def _select(self, job: Job, op: Operation, pool: List[Cell], shop: ShopState):
        """셀 선택. `core.selector` 가 있으면 그것, 없으면 최소부하 비용."""
        if _selector is not None:
            for fn_name in ("select_cell", "select", "choose"):
                fn = getattr(_selector, fn_name, None)
                if not callable(fn):
                    continue
                try:
                    res = fn(job, op, pool, shop)
                    cell_id = getattr(res, "cell_id", None) or (res if isinstance(res, str) else None)
                    if cell_id in {c.cell_id for c in pool}:
                        self.stats.selector_used += 1
                        # 결정 로그에는 후보 전원의 비용 성분을 남긴다 ("왜 그 셀이 아니었나"에 답하기 위해)
                        ranked = _selector.rank_cost(job, op, pool, shop) if hasattr(_selector, "rank_cost") else (res,)
                        comps_all = {}
                        for r in ranked:
                            d = getattr(r, "__dict__", {})
                            comps_all[r.cell_id] = {k: float(v) for k, v in dict(d).items() if isinstance(v, (int, float))}
                        return cell_id, f"selector.{fn_name}", comps_all
                except Exception as e:  # noqa: BLE001 - API 가 아직 맞지 않으면 내장 비용으로
                    logger.debug("[ROUTER] selector.%s 사용 불가(%s) — 최소부하로 대체", fn_name, e)
                break
        comps: Dict[str, Dict[str, float]] = {}
        best, best_cost = None, None
        for c in sorted(pool, key=lambda c: c.cell_id):
            st = shop.status_of(c.cell_id)
            wait = float(st.remaining_s) + st.queue_depth * c.nominal_cycle_time_s
            busy = 0.0 if st.state == "idle" else c.nominal_cycle_time_s
            cost = wait + busy
            comps[c.cell_id] = {"wait_s": wait, "busy_penalty_s": busy, "cost": cost}
            if best_cost is None or cost < best_cost:
                best, best_cost = c.cell_id, cost
        self.stats.min_load_used += 1
        return best, "min_load", comps

    # ── 2014 통지 ───────────────────────────────────────────────────
    def _emit_replan(self, tracker: JobTracker, task_id: str, process: str, reason: str,
                     excluded: Dict[str, str]) -> None:
        key = (tracker.job.job_id, tracker.route_index)
        first = key not in self._replanned
        self._replanned.add(key)
        if not first:
            return                      # 같은 작업에는 1회만 통지한다 (반복 억제)
        self.stats.replanning_events += 1
        notice = {
            "code": self.REPLAN_CODE, "category": "comm", "severity": "WARN",
            "meaning": "라우팅 계층 재계획 필요 감지",
            "job_id": tracker.job.job_id, "task_id": task_id, "process": process,
            "reason": reason, "excluded": excluded, "sim_time": self._sim_time,
            "source": "router",
        }
        self.bus.publish(Topics.FAULT, notice)
        self.bus.publish(Topics.KPI_LOG, KPIRecord(
            timestamp=time.time(), cell_id=None, metric="router_replan_2014_count",
            value=1.0, unit="events", tags={"job_id": tracker.job.job_id, "process": process}))
        logger.warning("[ROUTER] 2014 재계획 필요: job=%s process=%s — %s", tracker.job.job_id, process, reason)

    # ── 결정 로그 ───────────────────────────────────────────────────
    def _record(self, dec: Decision) -> None:
        self.decisions.append(dec)
        self.stats.decisions += 1
        self._log_fh.write(json.dumps(dec.to_dict(), ensure_ascii=False) + "\n")
        self._log_fh.flush()

    def close(self) -> None:
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
