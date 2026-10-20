# -*- coding: utf-8 -*-
"""오케스트레이터 스텁 (ops-01) — 외부 오케스트레이터가 붙기 전까지의 상대역.

하는 일
  1. ``fdw/v1/fdw-ops/#`` 구독. 라우터 중계본(cell_status·cell_capability)과 이벤트(2011~2015)를 받아 기록.
  2. 최초 ``cell_capability(join)`` 를 직접 받으면(셀이 fdw-ops 로 보낸 경우) **역방향 중계**(relay_to_router) — M축 규칙을
     오케스트레이션 계층(layer=fdw-ops)으로 평가해 그 결과대로 발행한다. 규칙 파일을 읽으므로 라우터와 같은 판정이다.
  3. ``send_production_order()`` 로 생산 오더를 라우터에 보낸다 (방향표상 수신 계층이 router 를 허용하지 않으면 검증기가 막는다 —
     그 경우 production_order 는 ops-01 내부에 두고 라우터에는 task 단위로 넘기는 구성이다. 결과를 ``last_validation`` 에 남긴다).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import ORCHESTRATOR_NODE_ID, ORCHESTRATOR_SYSTEM, ROUTER_NODE_ID, ROUTER_SYSTEM
from ..msg import build, envelope, rules
from .mqtt_names import *  # noqa: F401,F403 — 토픽 상수 재노출
from ..transport.mqtt import Transport, subscription_for


@dataclass
class StubLog:
    relayed_in: list[dict] = field(default_factory=list)     # 라우터가 중계해 준 봉투
    events: list[dict] = field(default_factory=list)         # 라우터 이벤트
    relayed_out: list[dict] = field(default_factory=list)    # 우리가 라우터로 역중계한 봉투
    others: list[dict] = field(default_factory=list)


class OrchestratorStub:
    def __init__(self, transport: Transport, *, now: Callable[[], float], run_id: str = "stub"):
        self.t = transport
        self.now = now
        self.factory = build.EnvelopeFactory(run_id=run_id)
        self.log = StubLog()
        self.seen: set[str] = set()
        self.last_validation: envelope.ValidationResult | None = None

    def start(self) -> None:
        self.t.subscribe(subscription_for(ORCHESTRATOR_SYSTEM), self._on_envelope)
        self.t.start()

    def stop(self) -> None:
        self.t.stop()

    def _on_envelope(self, topic: str, e: dict) -> None:
        if e.get("relayed_by") is not None:
            self.log.relayed_in.append(e)
            return
        if e.get("message_type") == "event":
            self.log.events.append(e)
            return
        d = rules.evaluate(e, layer=ORCHESTRATOR_SYSTEM, seen_envelope_ids=self.seen)
        if d.relay and d.relay_direction == "orchestration_to_router":
            self.t.publish(d.relayed_envelope)
            self.log.relayed_out.append(d.relayed_envelope)
        else:
            self.log.others.append(e)

    def send_production_order(self, order_id: str, product_id: str, quantity: int, due_date: str,
                              order_priority: str = "standard") -> dict | None:
        e = self.factory.make(message_type="production_order", frm=(ORCHESTRATOR_SYSTEM, ORCHESTRATOR_NODE_ID),
                              to=(ROUTER_SYSTEM, ROUTER_NODE_ID), at=self.now(),
                              payload={"order_id": order_id, "product_id": product_id, "quantity": quantity,
                                       "due_date": due_date, "order_priority": order_priority})
        self.last_validation = envelope.validate(e)
        if not self.last_validation.ok:
            return None
        self.t.publish(e)
        return e
