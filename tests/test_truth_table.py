# -*- coding: utf-8 -*-
"""규격 동봉 진리표 전건 시험.

`routing_rules.json` 의 `truth_table` 을 그대로 돌린다 (v2.1: 49건, v2.2: 64건).
**전건 통과가 조건이다** — 1건이라도 어긋나면 우리 구현이 규격과 갈린 것이다.

이 시험은 우리가 만든 표가 아니라 규격이 준 표다. 규격이 개정되어 표가 늘면
코드를 고치지 않아도 시험이 자동으로 늘어난다.

v2.2 부터 각 행은 판정 계층(`evaluated_at`)과, 응답 4종의 원 발행자 확인 결과
(`origin_issuer_resolved`)를 문맥으로 싣는다. 둘 다 그대로 판정기에 넘긴다.
"""
from __future__ import annotations

import pytest

from fdw_router import spec
from fdw_router.msg import rules

CASES = spec.truth_table()
OMIT_DISPLAY = spec.routing_rules()["rule_precedence"].get("omit_display", "<생략>")


def _ids(cases):
    return [c["id"] for c in cases]


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_truth_table_case(case):
    d = rules.evaluate(
        case["envelope"],
        layer=case.get("evaluated_at", "router"),
        router_known_binding=case.get("router_known_binding"),
        custody_known_binding=case.get("custody_known_binding"),
        origin_issuer_resolved=case.get("origin_issuer_resolved"),
    )
    why = case.get("why", "")

    # ── 5개 불리언은 전 건이 기대값을 갖는다 ────────────────────────
    assert d.schema_reject == case["expect_schema_reject"], f"schema_reject — {why}"
    assert d.discard == case["expect_discard"], f"discard — {why}"
    assert d.relay == case["expect_relay"], f"relay — {why}"
    assert d.fallback == case["expect_fallback"], f"fallback — {why}"
    assert d.hold == case["expect_hold"], f"hold — {why}"

    # ── 선택 기대값 ────────────────────────────────────────────────
    if "expect_routed_to" in case:
        want = case["expect_routed_to"]
        got = OMIT_DISPLAY if d.routed_to is rules.OMIT else d.routed_to
        assert got == want, f"routed_to — {why}"

    if "expect_relay_to" in case:
        assert d.relay_to == case["expect_relay_to"], f"relay_to — {why}"

    if "expect_relay_relayed_by" in case:
        assert d.relayed_envelope is not None, f"중계본 없음 — {why}"
        assert (
            d.relayed_envelope["relayed_by"] == case["expect_relay_relayed_by"]
        ), f"relayed_by — {why}"

    if "expect_relay_changed_keys" in case:
        want = sorted(case["expect_relay_changed_keys"])
        changed = sorted(
            k
            for k in set(d.relayed_envelope) | set(case["envelope"])
            if case["envelope"].get(k) != d.relayed_envelope.get(k)
        )
        assert changed == want, f"중계가 바꾼 키 — {why}"

    if case.get("relay_direction") and d.relay:
        assert d.relay_direction == case["relay_direction"], f"relay_direction — {why}"

    if "expect_self_binding" in case:
        assert d.self_binding == case["expect_self_binding"], f"self_binding — {why}"

    if "expect_node_binding" in case:
        assert d.node_binding == case["expect_node_binding"], f"node_binding — {why}"


def test_all_cases_present():
    """규격이 준 표를 전부 돌렸는지 확인한다."""
    assert len(CASES) >= 49, f"진리표가 {len(CASES)}건뿐입니다 — 배포본을 확인하십시오."
