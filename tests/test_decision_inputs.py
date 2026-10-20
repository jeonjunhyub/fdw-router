# -*- coding: utf-8 -*-
"""판정 입력 불변성 전수 시험.

규격 `decision_inputs_note` 가 직접 권한 검증 방법이다 —

    *"진리표 표본이 배제하지 못한 대안 술어는 행을 늘려 닫으려 하면 끝나지 않는다
    (실측: 폴백 16행이 필요하고, 중계는 2항을 죽이면 3항 20종이 새로 드러난다).
    판정 입력을 못박고 **그 밖의 축을 흔들어도 판정이 불변인지** 전수로 시험하는
    편이 닫힌다."*

즉 49건 표본 통과만으로는 부족하다. 각 규칙이 선언한 입력 조합을 전수로 돌리고,
선언되지 않은 축(payload·priority·ttl_ms·session_id 등)을 흔들어도 판정이 그대로인지
확인한다.
"""
from __future__ import annotations

import itertools

import pytest

from fdw_router import spec
from fdw_router.msg import build, node_registry, rules

SYSTEMS = ["cell", "fleet", "subtask", "router", "fdw-ops", "external"]
LAYERS = ["router", "fdw-ops"]
MTYPES = sorted(
    spec.schemas()["envelope.schema.json"]["properties"]["message_type"]["enum"]
)
KNOWN_NODE = {"cell": "welding_cell_01", "fleet": "mmrs_fleet_01",
              "subtask": "s2-node-01", "router": "nuc-01",
              "fdw-ops": "ops-01", "external": "mes-01"}
UNKNOWN_NODE = "unknown_zz_99"


def _env(mtype, fs, ts, from_known=True, to_known=True, **extra):
    e = {
        "schema_version": build.schema_version(),
        "envelope_id": "00000000-1111-4111-8111-111111111111",
        "timestamp_iso": "2026-05-31T09:00:00.000+09:00",
        "from": {"system": fs, "node_id": KNOWN_NODE[fs] if from_known else UNKNOWN_NODE},
        "to": {"system": ts, "node_id": KNOWN_NODE[ts] if to_known else UNKNOWN_NODE},
        "message_type": mtype,
        "payload": {},
    }
    e.update(extra)
    return e


def _decide(e, **kw):
    # 판정 규칙만 시험한다. 스키마 검증은 별도 시험(부정 표본)이 맡는다.
    return rules.evaluate(e, validate_schema=False, **kw)


# ── ① 선언되지 않은 축을 흔들어도 판정이 불변인가 ────────────────────

NOISE = [
    {},
    {"priority": "critical"},
    {"priority": "low", "ttl_ms": 1234},
    {"session_id": "s-1", "factory_id": "F1", "line_id": "L1"},
    {"payload": {"anything": [1, 2, 3], "nested": {"a": "b"}}},
    {"qos": 2},
]


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("mtype", MTYPES)
@pytest.mark.parametrize("fs,ts", list(itertools.product(SYSTEMS, SYSTEMS)))
def test_undeclared_axes_do_not_change_decision(mtype, fs, ts, layer):
    base = _decide(_env(mtype, fs, ts), layer=layer)
    for noise in NOISE:
        d = _decide(_env(mtype, fs, ts, **noise), layer=layer)
        got = (d.discard, d.relay, d.fallback, d.hold, d.routed_to)
        want = (base.discard, base.relay, base.fallback, base.hold, base.routed_to)
        assert got == want, (
            f"[{layer}] {mtype} {fs}->{ts} 에서 선언되지 않은 축 {list(noise)} 이(가) "
            f"판정을 바꿨습니다: {want} → {got}"
        )


# ── ② 각 규칙이 선언한 입력만으로 판정이 결정되는가 ──────────────────


def test_discard_matches_declared_condition():
    """`discard_at_router.decision_inputs` = received_at, message_type, from.system, to.system"""
    rule = spec.routing_rules()["discard_at_router"]
    assert set(rule["decision_inputs"]) == {
        "received_at", "message_type", "from.system", "to.system"
    }
    for mtype, fs, ts, fk, tk, layer in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, [True, False], [True, False], LAYERS
    ):
        d = _decide(_env(mtype, fs, ts, from_known=fk, to_known=tk), layer=layer)
        want = (
            layer == "router"
            and mtype not in rule["message_type_not_in"]
            and fs == rule["from_system"]
            and ts == rule["to_system"]
        )
        assert d.discard == want, f"폐기 판정 불일치: [{layer}] {mtype} {fs}->{ts}"


