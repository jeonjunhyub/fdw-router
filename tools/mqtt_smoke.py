# -*- coding: utf-8 -*-
"""실제 MQTT 브로커 위에서 라우터 노드 ↔ 셀 ↔ 오케스트레이터 스텁 왕복 스모크.

    uv run --python 3.11 --with paho-mqtt --with amqtt --with jsonschema --with referencing \
        --with rfc3339-validator python -I tools/mqtt_smoke.py [--host 127.0.0.1 --port 1883] [--embedded]

--embedded : amqtt 브로커를 이 프로세스 안에서 띄운다 (Mosquitto 가 없을 때). MQTT 3.1.1.
결과: 중계 1건(cell_status → ops-01), 역중계 1건(capability join → router), 2015 1건, task_request 1건.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fdw_router.integrations.orchestrator_stub import OrchestratorStub  # noqa: E402
from fdw_router.msg import build  # noqa: E402
from fdw_router.transport.mqtt import MqttTransport  # noqa: E402
from fdw_router.transport.router_node import RouterNode  # noqa: E402


def start_embedded_broker(port: int) -> threading.Thread:
    from amqtt.broker import Broker

    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
           "sys_interval": 0, "auth": {"allow-anonymous": True}, "topic-check": {"enabled": False}}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def boot():
            broker = Broker(cfg)          # 실행 중인 루프 안에서 만들어야 한다 (amqtt 0.12)
            await broker.start()
            return broker

        loop.run_until_complete(boot())
        loop.run_forever()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(1.5)
    return th


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--embedded", action="store_true")
    a = ap.parse_args()
    if a.embedded:
        start_embedded_broker(a.port)

    t0 = time.monotonic()
    now = lambda: time.monotonic() - t0  # noqa: E731
    node = RouterNode(MqttTransport(a.host, a.port, client_id="fdw-router-nuc-01"), now=now, run_id="smoke")
    stub = OrchestratorStub(MqttTransport(a.host, a.port, client_id="fdw-ops-ops-01-stub"), now=now, run_id="smoke-stub")
    cell_t = MqttTransport(a.host, a.port, client_id="welding_cell_01")
    node.start(); stub.start(); cell_t.start()
    cell = build.EnvelopeFactory(run_id="smoke-cell")

    def status(at):
        return cell.make(message_type="cell_status", frm=("cell", "welding_cell_01"), to=("router", "nuc-01"), at=at,
                         payload={"cell_id": "welding_cell_01", "owner_subtask": "S2", "state": "idle",
                                  "handling_mode": "autonomous", "timestamp_iso": cell.iso(at)})

    cell_t.publish(status(0.0))                                   # 1) 중계 기대
    bad = status(0.1); del bad["payload"]["state"]; cell_t.publish(bad)   # 2) 2015 기대
    cap = cell.make(message_type="cell_capability", frm=("cell", "waam_cell_01"), to=("fdw-ops", "ops-01"), at=0.2,
                    payload={"cell_id": "waam_cell_01", "owner_subtask": "S4", "announce_reason": "join",
                             "process_type": "waam", "supported_task_types": ["waam_layer_deposit"],
                             "timestamp_iso": cell.iso(0.2)})
    cell_t.publish(cap)                                           # 3) 역중계 기대
    node.issue_task_request("welding_cell_01", {"task_id": "t-1", "step_id": "weld_arc_pass1",
                                                 "params": {"weld_arc_current": 220}, "expected_duration_s": 30.0})
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (node.stats.relayed >= 1 and stub.log.relayed_out and stub.log.events):
        time.sleep(0.1)
    print("router stats :", node.stats)
    print("stub relayed_in :", len(stub.log.relayed_in), "| relayed_out :", len(stub.log.relayed_out),
          "| events :", [e["payload"]["code"] for e in stub.log.events])
    print("router capabilities :", list(node.capabilities))
    ok = node.stats.relayed >= 1 and len(stub.log.relayed_out) == 1 and 2015 in [e["payload"]["code"] for e in stub.log.events]
    print("RESULT:", "OK" if ok else "FAIL")
    node.stop(); stub.stop(); cell_t.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
