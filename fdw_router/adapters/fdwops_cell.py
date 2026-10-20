# -*- coding: utf-8 -*-
"""fdw-ops 시뮬레이터(opt1064/fdw-ops, `fdw_sim`) 브리지 — 통합 지점 ① .

`IsaacCellRuntime` 이 2차년도에 맡을 자리를 **지금 돌아가는 가상 작업장**으로 먼저 채운다.
fdw-ops 는 Isaac Sim 없이도 `--mode discrete` 로 돌아가므로, 이 어댑터를 통하면
라우터가 오늘 바로 "셀이 있는 것처럼" 봉투를 주고받을 수 있다.

변환 규칙 (순수 함수 — 단위시험 `tests/test_fdwops_integration.py`)

    fdw_sim.CellStatusMessage   →  fdw.v1.1 `cell_status` 봉투 (cell_state.schema.json)
    fdw.v1.1 `task_request` 봉투 →  fdw_sim.DispatchCommand / MaterialTransferCommand

어휘 차이는 전부 여기서 흡수한다. `core/` 와 `msg/` 는 fdw_sim 을 모른다.

    상태:  IDLE·READY→idle  PROCESSING→running  BLOCKED→blocked
           FAULT→error  RECOVERY→maintenance  OFFLINE→offline
    담당:  welding→S2  forming→S3  waam→S4  machining·inspection→S5  material→S6
    node_id: 규격 `cell_id` 패턴(^[a-z][a-z0-9_]+$)에 맞춰 소문자화 (WELDING_CELL_01 → welding_cell_01)
    task_id: "<job_id>:<route_index>:<process>"  — DispatchCommand.job_id 로 되돌릴 수 있게

fdw_sim 은 vendor 코드(`external/fdw-ops`)이며 여기서는 import 만 한다. 경로는
`FDW_OPS_ROOT` 환경변수 또는 저장소 옆 `external/fdw-ops` 에서 찾는다.
"""
from __future__ import annotations

import os
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import ROUTER_NODE_ID, ROUTER_SYSTEM
from ..msg.build import EnvelopeFactory
from ..ports.cell_runtime import CellRuntime

# ── fdw-ops 위치 ────────────────────────────────────────────────────


def fdw_ops_root() -> Path:
    """`external/fdw-ops` 의 위치. `FDW_OPS_ROOT` 가 있으면 그것을 쓴다."""
    env = os.environ.get("FDW_OPS_ROOT")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]          # fdw-router/
    return repo.parent / "external" / "fdw-ops"


def ensure_fdw_ops_importable() -> Path:
    root = fdw_ops_root()
    if not (root / "fdw_sim").is_dir():
        raise FileNotFoundError(
            f"fdw-ops 소스를 찾지 못했습니다: {root}  (FDW_OPS_ROOT 환경변수로 지정)"
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


# ── 순수 변환 ───────────────────────────────────────────────────────

#: fdw_sim.CellState.value → cell_state.schema.json `state` (7종)
STATE_MAP: dict[str, str] = {
    "IDLE": "idle",
    "READY": "idle",
    "PROCESSING": "running",
    "BLOCKED": "blocked",
    "FAULT": "error",
    "RECOVERY": "maintenance",
    "OFFLINE": "offline",
}

#: fdw_sim.CellType.value → owner_subtask
OWNER_SUBTASK: dict[str, str] = {
    "welding": "S2",
    "forming": "S3",
    "waam": "S4",
    "machining": "S5",
    "inspection": "S5",   # 검사 전용 셀은 1차년도 미적용(가이드라인 §5.6) — 정밀가공·검사 세부로 둔다
    "material": "S6",
}

#: fdw_sim.CellType.value → cell_capability.process_type (6종)
CELL_PROCESS_TYPE: dict[str, str] = {
    "welding": "welding",
    "forming": "forming",
    "waam": "waam",
    "machining": "machining",
    "inspection": "inspection",
    "material": "transport",
}

#: fdw-ops 공정 이름(JobSpec.process_route) → process_sequence.steps[].process_type 어휘
ROUTE_TO_STEP_PROCESS_TYPE: dict[str, str] = {
    "welding": "welding_arc",
    "forming": "forming",
    "waam": "waam",
    "machining": "machining",
    "inspection": "inspection",
    "transport": "amr_transfer",
    "material": "amr_transfer",
}

#: fdw-ops `priority` 문자열 → production_order.order_priority (v2.2, 3등급)
PRIORITY_TO_ORDER_PRIORITY: dict[str, str] = {
    "low": "standard",
    "normal": "standard",
    "high": "expedited",
    "critical": "rush",
}

#: order_priority → ATCS 가중치 w_j (회신 181 초안값)
ORDER_PRIORITY_WEIGHT: dict[str, float] = {"standard": 1.0, "expedited": 2.0, "rush": 4.0}


def node_id_of(cell_id: str) -> str:
    """fdw_sim 셀 ID → 규격 node_id/cell_id (소문자 snake_case)."""
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in cell_id)
    if not out or not out[0].isalpha():
        out = "cell_" + out
    return out


def make_task_id(job_id: str, route_index: int, process: str) -> str:
    return f"{job_id}:{route_index}:{process}"


def parse_task_id(task_id: str) -> tuple[str, int, str]:
    job_id, idx, process = task_id.split(":", 2)
    return job_id, int(idx), process


