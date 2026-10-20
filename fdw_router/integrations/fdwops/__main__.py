# -*- coding: utf-8 -*-
"""fdw-ops 시뮬레이터 위에서 라우터가 배정한다.

    python -m fdw_router.integrations.fdwops --mode discrete [--config path] [--seed 0]
                                              [--router-only] [--log-level INFO]

`SimulationManager.__init__` 이 `FDWOrchestrator(self.bus, orchestrator_config)` 를
직접 생성하므로(`fdw_sim/simulation/manager.py:155`), 빌드 전에
`fdw_sim.simulation.manager.FDWOrchestrator` 를 `RouterOrchestrator` 로 바꿔 끼운다
(monkeypatch). fdw-ops 소스는 손대지 않는다.

같은 설정으로 **원본 오케스트레이터**도 한 번 더 돌려 KPI 를 나란히 찍는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import random
import sys
from pathlib import Path
from typing import Any, Dict

from ...adapters.fdwops_cell import ensure_fdw_ops_importable
from .router_orchestrator import LOG_DIR, RouterOrchestrator

logger = logging.getLogger(__name__)


def _load_run_poc1(root: Path):
    """`scripts/run_poc1.py` 를 모듈로 읽는다 (패키지가 아니라 파일 경로로)."""
    spec = importlib.util.spec_from_file_location("fdw_run_poc1", root / "scripts" / "run_poc1.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_once(run_poc1, cfg: Dict[str, Any], *, use_router: bool, mode: str, seed: int,
              run_name: str) -> Dict[str, Any]:
    import fdw_sim.simulation.manager as manager  # type: ignore

    original = manager.FDWOrchestrator
    if use_router:
        manager.FDWOrchestrator = RouterOrchestrator      # 슬롯 교체
    random.seed(seed)                                      # 검사 셀의 측정 노이즈를 같게
    cfg = dict(cfg)
    cfg["simulation"] = dict(cfg["simulation"], log_dir=str(LOG_DIR / "fdw_sim"), run_name=run_name)
    try:
        sim = run_poc1.build_simulation(cfg, mode_override=mode, headless_override=True)
        sim.start()
        try:
            run_poc1.submit_jobs(sim, cfg)
            sim.run_until_done(all_jobs_done=True, extra_idle_sec=2.0)
            run_poc1.print_report(sim)
        finally:
            sim.stop()
    finally:
        manager.FDWOrchestrator = original

    orch = sim.orchestrator
    summ = sim.kpi.summary()
    out = {
        "label": "router (RouterOrchestrator)" if use_router else "original (FDWOrchestrator)",
        "sim_time_s": round(sim.sim_time, 1),
        "completed": len(orch.completed_jobs),
        "quarantined": len(orch.quarantined_jobs),
        "active": len(orch.active_jobs),
        "makespan_avg_s": round(summ.get("job_makespan_sec", {}).get("avg", float("nan")), 1),
        "makespan_max_s": round(summ.get("job_makespan_sec", {}).get("max", float("nan")), 1),
        "cycle_time_avg_s": round(summ.get("cycle_time", {}).get("avg", float("nan")), 2),
    }
    if use_router:
        orch.close()
        out.update({
            "router_decisions": orch.stats.decisions,
            "replan_2014_events": orch.stats.replanning_events,
            "selector_used": orch.stats.selector_used,
            "min_load_used": orch.stats.min_load_used,
            "decision_log": str(orch.stats.log_path),
        })
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="fdw-ops 위에서 라우터가 배정한다")
    p.add_argument("--mode", choices=["discrete", "isaac"], default="discrete")
    p.add_argument("--config", type=Path, default=None, help="fdw-ops poc1.yaml 경로 (기본: 패키지 내장)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--router-only", action="store_true", help="원본 오케스트레이터 비교 실행 생략")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    root = ensure_fdw_ops_importable()
    run_poc1 = _load_run_poc1(root)
    run_poc1.setup_logging(level=args.log_level)
    cfg_path = args.config or (root / "fdw_sim" / "config" / "poc1.yaml")
    cfg = run_poc1.load_config(cfg_path)

    results = []
    if not args.router_only:
        results.append(_run_once(run_poc1, cfg, use_router=False, mode=args.mode, seed=args.seed,
                                 run_name="original"))
    results.append(_run_once(run_poc1, cfg, use_router=True, mode=args.mode, seed=args.seed,
                             run_name="router"))

    print("=" * 70)
    print(" KPI side by side  (same config, same seed=%d)" % args.seed)
    print("=" * 70)
    keys = ["sim_time_s", "completed", "quarantined", "active", "makespan_avg_s", "makespan_max_s", "cycle_time_avg_s"]
    print(f" {'metric':18s} " + "  ".join(f"{r['label']:>30s}" for r in results))
    for k in keys:
        print(f" {k:18s} " + "  ".join(f"{str(r.get(k)):>30s}" for r in results))
    r = results[-1]
    print("-" * 70)
    print(f" router decisions: {r['router_decisions']}, 2014 events: {r['replan_2014_events']}, "
          f"selector/min_load: {r['selector_used']}/{r['min_load_used']}")
    print(f" decision log: {r['decision_log']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
