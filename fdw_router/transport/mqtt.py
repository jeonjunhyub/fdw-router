# -*- coding: utf-8 -*-
"""MQTT 전송 어댑터 (규격 밖 — 1차년도 기본값 MQTT, 협의 문서 확인 4 / v2.2 회신 「가능」).

규격은 전송 수단과 토픽을 정하지 않는다. 여기서 정하는 것은 **우리 라우터의 토픽 규약**뿐이고,
외부 오케스트레이터(protocol v0.1)와는 어휘 대응표로 잇는다.

토픽 규약 (fdw.v1.1 봉투 = payload 전체, UTF-8 JSON)

    fdw/v1/<to.system>/<to.node_id|any>/<message_type>

- 수신 계층은 자기 system 으로 시작하는 토픽을 구독한다. 라우터: ``fdw/v1/router/#``
- 중계본은 ``to`` 가 바뀌므로 토픽도 바뀐다 (``fdw/v1/fdw-ops/ops-01/cell_status``).
- QoS 는 봉투 ``qos`` 필드가 있으면 그 값, 없으면 1. retain 은 쓰지 않는다 (상태는 10 Hz 로 다시 온다).

paho-mqtt 2.x 를 쓴다. 브로커 없이 시험하려면 ``InProcessTransport`` 를 쓴다 — 인터페이스가 같다.
MQTT 5.0 전용 기능(속성·공유 구독)은 쓰지 않는다. 로컬 개발 브로커가 amqtt(3.1.1)일 수 있기 때문이다.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

Callback = Callable[[str, dict], None]

TOPIC_ROOT = "fdw/v1"


def topic_for(envelope: dict) -> str:
    """봉투의 ``to`` 와 ``message_type`` 으로 토픽을 만든다."""
    to = envelope["to"]
    return f"{TOPIC_ROOT}/{to['system']}/{to.get('node_id') or 'any'}/{envelope['message_type']}"


def subscription_for(system: str, node_id: str | None = None) -> str:
    """계층(또는 노드)이 구독할 패턴."""
    if node_id is None:
        return f"{TOPIC_ROOT}/{system}/#"
    # 노드 지정분 + 노드 미지정(any) 둘 다 받아야 한다 → 두 패턴을 쓴다
    return f"{TOPIC_ROOT}/{system}/{node_id}/#"


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT 와일드카드(+, #) 매칭 — InProcessTransport 용."""
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(p) == len(t)


class Transport(Protocol):
    def publish(self, envelope: dict) -> str: ...
    def subscribe(self, pattern: str, callback: Callback) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@dataclass
class InProcessTransport:
    """브로커 없는 동기 전송. 시험·DES 연동용. publish 가 돌아오면 구독자가 이미 호출된 뒤다."""

    _subs: list[tuple[str, Callback]] = field(default_factory=list)
    published: list[tuple[str, dict]] = field(default_factory=list)

    def publish(self, envelope: dict) -> str:
        topic = topic_for(envelope)
        self.published.append((topic, envelope))
        for pat, cb in list(self._subs):
            if topic_matches(pat, topic):
                cb(topic, json.loads(json.dumps(envelope)))  # 복사본 전달 (수신 측 변조 차단)
        return topic

    def subscribe(self, pattern: str, callback: Callback) -> None:
        self._subs.append((pattern, callback))

    def start(self) -> None:  # 인터페이스 맞춤
        pass

    def stop(self) -> None:
        pass


class MqttTransport:
    """paho-mqtt 2.x 기반 전송. ``start()`` 가 네트워크 스레드를 띄운다."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1883, client_id: str = "fdw-router-nuc-01",
                 username: str | None = None, password: str | None = None, keepalive: int = 30):
        try:
            import paho.mqtt.client as mqtt  # noqa: WPS433 — 선택 의존성
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("paho-mqtt 가 없습니다: uv pip install paho-mqtt") from e
        self._mqtt = mqtt
        self.host, self.port, self.keepalive = host, port, keepalive
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        if username:
            self._client.username_pw_set(username, password)
        self._subs: list[tuple[str, Callback]] = []
        self._lock = threading.Lock()
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self.connected = threading.Event()

    # ── paho 콜백 ──────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        with self._lock:
            for pat, _ in self._subs:
                client.subscribe(pat, qos=1)
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            env = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return  # JSON 이 아니면 봉투가 아니다. 2015 는 스키마 검증 단계에서 낸다.
        with self._lock:
            subs = list(self._subs)
        for pat, cb in subs:
            if topic_matches(pat, msg.topic):
                cb(msg.topic, env)

    # ── Transport 인터페이스 ───────────────────────────────────────
    def publish(self, envelope: dict) -> str:
        topic = topic_for(envelope)
        qos = int(envelope.get("qos", 1)) if isinstance(envelope.get("qos", 1), int) else 1
        self._client.publish(topic, json.dumps(envelope, ensure_ascii=False).encode("utf-8"), qos=min(max(qos, 0), 2))
        return topic

    def subscribe(self, pattern: str, callback: Callback) -> None:
        with self._lock:
            self._subs.append((pattern, callback))
        if self.connected.is_set():
            self._client.subscribe(pattern, qos=1)

    def start(self, timeout_s: float = 5.0) -> None:
        self._client.connect(self.host, self.port, self.keepalive)
        self._client.loop_start()
        if not self.connected.wait(timeout_s):
            raise TimeoutError(f"MQTT 브로커 접속 실패: {self.host}:{self.port}")

    def stop(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # pragma: no cover
            pass
