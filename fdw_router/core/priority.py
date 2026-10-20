# -*- coding: utf-8 -*-
"""작업 선택 규칙 — ATCS 지수.

설계 목표 *"작업 특성(긴급도, 우선순위, 공정조건)을 고려한 라우팅
로직"* 을 **세 항으로 나누어** 구현한다. 설계 문구와 수식 항이 1:1로 대응하는 것이
이 모듈의 설계 의도다 — 평가 때 "어디에 반영했는가"에 바로 답할 수 있어야 한다.

    I(j,c) = (w_j / p_jc) · exp( − max(0, d_j − p_jc − t) / (k1 · p_avg) )
                          · exp( − s_cj / (k2 · s_avg) )

| 항 | 설계 문구 | 뜻 |
|---|---|---|
| `w_j` | **우선순위** | 작업 중요도. 요청 2 확정 전에는 시나리오가 싣는다 |
| `exp(−slack/…)` | **긴급도** | 납기 여유가 줄수록 지수적으로 우선 |
| `exp(−s_cj/…)` | **공정조건** | 셋업·치구·재료 전환 비용. 같은 조건을 자연히 묶는다 |
| `p_jc` | — | 셀 c 에서의 처리시간 |

ATCS(Apparent Tardiness Cost with Setups)는 병렬기계 스케줄링의 표준 디스패칭
규칙이라 문헌 근거가 있고, `k1`·`k2` 튜닝만으로 성능을 조정할 수 있다.

**난수를 쓰지 않는다.** 동일 입력 → 동일 지수여야 KPI 개선 주장의 근거가 된다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Cell, Job, Operation, ShopState


@dataclass(frozen=True)
class AtcsParams:
    """튜닝 대상. P5(11월)에서 시나리오별로 맞춘다."""

    k1: float = 2.0          # 납기 여유 정규화 (긴급도 민감도)
    k2: float = 1.0          # 셋업 정규화 (공정조건 민감도)
    default_duration_s: float = 60.0
    #: 셋업 시간 실측이 없다. step_id 가 바뀌면 이만큼 든다고 본다.
    #: 확정 실측이 오면 `setup_time_s` 를 교체한다 (C-4 / 공정 소요시간).
    setup_switch_s: float = 30.0


DEFAULT = AtcsParams()


def setup_time_s(cell: Cell, op: Operation, shop: ShopState,
                 p: AtcsParams = DEFAULT) -> float:
    """`s_cj` — 셀 c 가 작업 j 를 받을 때 드는 전환 비용.

    실측이 없으므로 **직전 작업과 같은 `step_id` 면 0, 다르면 `setup_switch_s`** 로
    근사한다. 이 근사가 "동일 조건 연속 작업을 자연스럽게 묶는다"는 의도를 만든다.
    """
    last = shop.last_step.get(cell.cell_id)
    if last is None or last == op.step_id:
        return 0.0
    return p.setup_switch_s


def slack_s(job: Job, op: Operation, cell: Cell, shop: ShopState,
            p: AtcsParams = DEFAULT) -> float:
    """납기 여유 = `d_j − (남은 작업량) − t`.

    원 식은 `d_j − p_jc − t` 이지만, 한 작업만 보면 뒤에 남은 공정이
    무시되어 여러 공정을 거치는 job 이 과소평가된다. 남은 작업량 전체로 본다.
    """
    remaining = job.remaining_work_s(shop.done_of(job.job_id), p.default_duration_s)
    return job.due_s - remaining - shop.now_s


def atcs_index(job: Job, op: Operation, cell: Cell, shop: ShopState,
               p: AtcsParams = DEFAULT) -> float:
    """작업 j 를 셀 c 에 지금 배정할 때의 지수. **클수록 먼저.**"""
    p_jc = max(cell.duration_for(op), 1e-9)
    p_avg = max(shop.mean_duration_s(p.default_duration_s), 1e-9)
    s_cj = setup_time_s(cell, op, shop, p)
    s_avg = max(p.setup_switch_s, 1e-9)

    urgency = math.exp(-max(0.0, slack_s(job, op, cell, shop, p)) / (p.k1 * p_avg))
    changeover = math.exp(-s_cj / (p.k2 * s_avg))
    return (job.weight / p_jc) * urgency * changeover


@dataclass(frozen=True)
class Ranked:
    job_id: str
    op_id: str
    cell_id: str
    index: float

    #: 결정 로그용 성분 분해 — "왜 이 순서였나"에 답한다
    w: float = 0.0
    p_jc: float = 0.0
    slack_s: float = 0.0
    setup_s: float = 0.0


def rank(job: Job, op: Operation, cells, shop: ShopState,
         p: AtcsParams = DEFAULT) -> tuple[Ranked, ...]:
    """후보 셀들을 지수 내림차순으로 세운다.

    동점이면 `cell_id` 오름차순 — 결정론을 위해 임의 순서를 남기지 않는다.
    """
    out = [
        Ranked(
            job_id=job.job_id,
            op_id=op.op_id,
            cell_id=c.cell_id,
            index=atcs_index(job, op, c, shop, p),
            w=job.weight,
            p_jc=c.duration_for(op),
            slack_s=slack_s(job, op, c, shop, p),
            setup_s=setup_time_s(c, op, shop, p),
        )
        for c in cells
    ]
    out.sort(key=lambda r: (-r.index, r.cell_id))
    return tuple(out)


def select_next_op(jobs, shop: ShopState, p: AtcsParams = DEFAULT):
    """준비된 작업 전체에서 **지수가 가장 큰 (작업, 셀)** 을 고른다.

    실행가능성 필터를 통과한 조합만 본다. 후보가 없으면 `None`.
    """
    from .feasibility import feasible_cells

    best: Ranked | None = None
    for job in jobs:
        for op in job.ready_ops(shop.done_of(job.job_id)):
            fs = feasible_cells(job, op, shop)
            if not fs:
                continue
            top = rank(job, op, fs.cells, shop, p)[0]
            if best is None or (top.index, best.cell_id) > (best.index, top.cell_id):
                best = top
    return best
