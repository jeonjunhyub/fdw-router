# -*- coding: utf-8 -*-
"""결정 로그 — 1급 산출물 (JSON Lines).

설계 문서가 결정 로그를 1급 산출물로 두었고, 규격 협의에서
규격 담당이 "현재 기록 여부와 필드 구성 공유 — 2차년도 AI 모델링 입력, 1차년도
운전분은 소급 수집 불가" 라고 요청했다. 그래서 **처음부터 기록한다.**

한 줄 = 결정 하나. 필드:

    task_id · job_id · op_id · outcome(assigned/delegated/waiting)
    decided_at_s (포트 시각) · duration_ms (결정 소요) · profile
    candidates[] · chosen_cell · exclusions[{cell_id, reason, detail}]
    atcs[{cell_id, index, w, p_jc, slack_s, setup_s}]
    cost[{cell_id, cost, wait_s, p_jc, transport_s, setup_s, imbalance_s}]
    note

이 모듈은 **순수 함수 + 쓰기 전용**이다. 시각은 호출자가 포트에서 받아 넘긴다 —
여기서 `time.time()` 을 부르지 않는다 (DES 가상시간과 어긋난다).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .feasibility import Rejection
from .priority import Ranked
from .selector import CostRanked


@dataclass(frozen=True)
class DecisionRecord:
    task_id: str
    job_id: str
    op_id: str
    outcome: str                      # assigned | delegated | waiting
    decided_at_s: float
    duration_ms: float
    profile: str
    candidates: tuple[str, ...] = ()
    chosen_cell: str | None = None
    exclusions: tuple[dict, ...] = ()
    atcs: tuple[dict, ...] = ()
    cost: tuple[dict, ...] = ()
    note: str = ""
    attempt: int = 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = list(self.candidates)
        d["exclusions"] = list(self.exclusions)
        d["atcs"] = list(self.atcs)
        d["cost"] = list(self.cost)
        return d


def _exclusions(rejections: Iterable[Rejection]) -> tuple[dict, ...]:
    return tuple(
        {"cell_id": r.cell_id, "reason": r.reason.name, "detail": r.detail}
        for r in rejections
    )


def _atcs(ranked: Iterable[Ranked]) -> tuple[dict, ...]:
    return tuple(
        {"cell_id": r.cell_id, "index": r.index, "w": r.w, "p_jc": r.p_jc,
         "slack_s": r.slack_s, "setup_s": r.setup_s}
        for r in ranked
    )


def _cost(ranked: Iterable[CostRanked]) -> tuple[dict, ...]:
    return tuple(
        {"cell_id": r.cell_id, "cost": r.cost, "wait_s": r.wait_s, "p_jc": r.p_jc,
         "transport_s": r.transport_s, "setup_s": r.setup_s, "imbalance_s": r.imbalance_s}
        for r in ranked
    )


def make_record(*, task_id: str, job_id: str, op_id: str, outcome: str,
                decided_at_s: float, duration_ms: float, profile: str,
                candidates: Sequence[str] = (), chosen_cell: str | None = None,
                rejections: Iterable[Rejection] = (),
                atcs: Iterable[Ranked] = (), cost: Iterable[CostRanked] = (),
                note: str = "", attempt: int = 1) -> DecisionRecord:
    """순수 함수. 성분 객체를 로그 행으로 바꾼다."""
    return DecisionRecord(
        task_id=task_id, job_id=job_id, op_id=op_id, outcome=outcome,
        decided_at_s=decided_at_s, duration_ms=duration_ms, profile=profile,
        candidates=tuple(candidates), chosen_cell=chosen_cell,
        exclusions=_exclusions(rejections), atcs=_atcs(atcs), cost=_cost(cost),
        note=note, attempt=attempt,
    )


def to_json_line(rec: DecisionRecord) -> str:
    return json.dumps(rec.to_dict(), ensure_ascii=False, sort_keys=True)


class DecisionLog:
    """메모리에 쌓고, 경로가 있으면 JSON Lines 로도 즉시 덧붙인다."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[DecisionRecord] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def write(self, rec: DecisionRecord) -> None:
        self.records.append(rec)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(to_json_line(rec) + "\n")

    def lines(self) -> list[str]:
        return [to_json_line(r) for r in self.records]

    def __len__(self) -> int:
        return len(self.records)


def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