def cell_status_payload(msg: Any, sim_time: float, factory: EnvelopeFactory) -> dict:
    """`CellStatusMessage` → cell_state.schema.json 본문."""
    state_raw = msg.state.value if hasattr(msg.state, "value") else str(msg.state)
    ctype = msg.cell_type.value if hasattr(msg.cell_type, "value") else str(msg.cell_type)
    state = STATE_MAP.get(state_raw, "error")
    payload: dict = {
        "cell_id": node_id_of(msg.cell_id),
        "owner_subtask": OWNER_SUBTASK.get(ctype, "S1"),
        "state": state,
        "handling_mode": "autonomous",
        "timestamp_iso": factory.iso(sim_time),
        "queue_depth": int(getattr(msg.input_buffer, "queue_length", 0) or 0),
        "health": {"available": state not in ("error", "offline")},
        "source": "simulated",
    }
    if msg.current_job_id and state not in ("idle", "offline"):
        payload["current_task_id"] = str(msg.current_job_id)
    if getattr(msg, "current_command_id", None) and state == "running":
        payload["current_step"] = str(msg.current_command_id)
    health = getattr(msg, "health", None)
    if health is not None and getattr(health, "status", "normal") != "normal" and getattr(health, "message", None):
        payload["health"]["degraded_reason"] = str(health.message)
    return payload


def cell_status_envelope(msg: Any, sim_time: float, factory: EnvelopeFactory,
                         to: tuple[str, str] = (ROUTER_SYSTEM, ROUTER_NODE_ID)) -> dict:
    """`CellStatusMessage` → fdw.v1.1 `cell_status` 봉투 (셀 → 라우터)."""
    return factory.make(
        message_type="cell_status",
        frm=("cell", node_id_of(msg.cell_id)),
        to=to,
        payload=cell_status_payload(msg, sim_time, factory),
        at=sim_time,
        priority="low",
    )


def task_request_to_command(envelope: dict, node_to_cell: dict[str, str]):
    """fdw.v1.1 `task_request` 봉투 → fdw_sim 명령.

    `to.system == "fleet"` 이면 `MaterialTransferCommand`, 그 외는 `DispatchCommand`.
    반환형은 fdw_sim 의 dataclass 이므로 fdw_sim 이 import 가능해야 한다.
    """
    ensure_fdw_ops_importable()
    from fdw_sim.messaging.schemas import DispatchCommand, MaterialTransferCommand  # type: ignore

    p = envelope["payload"]
    task_id = str(p["task_id"])
    try:
        job_id, _idx, _proc = parse_task_id(task_id)
    except ValueError:
        job_id = str(p.get("job_id", task_id))
    prio = envelope.get("priority", "normal")

    if envelope["to"]["system"] == "fleet":
        return MaterialTransferCommand(
            from_cell=node_to_cell.get(p["from_loc"], p["from_loc"]),
            to_cell=node_to_cell.get(p["to_loc"], p["to_loc"]),
            part_id=str(p["material_id"]),
            carrier=str(p.get("carrier", "AUTO")),
            priority=prio,
            command_id=f"MOVE_{task_id}",
        )
    target_node = envelope["to"]["node_id"]
    return DispatchCommand(
        target_cell=node_to_cell.get(target_node, target_node),
        job_id=job_id,
        operation=str(p.get("task_type") or p.get("step_id")),
        recipe=dict(p.get("params") or {}),
        next_cell=p.get("next_cell"),
        priority=prio,
        command_id=f"CMD_{task_id}",
        part_id=p.get("part_id") or p.get("material_id"),
    )


# ── 런타임 (포트 구현) ──────────────────────────────────────────────


@dataclass
class _Clock:
    now_fn: Callable[[], float]


class FdwOpsCellRuntime(CellRuntime):
    """fdw_sim 버스를 `CellRuntime` 포트로 감싼다.

    - `poll()`  : 버스의 `/fdw/cell_status` 를 받아 `cell_status` 봉투로 돌려준다.
    - `send()`  : `task_request` 봉투를 `DispatchCommand`/`MaterialTransferCommand` 로 발행한다.
    - `now()`   : 시뮬레이션 시각 — 포트 규칙 1 (시간은 포트에서 받는다).
    - `step()`  : 아무것도 하지 않는다. 시간은 `SimulationManager` 가 민다.
    """

    name = "fdwops"

    def __init__(self, bus: Any, *, now_fn: Callable[[], float], run_id: str = "fdwops"):
        ensure_fdw_ops_importable()
        from fdw_sim.messaging.bus import Topics  # type: ignore

        self.bus = bus
        self._clock = _Clock(now_fn)
        self.factory = EnvelopeFactory(run_id=run_id)
        self._out: deque[dict] = deque()
        self._node_to_cell: dict[str, str] = {}
        self._topics = Topics
        bus.subscribe(Topics.CELL_STATUS, self._on_status)

    # 셀 → 라우터
    def _on_status(self, msg: Any) -> None:
        self._node_to_cell[node_id_of(msg.cell_id)] = msg.cell_id
        self._out.append(cell_status_envelope(msg, self.now(), self.factory))

    def poll(self) -> Iterable[dict]:
        items = list(self._out)
        self._out.clear()
        return items

    # 라우터 → 셀
    def send(self, envelope: dict) -> None:
        cmd = task_request_to_command(envelope, self._node_to_cell)
        topic = (self._topics.MATERIAL_TRANSFER if envelope["to"]["system"] == "fleet"
                 else self._topics.DISPATCH_COMMAND)
        self.bus.publish(topic, cmd)

    def step(self, until: float | None = None) -> None:
        return None

    def now(self) -> float:
        return float(self._clock.now_fn())

    @property
    def finished(self) -> bool:
        return False

    @property
    def node_to_cell(self) -> dict[str, str]:
        return dict(self._node_to_cell)