# ── ②-1 v2.2: 오케스트레이션 계층이 수신한 봉투에는 라우팅 계층 규칙이 걸리지 않는다 ──


def test_orchestration_layer_never_fallbacks_discards_or_holds():
    """TT-60·61·64 의 전수판 — 폴백·보류·폐기는 라우팅 계층의 규칙이다 (별첨 D.11)."""
    for mtype, fs, ts, fk, tk in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, [True, False], [True, False]
    ):
        d = _decide(_env(mtype, fs, ts, from_known=fk, to_known=tk), layer="fdw-ops")
        assert not d.fallback and not d.hold and not d.discard, (
            f"오케스트레이션 계층이 라우팅 규칙을 적용했습니다: {mtype} {fs}->{ts}"
        )
        assert 2012 not in d.event_codes


def test_relay_to_router_applies_only_to_declared_inputs():
    """`relay_to_router.applies_when` — received_at=fdw-ops · to.system=fdw-ops · 5종 · 중계본 아님.

    응답 4종은 거기에 더해 원 발행자가 `router` 로 **확인된** 것만 (response_scope).
    """
    rule = spec.routing_rules()["relay_to_router"]
    types = set(rule["applies_when"]["message_types"])
    for mtype, fs, ts, issuer, layer in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, ["router", "fdw-ops", "unknown", None], LAYERS
    ):
        d = _decide(_env(mtype, fs, ts), layer=layer, origin_issuer_resolved=issuer)
        want = layer == "fdw-ops" and ts == "fdw-ops" and mtype in types
        if want and mtype != "cell_capability":
            want = issuer == "router"
        got = d.relay and d.relay_direction == "orchestration_to_router"
        assert got == want, f"역방향 중계 불일치: [{layer}] {mtype} {fs}->{ts} issuer={issuer}"
        if got:
            assert d.relay_to == {"system": "router", "node_id": "nuc-01"}
            assert d.relayed_envelope["relayed_by"] == "ops-01"
            assert d.relayed_envelope["from"] == {"system": fs, "node_id": KNOWN_NODE[fs]}  # 원발신자 그대로
        if layer == "fdw-ops" and ts == "fdw-ops" and mtype in types - {"cell_capability"}:
            assert d.issuer_check_required == (issuer not in ("router", "fdw-ops"))


def test_relayed_envelopes_are_terminal_at_both_layers():
    """`re_relay_exclusion` — 중계본은 어느 계층에서도 다시 중계·폴백하지 않는다."""
    for mtype in ("cell_capability", "task_accept", "task_complete"):
        e = _env(mtype, "cell", "router", from_known=False)
        e["relayed_by"] = "ops-01"
        d = _decide(e, layer="router")
        assert not d.relay and not d.fallback and not d.hold
    e = _env("cell_capability", "cell", "fdw-ops", from_known=False)
    e["relayed_by"] = "nuc-01"
    d = _decide(e, layer="fdw-ops")
    assert not d.relay


def test_forged_relay_is_rejected_even_without_schema():
    """`relayed_by_rule.forbid_when` — 자기 계층으로 중계한 형태는 위조 (TT-51·52 의 규칙판)."""
    e = _env("cell_capability", "cell", "fdw-ops"); e["relayed_by"] = "ops-01"
    d = _decide(e, layer="router")
    assert d.schema_reject and d.forged_relay
    e = _env("cell_capability", "cell", "router"); e["relayed_by"] = "nuc-01"
    assert _decide(e).forged_relay
    e = _env("heartbeat", "cell", "router"); e["relayed_by"] = "ops-01"   # NEG-43
    assert _decide(e).forged_relay


def test_duplicate_envelope_id_later_one_is_discarded():
    """`duplicate_handling` — 같은 envelope_id 를 두 번 받으면 뒤의 것을 폐기한다."""
    seen: set[str] = set()
    e = _env("cell_status", "cell", "router")
    first = _decide(e, seen_envelope_ids=seen)
    assert first.relay and not first.duplicate
    again = dict(e); again["relayed_by"] = "nuc-01"; again["to"] = {"system": "fdw-ops", "node_id": "ops-01"}
    second = _decide(again, seen_envelope_ids=seen)
    assert second.duplicate and second.discard and second.routed_to is rules.OMIT
    assert 2011 not in second.event_codes


