# -*- coding: utf-8 -*-
"""통합 지점 ① — fdw-ops 시뮬레이터 안에서 라우터가 배정한다.

    python -m fdw_router.integrations.fdwops --mode discrete

`RouterOrchestrator` 는 fdw_sim 의 `FDWOrchestrator` 와 같은 표면을 가진 대체물이다.
"""
from .router_orchestrator import RouterOrchestrator, RouterOrchestratorStats  # noqa: F401
