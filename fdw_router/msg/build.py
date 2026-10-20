# -*- coding: utf-8 -*-
"""T2 봉투 생성기.

봉투를 손으로 조립하면 필드를 빠뜨리기 쉽고, 빠뜨린 것은 스키마 검증에서야 드러난다.
여기서 한 번만 조립하고 만드는 쪽은 payload 만 신경 쓴다.

**결정론**: `envelope_id` 는 난수가 아니라 `uuid5(네임스페이스, 일련키)` 다.
같은 시나리오를 같은 시드로 돌리면 봉투 ID 까지 같아야 실험이 재현된다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .. import spec
from ..config import ROUTER_SYSTEM

KST = timezone(timedelta(hours=9))


def schema_version() -> str:
    """봉투에 실을 `schema_version`. **규격에서 읽는다** (v2.2 = `fdw.v1.1`)."""
    return spec.schema_version()


def __getattr__(name: str):
    # 호환: 예전 코드가 `build.SCHEMA_VERSION` 상수를 참조하던 자리.
    if name == "SCHEMA_VERSION":
        return schema_version()
    raise AttributeError(name)

#: 시뮬레이션 가상 시각 0초에 해당하는 실제 시각
SIM_EPOCH = datetime(2026, 5, 31, 9, 0, 0, tzinfo=KST)

_NS = uuid.UUID("6f1d3a2c-0000-4000-8000-000000000001")


class EnvelopeFactory:
    """한 실행(run) 안에서 봉투를 결정론적으로 찍어 낸다."""

    def __init__(self, run_id: str = "run-0", epoch: datetime | None = None):
        self.run_id = run_id
        self.epoch = epoch or SIM_EPOCH
        self._seq = 0

    # ── 식별자 ─────────────────────────────────────────────────────
    def next_id(self) -> str:
        self._seq += 1
        return str(uuid.uuid5(_NS, f"{self.run_id}:{self._seq}"))

    def task_id(self, key: str) -> str:
        return str(uuid.uuid5(_NS, f"{self.run_id}:task:{key}"))

    def correlation_id(self, key: str) -> str:
        return str(uuid.uuid5(_NS, f"{self.run_id}:corr:{key}"))

    # ── 시각 ───────────────────────────────────────────────────────
    def iso(self, sim_seconds: float) -> str:
        """가상 시각(초) → `timestamp_iso`.

        규격 부록 B.3 — 밀리초 3자리 이상 + 시간대 오프셋을 포함한다.
        """
        dt = self.epoch + timedelta(seconds=float(sim_seconds))
        return dt.isoformat(timespec="milliseconds")

    # ── 봉투 ───────────────────────────────────────────────────────
    def make(
        self,
        *,
        message_type: str,
        frm: tuple[str, str],
        to: tuple[str, str | None],
        payload: dict,
        at: float,
        priority: str | None = None,
        correlation_id: str | None = None,
        ttl_ms: int | None = None,
    ) -> dict:
        e: dict = {
            "schema_version": schema_version(),
            "envelope_id": self.next_id(),
            "timestamp_iso": self.iso(at),
            "from": {"system": frm[0], "node_id": frm[1]},
            "to": {"system": to[0]},
            "message_type": message_type,
            "payload": payload,
        }
        if to[1] is not None:
            e["to"]["node_id"] = to[1]
        if priority is not None:
            e["priority"] = priority
        # v2.2 (요청 1 확정): 라우팅 계층이 발행하는 task_request 에는 correlation_id 필수.
        # 스키마가 강제하므로(NEG-44) 빠뜨리면 우리 봉투가 우리 검증기에서 거절된다.
        if (
            correlation_id is None
            and message_type == "task_request"
            and frm[0] == ROUTER_SYSTEM
        ):
            correlation_id = self.correlation_id(str(payload.get("task_id") or self._seq))
        if correlation_id is not None:
            e["correlation_id"] = correlation_id
        if ttl_ms is not None:
            e["ttl_ms"] = ttl_ms
        return e
