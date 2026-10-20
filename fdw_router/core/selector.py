# -*- coding: utf-8 -*-
"""셀 선택 규칙 — 비용 최소화.

설계 목표 *"공정 셀 부하 분산 알고리즘 구현"* 의 실체.

    Cost(j,c) = α·Wait(c) + β·p_jc + γ·Transport(prev→c) + δ·Setup(c,j) + ε·Imbalance(c)

| 항 | 설계 대응 | 입력 |
|---|---|---|
| `Wait(c)` | **대기시간 최소화** | `CellStatus.load_s` (진행중 잔여 + 대기큐) |
| `p_jc` | — | `Cell.duration_for(op)` |
| `Transport` | — | `ShopState.last_cell[job]` → c 이송시간 (`TransferTable`) |
| `Setup` | 공정조건 | `priority.setup_time_s` 재사용 |
| `Imbalance(c)` | **부하 분산** | 배정 후 셀별 누적부하 표준편차의 **증가분** |

α~ε 는 시나리오별 프로파일(`leadtime` / `balanced` / `urgent`)로 제공한다. P5 튜닝 대상.
**난수 없음. 동점은 `cell_id` 오름차순.** 결정 로그에 성분별 값을 남긴다 (`priority.Ranked` 와 같은 방식).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from . import priority
from .model import Cell, Job, Operation, ShopState


@dataclass(frozen=True)
class CostWeights:
    """α~ε. 단위를 맞추기 위해 전부 '초'에 곱하는 무차원 계수로 둔다."""

    alpha: float = 1.0      # Wait
    beta: float = 1.0       # p_jc
    gamma: float = 1.0      # Transport
    delta: float = 1.0      # Setup
    epsilon: float = 1.0    # Imbalance


#: 시나리오별 프로파일. 값은 P5(11월) 튜닝 전 초기값이다.
PROFILES: Mapping[str, CostWeights] = {
    # 리드타임 우선 — 지금 가장 빨리 끝나는 셀
    "leadtime": CostWeights(alpha=1.0, beta=1.0, gamma=1.0, delta=1.0, epsilon=0.2),
    # 균형 우선 — 셀 간 누적부하 편차를 가장 무겁게
    "balanced": CostWeights(alpha=1.0, beta=0.5, gamma=0.5, delta=0.5, epsilon=2.0),
    # 긴급 우선 — 대기·이송만 보고 즉시 착수 (셋업·균형은 무시에 가깝게)
    "urgent": CostWeights(alpha=2.0, beta=1.0, gamma=1.0, delta=0.1, epsilon=0.0),
}
DEFAULT_PROFILE = "balanced"


class TransferTable:
    """셀 간 이송시간 표. 실측이 없으므로 기본 0 — 값이 오면 표만 교체한다.

    키는 `(from_cell_id, to_cell_id)`. 같은 셀이면 0. 미등재 쌍은 `default_s`.
    """

    def __init__(self, table: Mapping[tuple[str, str], float] | None = None,
                 default_s: float = 0.0):
        self._t = dict(table or {})
        self.default_s = default_s

    def seconds(self, frm: str | None, to: str) -> float:
        if frm is None or frm == to:
            return 0.0
        return float(self._t.get((frm, to), self.default_s))


NO_TRANSFER = TransferTable()


def load_of(cell: Cell, shop: ShopState) -> float:
    """셀 c 의 부하(초) = 진행중 잔여 + 대기큐 × 공칭 사이클. `CellStatus.queue_depth` 는 건수라 셀의 사이클로 초로 바꾼다."""
    st = shop.status_of(cell.cell_id)
    return float(st.remaining_s) + float(st.queue_depth) * float(cell.nominal_cycle_time_s)


def wait_s(cell: Cell, shop: ShopState) -> float:
    """`Wait(c)` — 대기시간 최소화 항. 진행중 잔여 + 대기큐 (초)."""
    return load_of(cell, shop)


def transport_s(job: Job, cell: Cell, shop: ShopState,
                transfer: TransferTable = NO_TRANSFER) -> float:
    """`Transport(prev→c)` — 직전 작업이 끝난 셀에서 c 까지."""
    return transfer.seconds(shop.last_cell.get(job.job_id), cell.cell_id)


def _std(values: Iterable[float]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))


def imbalance_s(cell: Cell, p_jc: float, shop: ShopState) -> float:
    """`Imbalance(c)` — c 에 `p_jc` 를 얹었을 때 셀별 누적부하 표준편차가 얼마나 느는가.

    음수가 될 수 있다(부하가 낮은 셀에 주면 편차가 준다). 그것이 의도다 —
    부하 분산 레버 ①(공간적 — 셀 선택)이 이 항으로 작동한다.
    """
    loads = {cid: load_of(c, shop) for cid, c in shop.cells.items()}
    before = _std(loads.values())
    loads[cell.cell_id] = loads.get(cell.cell_id, 0.0) + p_jc
    after = _std(loads.values())
    return after - before


@dataclass(frozen=True)
class CostRanked:
    """결정 로그용 성분 분해 — "왜 이 셀이었나"에 답한다."""

    job_id: str
    op_id: str
    cell_id: str
    cost: float
    profile: str
    wait_s: float = 0.0
    p_jc: float = 0.0
    transport_s: float = 0.0
    setup_s: float = 0.0
    imbalance_s: float = 0.0


def cost(job: Job, op: Operation, cell: Cell, shop: ShopState, *,
         profile: str = DEFAULT_PROFILE,
         transfer: TransferTable = NO_TRANSFER,
         atcs: priority.AtcsParams = priority.DEFAULT) -> CostRanked:
    w = PROFILES[profile]
    p_jc = cell.duration_for(op)
    terms = CostRanked(
        job_id=job.job_id, op_id=op.op_id, cell_id=cell.cell_id, cost=0.0, profile=profile,
        wait_s=wait_s(cell, shop),
        p_jc=p_jc,
        transport_s=transport_s(job, cell, shop, transfer),
        setup_s=priority.setup_time_s(cell, op, shop, atcs),
        imbalance_s=imbalance_s(cell, p_jc, shop),
    )
    total = (w.alpha * terms.wait_s + w.beta * terms.p_jc + w.gamma * terms.transport_s
             + w.delta * terms.setup_s + w.epsilon * terms.imbalance_s)
    return CostRanked(**{**terms.__dict__, "cost": total})


def rank_cost(job: Job, op: Operation, cells: Iterable[Cell], shop: ShopState, *,
              profile: str = DEFAULT_PROFILE,
              transfer: TransferTable = NO_TRANSFER,
              atcs: priority.AtcsParams = priority.DEFAULT) -> tuple[CostRanked, ...]:
    """후보 셀들을 비용 오름차순으로 세운다. 동점이면 `cell_id` 오름차순."""
    out = [cost(job, op, c, shop, profile=profile, transfer=transfer, atcs=atcs) for c in cells]
    out.sort(key=lambda r: (r.cost, r.cell_id))
    return tuple(out)


def select_cell(job: Job, op: Operation, cells: Iterable[Cell], shop: ShopState,
                **kw) -> CostRanked | None:
    """비용 최소 셀. 후보가 없으면 `None`."""
    ranked = rank_cost(job, op, cells, shop, **kw)
    return ranked[0] if ranked else None
