# -*- coding: utf-8 -*-
"""라우터 MQTT 노드 — nuc-01 이 전송 계층 위에서 M축을 수행한다.

    수신(fdw/v1/router/#) → 스키마 검증 → 규칙 판정(rules.evaluate, layer=router)
      → 중계본 발행 / 보류 / 폐기 → 이벤트(2011·2012·2015) 발행 → 운영 로그

J축(작업 배정)은 여기 없다. 이 노드는 봉투를 `on_delivered` 콜백으로 넘기고, J축 연동은
`core/adapt.py` 쪽이 그 콜백을 받아 한다. 전송 구현(MQTT·in-process)은 `Transport` 인터페이스로 갈아 끼운다.

이벤트 본문은 규격 `event_log.schema.json` 의 필수 항목(timestamp_iso·equip_id·code·severity·category·meaning·source)
+ 2012/2015 의 필수 진단 필드(observed_node_id / violation_path)만 싣는다. 원 봉투 전문은 싣지 않는다 (D.13).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..config import ORCHESTRATOR_NODE_ID, ORCHESTRATOR_SYSTEM, PENDING, ROUTER_NODE_ID, ROUTER_SYSTEM
from ..msg import build, envelope, rules
from .mqtt import Transport, subscription_for

log = logging.getLogger("fdw_router.node")

_EVENT_META = {
    2011: ("comm", "WARN", "라우팅 계층의 계층 내부 메시지 폐기"),
    2012: ("comm", "WARN", "미등재 노드 envelope 전달·보류 통지"),
    2013: ("comm", "WARN", "보류 envelope 기한 만료 폐기"),
    2014: ("comm", "WARN", "라우팅 계층의 재계획 필요 감지"),
    2015: ("comm", "WARN", "라우팅 계층의 규격 위반 envelope 수신"),
}


@dataclass
class NodeStats:
    received: int = 0
    delivered: int = 0
    relayed: int = 0
    discarded: int = 0
    held: int = 0
    rejected: int = 0
    duplicates: int = 0
    events: list[int] = field(default_factory=list)


class RouterNode:
    """전송 계층 위의 라우팅 계층 노드."""

    def __init__(self, transport: Transport, *, now: Callable[[], float], run_id: str = "mqtt",
                 on_delivered: Callable[[dict, rules.Decision], None] | None = None,
                 emit_events: bool = True):
        self.t = transport
        self.now = now
        self.factory = build.EnvelopeFactory(run_id=run_id)
        self.on_delivered = on_delivered
        self.emit_events = emit_events
        self.stats = NodeStats()
        self.seen: set[str] = set()
        self.held: list[dict] = []
        self.capabilities: dict[str, dict] = {}      # cell_id → 최신 cell_capability (항목 150)
        self.issued: dict[str, dict] = {}            # correlation_id → 라우터 발행 task_request (발행 대장)
        self._suppress: dict[tuple, float] = {}      # (node, type) → 마지막 2015 발행 시각
        self.log: list[dict] = []                    # 운영 로그 (메모리)

    # ── 수명 ───────────────────────────────────────────────────────
    def start(self) -> None:
        self.t.subscribe(subscription_for(ROUTER_SYSTEM), self._on_envelope)
        self.t.start()

    def stop(self) -> None:
        self.t.stop()

    # ── 발행면 (J축이 쓴다) ────────────────────────────────────────
    def issue_task_request(self, to_node: str, payload: dict, *, to_system: str = "cell") -> dict:
        e = self.factory.make(message_type="task_request", frm=(ROUTER_SYSTEM, ROUTER_NODE_ID),
                              to=(to_system, to_node), payload=payload, at=self.now())
        self.issued[e["correlation_id"]] = e
        self.t.publish(e)
        return e

    def emit_event(self, code: int, *, detail: dict | None = None, equip_id: str = ROUTER_NODE_ID) -> dict | None:
        if not self.emit_events:
            return None
        if code == 2015 and PENDING.schema_reject_event_code is None:
            return None
        cat, sev, meaning = _EVENT_META.get(code, ("comm", "WARN", f"code {code}"))
        payload = {"timestamp_iso": self.factory.iso(self.now()), "equip_id": equip_id, "code": code,
                   "severity": sev, "category": cat, "meaning": meaning, "source": "measured"}
        if detail:
            payload.update(detail)
        e = self.factory.make(message_type="event", frm=(ROUTER_SYSTEM, ROUTER_NODE_ID),
                              to=(ORCHESTRATOR_SYSTEM, ORCHESTRATOR_NODE_ID), payload=payload, at=self.now())
        ok = envelope.validate(e)
        if not ok.ok:
            # 이벤트 본문이 규격에 안 맞으면 보내지 않고 로그만 남긴다 — 2015 의 2015 재발행 금지와 같은 정신
            self._log("event_invalid", code=code, errors=ok.errors)
            return None
        self.t.publish(e)
        self.stats.events.append(code)
        return e

    # ── 수신면 ─────────────────────────────────────────────────────
    def _on_envelope(self, topic: str, e: dict) -> None:
        self.stats.received += 1
        issuer = self._resolve_issuer(e)
        d = rules.evaluate(e, layer=ROUTER_SYSTEM, router_known_binding=self._binding_of(e),
                           origin_issuer_resolved=issuer, seen_envelope_ids=self.seen)
        if d.schema_reject:
            self.stats.rejected += 1
            self._log("schema_reject", topic=topic, errors=d.schema_errors[:3])
            self._notify_2015(e, d)
            return
        if d.duplicate:
            self.stats.duplicates += 1
            self._log("duplicate", envelope_id=e.get("envelope_id"))
            return
        if e.get("message_type") == "cell_capability":
            cid = (e.get("payload") or {}).get("cell_id")
            if cid:
                self.capabilities[cid] = e["payload"]
        if d.discard:
            self.stats.discarded += 1
            self._log("discard", envelope_id=e.get("envelope_id"), message_type=e.get("message_type"))
        elif d.hold:
            self.stats.held += 1
            self.held.append({"envelope": e, "hold_start": self.now()})
            self._log("hold", envelope_id=e.get("envelope_id"), node=d.observed_node_id)
        elif d.relay:
            self.stats.relayed += 1
            self.t.publish(d.relayed_envelope)
            # 중계본은 ops-01 로 가지만 라우터도 같은 상태·능력을 **판단 입력**으로 쓴다 (J축).
            if self.on_delivered:
                self.on_delivered(e, d)
        else:
            self.stats.delivered += 1
            if self.on_delivered:
                self.on_delivered(e, d)
        for code in d.event_codes:
            detail = {}
            if code == 2012:
                if d.observed_node_id:
                    detail["observed_node_id"] = d.observed_node_id
                if d.observed_from_node_id:
                    detail["observed_from_node_id"] = d.observed_from_node_id
                if d.routed_to:
                    detail["routed_to"] = d.routed_to
            self.emit_event(code, detail=detail)

    def expire_holds(self) -> int:
        """보류 봉투의 TTL 만료 → 폐기 + 2013 (D.8). 주기적으로 호출한다."""
        from ..config import DEFAULT_HOLD_TTL_MS
        expired = 0
        keep = []
        for h in self.held:
            ttl = h["envelope"].get("ttl_ms") or DEFAULT_HOLD_TTL_MS
            if (self.now() - h["hold_start"]) * 1000.0 >= ttl:
                expired += 1
                self.emit_event(2013, detail={"held_envelope_id": h["envelope"].get("envelope_id"),
                                              "held_node_id": (h["envelope"].get("to") or {}).get("node_id")})
            else:
                keep.append(h)
        self.held = keep
        return expired

    # ── 보조 ───────────────────────────────────────────────────────
    def _resolve_issuer(self, e: dict) -> str | None:
        if e.get("message_type") not in ("task_accept", "task_reject", "task_complete", "task_failed"):
            return None
        cid = e.get("correlation_id")
        if cid and cid in self.issued:
            return ROUTER_SYSTEM
        return "unknown"   # 대장에 없다는 사실만으로 라우터 발행분으로 판단하지 않는다

    def _binding_of(self, e: dict) -> dict | None:
        cid = (e.get("payload") or {}).get("cell_id")
        cap = self.capabilities.get(cid) if cid else None
        if not cap:
            return None
        return ((cap.get("interface") or {}).get("recipient_node_binding") or None)

    def _notify_2015(self, e: dict, d: rules.Decision) -> None:
        # 억제: 발신 노드와 메시지 종류를 **모두** 확인할 수 있을 때만 그 조합으로 (D.13)
        node = (e.get("from") or {}).get("node_id") if isinstance(e.get("from"), dict) else None
        mtype = e.get("message_type")
        key = (node, mtype) if isinstance(node, str) and isinstance(mtype, str) else None
        if key is not None:
            last = self._suppress.get(key)
            if last is not None and self.now() - last < PENDING.unregistered_event_suppress_s:
                return
            self._suppress[key] = self.now()
        violation_path = d.schema_errors[0].split(":")[0] if d.schema_errors else "<root>"
        detail = {"violation_path": violation_path}
        if isinstance(e.get("envelope_id"), str):
            detail["rejected_envelope_id"] = e["envelope_id"]
        if isinstance(node, str):
            detail["rejected_from_node_id"] = node
        if isinstance(mtype, str):
            detail["rejected_message_type"] = mtype
        self.emit_event(2015, detail=detail)

    def _log(self, kind: str, **kw) -> None:
        rec = {"t": self.now(), "kind": kind, **kw}
        self.log.append(rec)
        log.info("%s %s", kind, kw)
