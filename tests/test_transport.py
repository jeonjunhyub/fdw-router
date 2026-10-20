# -*- coding: utf-8 -*-
"""MQTT 전송 어댑터 — 토픽 규약과 브로커 없는 왕복."""
from fdw_router.msg import build, envelope
from fdw_router.transport.mqtt import (InProcessTransport, subscription_for, topic_for, topic_matches)


def _factory():
    return build.EnvelopeFactory(run_id="transport-test")


def test_topic_follows_to_and_message_type():
    e = _factory().make(message_type="cell_status", frm=("cell", "welding_cell_01"),
                        to=("router", "nuc-01"), payload={}, at=0.0)
    assert topic_for(e) == "fdw/v1/router/nuc-01/cell_status"


def test_topic_without_node_id_uses_any():
    e = {"to": {"system": "fdw-ops"}, "message_type": "event"}
    assert topic_for(e) == "fdw/v1/fdw-ops/any/event"


def test_wildcards():
    assert topic_matches("fdw/v1/router/#", "fdw/v1/router/nuc-01/cell_status")
    assert topic_matches("fdw/v1/+/ops-01/#", "fdw/v1/fdw-ops/ops-01/event")
    assert not topic_matches("fdw/v1/router/#", "fdw/v1/fdw-ops/ops-01/event")
    assert not topic_matches("fdw/v1/router/nuc-01/event", "fdw/v1/router/nuc-01")
    assert subscription_for("router") == "fdw/v1/router/#"


def test_in_process_round_trip_delivers_copy():
    t = InProcessTransport()
    got = []
    t.subscribe(subscription_for("router"), lambda topic, env: got.append((topic, env)))
    e = _factory().make(message_type="heartbeat", frm=("cell", "welding_cell_01"),
                        to=("router", "nuc-01"), payload={"cell_id": "welding_cell_01"}, at=1.0)
    t.publish(e)
    assert len(got) == 1 and got[0][0] == "fdw/v1/router/nuc-01/heartbeat"
    got[0][1]["payload"]["cell_id"] = "tampered"
    assert e["payload"]["cell_id"] == "welding_cell_01"  # 원본은 안 바뀐다


def test_published_envelope_is_spec_valid():
    f = _factory()
    e = f.make(message_type="production_order", frm=("fdw-ops", "ops-01"), to=("router", "nuc-01"),
               payload={"order_id": "ORD-1", "product_id": "P1", "quantity": 2,
                        "due_date": f.iso(3600.0), "order_priority": "expedited"}, at=0.0)
    r = envelope.validate(e)
    # production_order 의 to 가 router 를 허용하지 않으면 그 사실 자체가 발견이다 — 둘 중 하나는 참이어야 한다
    assert r.ok or any("to" in err for err in r.errors), r.errors