def test_observed_ids_name_only_unregistered_nodes():
    """`observed_node_id_rule` — 등재된 쪽 식별자를 미등재 자리에 적지 않는다."""
    d = _decide(_env("event", "cell", "fdw-ops", from_known=False))
    assert d.observed_from_node_id == UNKNOWN_NODE and d.observed_node_id is None
    d = _decide(_env("event", "cell", "fdw-ops", to_known=False))
    assert d.observed_node_id == UNKNOWN_NODE and d.observed_from_node_id is None
    d = _decide(_env("event", "cell", "fdw-ops", from_known=False, to_known=False))
    assert d.observed_node_id == UNKNOWN_NODE and d.observed_from_node_id == UNKNOWN_NODE


def test_fallback_depends_only_on_node_registration():
    for mtype, fs, ts, fk, tk in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, [True, False], [True, False]
    ):
        d = _decide(_env(mtype, fs, ts, from_known=fk, to_known=tk))
        assert d.fallback == (not fk or not tk), f"폴백 판정 불일치: {mtype} {fs}->{ts}"


def test_hold_only_when_to_router_and_to_node_unknown():
    """`terminal_note` — 두 조건의 논리곱. 단 폐기·중계가 우선한다."""
    for mtype, fs, ts, fk, tk in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, [True, False], [True, False]
    ):
        d = _decide(_env(mtype, fs, ts, from_known=fk, to_known=tk))
        terminal = (ts == "router") and (not tk)
        if d.discard or d.relay:
            assert not d.hold, "폐기·중계가 보류에 우선해야 합니다"
        else:
            assert d.hold == terminal, f"보류 판정 불일치: {mtype} {fs}->{ts}"


def test_relayed_envelopes_are_not_rejudged():
    """`no_reemit_on_relayed` — 중계본은 다시 판정하지 않는다."""
    for mtype in ("cell_status", "cell_capability"):
        e = _env(mtype, "cell", "router", from_known=False)
        e["relayed_by"] = "nuc-01"
        d = _decide(e)
        assert not d.fallback, "중계본에 폴백을 다시 적용했습니다"
        assert not d.relay, "중계본을 다시 중계했습니다"


def test_routed_to_omitted_on_discard_and_hold():
    """`routed_to_on_discard` / `routed_to_on_hold` = omit"""
    for mtype, fs, ts, fk, tk in itertools.product(
        MTYPES, SYSTEMS, SYSTEMS, [True, False], [True, False]
    ):
        d = _decide(_env(mtype, fs, ts, from_known=fk, to_known=tk))
        if d.discard or d.hold:
            assert d.routed_to is rules.OMIT, "폐기·보류인데 routed_to 를 실었습니다"
        else:
            assert d.routed_to is not rules.OMIT, "배달되는데 routed_to 가 없습니다"


def test_simultaneous_discard_and_fallback_exists():
    """규격 `discard_note` — 폐기와 폴백이 동시에 성립하면 2011·2012 를 모두 발행한다."""
    hits = [
        (m, fk, tk)
        for m, fk, tk in itertools.product(MTYPES, [True, False], [True, False])
        if (d := _decide(_env(m, "fdw-ops", "fdw-ops", from_known=fk, to_known=tk))).discard
        and d.fallback
    ]
    assert hits, "동시 성립 사례가 하나도 없습니다 — 규칙 해석을 확인하십시오"
    d = _decide(_env("replanning_request", "fdw-ops", "fdw-ops", from_known=False))
    assert 2011 in d.event_codes and 2012 in d.event_codes, d.event_codes


def test_node_dictionary_parsed_from_spec():
    """노드 사전을 손으로 옮겨 적지 않고 규격에서 읽는지."""
    nodes = node_registry.node_dictionary()
    assert nodes.get("nuc-01") == "router"
    assert nodes.get("ops-01") == "fdw-ops"
    assert len(nodes) >= 12, f"사전이 {len(nodes)}건뿐입니다"
