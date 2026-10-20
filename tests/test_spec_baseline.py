# -*- coding: utf-8 -*-
"""로드된 규격 패키지가 현행 기준선(v2.2 · fdw.v1.1)인지.

규격이 또 개정되면 이 시험이 먼저 깨진다 — 그때 기준선을 올리고 CHANGELOG 의
「구현이 확인할 변경」표를 따라 config/코드를 점검한다.
"""
from __future__ import annotations

from fdw_router import spec
from fdw_router.msg import build


def test_package_is_v22():
    assert spec.package_version() == "2.2", (
        f"규격 패키지가 v2.2 가 아닙니다: {spec.package_version()} ({spec.spec_root()})"
    )


def test_schema_version_is_fdw_v1_1():
    assert spec.schema_version() == "fdw.v1.1"
    assert build.schema_version() == "fdw.v1.1"
    assert build.SCHEMA_VERSION == "fdw.v1.1"      # 호환 속성


def test_guideline_md_found_via_index():
    p = spec.guideline_md()
    assert p.name == "T2_guideline_v2.2.md", p


def test_v22_rules_present():
    rules = spec.routing_rules()
    assert "relay_to_router" in rules
    assert rules["rule_precedence"]["order_at_fdw_ops"] == ["relay_to_router"]
    assert len(spec.truth_table()) == 64
    assert len(spec.negative_fixtures()) == 44
    assert len(spec.message_type_catalog()) == 24


def test_router_issued_task_request_carries_correlation_id():
    """NEG-44 — 라우터 발행 task_request 에 correlation_id 가 없으면 스키마가 거절한다."""
    from fdw_router.msg import envelope
    f = build.EnvelopeFactory(run_id="corr")
    e = f.make(
        message_type="task_request",
        frm=("router", "nuc-01"),
        to=("cell", "welding_cell_01"),
        payload={"task_id": f.task_id("t"), "step_id": "weld_arc_pass1",
                 "expected_duration_s": 20.0, "params": {"weld_arc_current": 200}},
        at=0.0,
    )
    assert "correlation_id" in e
    assert envelope.validate(e).ok, envelope.validate(e).errors
    bad = dict(e); bad.pop("correlation_id")
    assert not envelope.validate(bad).ok
