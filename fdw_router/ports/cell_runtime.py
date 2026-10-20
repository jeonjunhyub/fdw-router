# -*- coding: utf-8 -*-
"""셀 런타임 포트 — Isaac Sim 이 들어올 자리.

**이 파일이 1차년도 설계의 핵심 결정이다.**

Isaac Sim 은 아직 없다. 그러나 2차년도에 반드시 들어온다. 그러니 라우터를
"Isaac Sim 이 이미 있는 것처럼" 만들되, 그 자리를 **포트로 비워 둔다.**

    라우터  ──T2 봉투──▶  CellRuntime (포트)
                              ├─ DesCellRuntime    : SimPy       (1차년도)
                              └─ IsaacCellRuntime  : ROS 2 브리지 (2차년도)

라우터는 셀이 시뮬레이션인지 실물인지 **모른다.** 양쪽 다 T2 봉투로만 말하므로,
Isaac Sim 도입은 어댑터 교체이지 라우터 수정이 아니다.

────────────────────────────────────────────────────────────────────
지켜야 할 것 세 가지 — 이걸 어기면 2차년도에 라우터를 다시 짜야 한다
────────────────────────────────────────────────────────────────────

**1. 시간은 포트에서 받는다.**
   DES 는 가상 시간이고 Isaac Sim 은 실시간(또는 배속)이다. 라우터가
   `time.monotonic()` 을 직접 부르면 DES 에서 맞춰 둔 타임아웃이 Isaac 에서
   전부 어긋난다. 타임아웃·보류 TTL·결정 지연 측정은 **반드시** `now()` 를 쓴다.

**2. 라우터는 봉투만 주고받는다.**
   포트에 "셀 내부 상태를 직접 읽는" 메서드를 두지 않는다. DES 는 내부를 다 알지만
   Isaac Sim 은 그렇지 않다. DES 의 편의에 기대면 그 편의가 사라질 때 무너진다.

**3. 결정론을 포트 밖에 둔다.**
   난수는 어댑터가 seed 로 관리한다. 라우터 자체는 난수를 쓰지 않는다 —
   동일 입력 → 동일 배정이어야 KPI 개선 주장의 근거가 된다.
"""
from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """시간축. DES 는 가상 시간, Isaac 은 시뮬레이션 시각을 돌려준다."""

    def now(self) -> float:
        """현재 시각 (초). 기점은 구현이 정하되 단조 증가해야 한다."""
        ...


@runtime_checkable
class CellRuntime(Clock, Protocol):
    """셀 집합을 대리하는 런타임.

    구현체는 `send` 로 받은 지시를 자기 방식으로 수행하고, 그 결과를
    T2 봉투로 만들어 `poll` 로 돌려준다.
    """

    name: str

    # ── 라우터 → 셀 ────────────────────────────────────────────────
    def send(self, envelope: dict) -> None:
        """봉투 하나를 셀 계층으로 보낸다.

        `envelope["to"]["node_id"]` 가 대상 셀이다. 런타임이 모르는 노드면
        조용히 버리지 말고 `task_reject` 또는 `event` 봉투로 되돌려 준다 —
        라우터가 그것을 보고 재배정한다.
        """
        ...

    # ── 셀 → 라우터 ────────────────────────────────────────────────
    def poll(self) -> Iterable[dict]:
        """지난 호출 이후 셀 계층이 만들어 낸 봉투를 모두 돌려준다.

        `cell_status` · `task_accept` · `task_complete` · `task_failed` ·
        `event` · `heartbeat` · `cell_capability` 가 여기로 온다.
        빈 반복자를 돌려주는 것은 정상이다.
        """
        ...

    # ── 시간 진행 ──────────────────────────────────────────────────
    def step(self, until: float | None = None) -> None:
        """런타임을 다음 사건까지(또는 `until` 까지) 진행시킨다.

        DES: 다음 이산사건까지 점프한다.
        Isaac: 실시간이므로 대개 아무것도 하지 않거나 한 틱 대기한다.
        """
        ...

    @property
    def finished(self) -> bool:
        """더 진행할 사건이 없는가. Isaac 런타임은 보통 항상 False 다."""
        ...


@runtime_checkable
class OrchestratorPort(Protocol):
    """오케스트레이션 계층(LLM) 대리.

    1차년도에는 스텁이다. S6·S7 대조실험에서 "라우터 없는 직결 구성"의
    LLM 호출 횟수를 세려면 이 포트의 호출 수만 세면 된다.
    """

    def send(self, envelope: dict) -> None:
        """라우터 → 오케스트레이터 (중계본·이벤트·위임)."""
        ...

    def poll(self) -> Iterable[dict]:
        """오케스트레이터 → 라우터 (재계획 지시 등)."""
        ...

    @property
    def call_count(self) -> int:
        """S6·S7 의 핵심 측정값 — LLM 호출 횟수."""
        ...
