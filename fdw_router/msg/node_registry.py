# -*- coding: utf-8 -*-
"""노드 식별자 사전 (§3.2b) 과 미등재 노드 보류.

사전의 정본은 규격 `T2_guideline_v*.md` §3.2b 표다(파일명은 `00_INDEX.json` 의
`canonical_sources.common_spec` 에서 읽는다). 여기서는 그 표를 파싱해
읽는다 — 값을 손으로 옮겨 적으면 규격이 개정될 때 두 문서가 갈린다.

§3.2b 가 못박은 성격: **이 표는 「알려진 노드의 목록」이지 「허용 목록」이 아니다.**
사전에 없는 노드를 받아도 폐기하지 않는다. 자가적응 시연에서 새 셀이 붙는 경로가
이 규칙에 의존한다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from .. import spec
from ..config import DEFAULT_HOLD_TTL_MS

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|")


@lru_cache(maxsize=1)
def node_dictionary() -> dict[str, str]:
    """`node_id` -> `system`. §3.2b 표에서 파싱한다."""
    path = spec.guideline_md()
    text = path.read_text(encoding="utf-8")
    # §3.2b 절만 잘라낸다 (다음 절 §3.2c 전까지)
    start = text.find("### 3.2b")
    end = text.find("### 3.2c", start)
    if start < 0 or end < 0:
        raise spec.SpecNotFound("§3.2b 노드 식별자 사전 절을 찾지 못했습니다.")
    out: dict[str, str] = {}
    for line in text[start:end].splitlines():
        m = _ROW.match(line.strip())
        if m:
            node_id, system = m.group(1), m.group(2)
            if node_id == "node_id":       # 헤더 행 방어
                continue
            out[node_id] = system
    if not out:
        raise spec.SpecNotFound("§3.2b 표를 파싱하지 못했습니다.")
    return out


def is_known(node_id: str | None) -> bool:
    """사전 등재 여부. `node_id` 가 없는 봉투는 대조 대상이 아니다."""
    if node_id is None:
        return True
    return node_id in node_dictionary()


def system_of(node_id: str) -> str | None:
    return node_dictionary().get(node_id)


# ── 미등재 노드 보류 ────────────────────────────────────────────────


@dataclass
class HeldEnvelope:
    envelope: dict
    held_at: float
    ttl_ms: int

    def expired(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.held_at) * 1000.0 >= self.ttl_ms


@dataclass
class HoldBuffer:
    """배달할 상대가 없는 봉투의 보류함.

    `unregistered_node_fallback.terminal_note` — `to.system` 이 `router` 이면서
    `to.node_id` 가 사전에 없을 때가 유일한 보류 조건이다(두 조건의 논리곱).
    만료되면 폐기하고 `2013` 을 발행한다. `2099`(통신 단절)와 구분한다.
    """

    items: list[HeldEnvelope] = field(default_factory=list)

    def hold(self, envelope: dict, now: float | None = None) -> HeldEnvelope:
        ttl = envelope.get("ttl_ms") or DEFAULT_HOLD_TTL_MS
        item = HeldEnvelope(
            envelope=envelope,
            held_at=time.monotonic() if now is None else now,
            ttl_ms=int(ttl),
        )
        self.items.append(item)
        return item

    def pop_expired(self, now: float | None = None) -> list[HeldEnvelope]:
        """만료된 보류 봉투를 꺼낸다. 호출자가 2013 을 발행한다."""
        expired = [i for i in self.items if i.expired(now)]
        self.items = [i for i in self.items if not i.expired(now)]
        return expired

    def release(self, node_id: str) -> list[HeldEnvelope]:
        """노드가 등재되면 그 노드로 가던 보류분을 풀어 준다."""
        out = [i for i in self.items if i.envelope["to"].get("node_id") == node_id]
        self.items = [i for i in self.items if i not in out]
        return out

    def __len__(self) -> int:  # pragma: no cover - 편의
        return len(self.items)
