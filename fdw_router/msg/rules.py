# -*- coding: utf-8 -*-
"""M축 판정기 — `routing_rules.json` 을 읽어 그대로 평가한다.

**조건을 코드에 하드코딩하지 않는다.** 규격 파일에서 읽어 쓴다. 규격이 개정되면
파일만 새로 읽으면 되고, 판정 코드는 그대로다.

v2.2 부터 판정은 **계층별**이다 (`rule_precedence.order_at_router` / `order_at_fdw_ops`).

    라우팅 계층(`router`)이 수신한 봉투
      ① unregistered_node_fallback   (배달 기준만 정하고 배달하지 않는다)
      ② discard_at_router            (폐기되면 거기서 끝)
      ③ relay_to_orchestrator        (폐기되지 않은 셀 상태·능력만)
      → 세 규칙을 끝까지 적용한 뒤 **한 번만** 배달한다
    오케스트레이션 계층(`fdw-ops`)이 수신한 봉투
      ④ relay_to_router              (셀 능력 공지 + 라우터 발행 지시의 응답 4종)

다섯 규칙(폴백·보류·폐기·중계 양방향) 모두 「어느 계층이 수신했는가」(`received_at`)를
조건으로 가진다. `relayed_by` 가 있는 중계본은 다시 중계·폴백하지 않고 종단 처리한다.
같은 `envelope_id` 를 원본과 중계본으로 두 번 받으면 뒤에 온 것을 폐기한다.

`decision_inputs_note` — 각 규칙이 판정에 쓰는 입력은 규격이 열거한 것이 전부다.
여기 없는 축(payload, 봉투 선택 필드 등)은 판정에 쓰지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import spec
from ..config import (
    ORCHESTRATOR_NODE_ID,
    ORCHESTRATOR_SYSTEM,
    ROUTER_NODE_ID,
    ROUTER_SYSTEM,
    STATEDB_NODE_ID,
)
from . import envelope as env_mod
from . import node_registry

OMIT = None  # routed_to 를 싣지 않음 (`routed_to_on_discard`/`on_hold` = "omit")

#: 계층 → 그 계층의 중계 노드 (`relayed_by` 에 적는 값). 정본은 `relayed_by_rule.value_must_be`.
_LAYER_NODE = {ROUTER_SYSTEM: ROUTER_NODE_ID, ORCHESTRATOR_SYSTEM: ORCHESTRATOR_NODE_ID}


@dataclass
class Decision:
    """봉투 하나에 대한 M축 판정 결과."""

    schema_reject: bool = False
    schema_errors: tuple[str, ...] = ()
    forged_relay: bool = False            # relayed_by 가 to 계층의 노드와 같음 (위조)

    fallback: bool = False
    discard: bool = False
    relay: bool = False
    hold: bool = False
    duplicate: bool = False               # 같은 envelope_id 를 두 번 받음 → 뒤의 것 폐기

    layer: str = "router"                 # 판정 계층 (received_at)
    relay_direction: str | None = None    # router_to_orchestration | orchestration_to_router
    routed_to: str | None = OMIT          # 최종 배달 계층 (폐기·보류 시 생략)
    relay_to: dict | None = None          # 중계 목적지 {system, node_id}
    relayed_envelope: dict | None = None
    relay_changed_keys: list[str] | None = None

    self_binding: str | None = None       # cell_capability 자기 정합
    node_binding: str | None = None       # cell_status 노드 공지 정합

    observed_node_id: str | None = None        # 수신 노드가 미등재일 때 to.node_id
    observed_from_node_id: str | None = None   # 발신 노드가 미등재일 때 from.node_id
    issuer_check_required: bool = False        # 응답 4종의 원 발행자 확인 불가 → 운영 로그
    event_codes: list[int] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return not (self.schema_reject or self.discard or self.hold or self.duplicate)


def _rules() -> dict:
    return spec.routing_rules()


def _relay_target(message_type: str, router_binding: dict | None) -> dict:
    """`orchestration_node_routing` 에 따라 중계 목적지 노드를 고른다."""
    onr = _rules()["orchestration_node_routing"]
    if message_type in onr.get("either", []):
        binding = (router_binding or {}).get(message_type)
        node = binding or onr.get("either_default", ORCHESTRATOR_NODE_ID)
    elif message_type in onr.get(STATEDB_NODE_ID, []):
        node = STATEDB_NODE_ID
    else:
        node = ORCHESTRATOR_NODE_ID
    return {"system": ORCHESTRATOR_SYSTEM, "node_id": node}


def _is_router_node(value: str | None) -> bool:
    return value is not None and node_registry.system_of(value) == ROUTER_SYSTEM


def _is_forged(e: dict) -> bool:
    """`relayed_by_rule.forbid_when` — 자기 계층으로 중계한 형태는 위조다.

    스키마가 같은 제약을 강제하므로 검증을 켠 경우 여기까지 오지 않는다. 검증을 끈
    호출(판정 입력 시험)에서도 같은 판정이 나오도록 규칙 파일의 금지 조합을 읽어 대조한다.
    """
    rb = e.get("relayed_by")
    if rb is None:
        return False
    rule = _rules().get("relayed_by_rule", {})
    to_sys = (e.get("to") or {}).get("system")
    for combo in (rule.get("forbid_when") or {}).get("forbidden_combinations", []):
        if combo.get("relayed_by") == rb and combo.get("to.system") == to_sys:
            return True
    # 중계 표시가 붙을 수 있는 종류 밖이면 역시 위조 (NEG-43)
    only = rule.get("only_on_message_types")
    if only and e.get("message_type") not in only:
        return True
    return False


def _eval_self_binding(e: dict) -> str | None:
    """`capability_self_binding` — cell_capability 봉투에만 적용."""
    if e.get("message_type") != "cell_capability":
        return None
    payload = e.get("payload") or {}
    if payload.get("announce_reason") == "join":
        return "exempt"                       # announce_reason_join
    if e.get("relayed_by") is not None:
        return "exempt"                       # relayed_by_present — 중계 방향과 무관
    declared = ((payload.get("interface") or {}).get("recipient_binding") or {}).get(
        "cell_capability"
    )
    if declared is None:
        # 공지하지 않으면 방향표 첫 값. 대조할 값이 없으므로 위반으로 보지 않는다.
        return "exempt"
    return "ok" if declared == e["to"].get("system") else "correction_requested"


def _eval_node_binding(e: dict, custody_binding: dict | None) -> str | None:
    """`node_binding_self_consistency` — cell_status 가 fdw-ops 로 직발신될 때만."""
    applies = _rules()["node_binding_self_consistency"]["applies_when"]
    if e.get("message_type") != applies["message_type"]:
        return "not_applicable"               # 적용 대상 종류가 아님 (TT-46)
    if e["to"].get("system") != applies["to_system"]:
        return "not_applicable"
    if _is_router_node(e.get("relayed_by")):
        return "exempt"                       # relayed_by_is_router_node
    known = (custody_binding or {}).get("cell_status")
    if known is None:
        return "exempt"                       # router_binding_not_yet_known
    return "ok" if e["to"].get("node_id") == known else "correction_requested"


def _apply_relay(e: dict, target: dict, relayed_by: str) -> dict:
    """`replace_to_only` — `to` 만 바꾸고 나머지 필드는 **열거 복사하지 않는다.**

    규격이 직접 경고한 함정이다. 옮길 필드를 열거하면 열거에서 빠진 필드가 사라진다.
    얕은 복사 후 두 키만 손댄다.
    """
    out = dict(e)
    out["to"] = dict(target)
    out["relayed_by"] = relayed_by
    return out


def evaluate(
    e: dict,
    *,
    layer: str = ROUTER_SYSTEM,
    router_known_binding: dict | None = None,
    custody_known_binding: dict | None = None,
    origin_issuer_resolved: str | None = None,
    seen_envelope_ids: set[str] | None = None,
    validate_schema: bool = True,
) -> Decision:
    """봉투 하나를 M축 규칙으로 판정한다.

    Parameters
    ----------
    layer
        판정 계층(`received_at`). `"router"`(nuc-01) 또는 `"fdw-ops"`(ops-01).
    origin_issuer_resolved
        응답 4종에 한해, 원 지시의 발행자 확인 결과 — `"router"` / `"fdw-ops"` / `"unknown"`
        (`relay_to_router.response_scope`). 확인은 `correlation_id` 로 발행 대장을 대조해
        호출자가 한다. 대장에 없다는 사실만으로 라우터 발행분으로 판단하지 않는다.
    seen_envelope_ids
        주면 중복 수신을 판정한다(`duplicate_handling`). 판정 후 이 집합에 추가한다.
    """
    d = Decision(layer=layer)

    # ── 0. 스키마 검증이 먼저다 (validation_order) ──────────────────
    if validate_schema:
        res = env_mod.validate(e)
        if not res.ok:
            d.schema_reject = True
            d.schema_errors = res.errors
            return d                      # 거절된 봉투는 어떤 판정도 받지 않는다
    if _is_forged(e):
        d.schema_reject = True
        d.forged_relay = True
        d.schema_errors = ("relayed_by: 자기 계층으로 중계한 형태 — 위조",)
        return d

    # ── 0b. 중복 수신 (duplicate_handling) — 판별 기준은 envelope_id ──
    eid = e.get("envelope_id")
    if seen_envelope_ids is not None and eid is not None:
        if eid in seen_envelope_ids:
            d.duplicate = True
            d.discard = True
            d.routed_to = OMIT
            return d
        seen_envelope_ids.add(eid)

    rules = _rules()
    prec = rules["rule_precedence"]
    order = prec.get("order_at_router" if layer == ROUTER_SYSTEM else "order_at_fdw_ops")
    if order is None:
        order = prec["order"]
    mtype = e.get("message_type")
    frm, to = e.get("from", {}), e.get("to", {})
    relayed = e.get("relayed_by") is not None      # 중계본 여부 (방향 무관)

    for rule_name in order:
        if rule_name == "unregistered_node_fallback":
            _apply_fallback(d, e, rules[rule_name], layer, relayed, frm, to)
        elif rule_name == "discard_at_router":
            _apply_discard(d, e, rules[rule_name], layer, mtype, frm, to)
        elif rule_name == "relay_to_orchestrator":
            _apply_relay_to_orchestrator(
                d, e, rules[rule_name], layer, relayed, mtype, to, router_known_binding
            )
        elif rule_name == "relay_to_router":
            _apply_relay_to_router(
                d, e, rules[rule_name], layer, relayed, mtype, to, origin_issuer_resolved
            )

    # ── 자기 정합 판정 (폐기·중계와 독립) ───────────────────────────
    d.self_binding = _eval_self_binding(e)
    d.node_binding = _eval_node_binding(e, custody_known_binding)

    # ── 최종 배달 계층과 2012 발행 ─────────────────────────────────
    if d.discard or d.hold:
        d.routed_to = OMIT                 # routed_to_on_discard / on_hold = omit
    elif d.relay:
        d.routed_to = d.relay_to["system"]
    else:
        d.routed_to = to.get("system")

    if d.fallback:
        d.event_codes.append(2012)         # event_emission: after_rules

    return d


# ── 규칙별 적용 ─────────────────────────────────────────────────────


def _applies_layer(rule: dict, layer: str) -> bool:
    """`applies_when.received_at` — 규칙이 적용되는 판정 계층인가."""
    aw = rule.get("applies_when") or {}
    want = aw.get("received_at", rule.get("applies_to", ROUTER_SYSTEM))
    if isinstance(want, list):
        return layer in want
    return layer == want


def _apply_fallback(d, e, fb, layer, relayed, frm, to) -> None:
    """① 미등재 노드 폴백 — 배달 기준만 정한다. 라우팅 계층·중계본 아님이 조건."""
    if not _applies_layer(fb, layer):
        return
    if (fb.get("applies_when") or {}).get("relayed_by_absent", True) and relayed:
        return                             # already_judged_at_router
    from_unknown = not node_registry.is_known(frm.get("node_id"))
    to_unknown = not node_registry.is_known(to.get("node_id"))
    if not (from_unknown or to_unknown):
        return
    d.fallback = True
    # observed_node_id_rule — 미등재 노드의 식별자만, 각자의 필드에
    if to_unknown:
        d.observed_node_id = to.get("node_id")
    if from_unknown:
        d.observed_from_node_id = frm.get("node_id")
    # hold_trigger — 네 조건의 논리곱: 판정 계층 · 중계본 아님 · to.system==router · to.node_id 미등재
    ht = fb.get("hold_trigger") or {}
    haw = ht.get("applies_when") or {}
    if (
        layer == haw.get("received_at", ROUTER_SYSTEM)
        and to.get("system") == haw.get("to_system", ROUTER_SYSTEM)
        and to_unknown
    ):
        d.hold = True


def _apply_discard(d, e, da, layer, mtype, frm, to) -> None:
    """② 계층 내부 메시지 폐기 — 라우팅 계층이 수신한 봉투에만."""
    aw = da.get("applies_when") or {}
    if layer != aw.get("received_at", ROUTER_SYSTEM):
        return
    not_types = da.get("message_type_not_in") or [aw.get("message_type_not")]
    if mtype in not_types:
        return
    if frm.get("system") == aw.get("from_system", da.get("from_system")) and to.get(
        "system"
    ) == aw.get("to_system", da.get("to_system")):
        d.discard = True
        d.hold = False                     # 폐기되면 거기서 끝
        d.event_codes.append(2011)


def _apply_relay_to_orchestrator(d, e, rel, layer, relayed, mtype, to, router_binding) -> None:
    """③ 라우팅 → 오케스트레이션 중계 (셀 상태·능력)."""
    if d.discard:
        return
    aw = rel.get("applies_when") or {}
    if layer != aw.get("received_at", ROUTER_SYSTEM):
        return
    if aw.get("relayed_by_absent", True) and relayed:
        return                             # re_relay_exclusion
    if mtype not in aw.get("message_types", rel.get("message_types", [])):
        return
    if to.get("system") != aw.get("to_system", ROUTER_SYSTEM):
        return
    d.relay = True
    d.relay_direction = "router_to_orchestration"
    d.hold = False                         # terminal_vs_relay: 중계가 보류에 우선
    d.relay_to = _relay_target(mtype, router_binding)
    d.relayed_envelope = _apply_relay(e, d.relay_to, _LAYER_NODE[layer])
    d.relay_changed_keys = sorted(["to", *rel.get("add_field", {"relayed_by": ""}).keys()])


def _apply_relay_to_router(d, e, rel, layer, relayed, mtype, to, issuer) -> None:
    """④ 오케스트레이션 → 라우팅 중계 (셀 능력 공지 + 라우터 발행 지시의 응답 4종)."""
    aw = rel.get("applies_when") or {}
    if layer != aw.get("received_at", ORCHESTRATOR_SYSTEM):
        return
    if aw.get("relayed_by_absent", True) and relayed:
        return                             # re_relay_exclusion — 중계본은 종단 처리
    if mtype not in aw.get("message_types", rel.get("message_types", [])):
        return
    if to.get("system") != aw.get("to_system", ORCHESTRATOR_SYSTEM):
        return
    # response_scope — 응답 4종은 원 지시 발행자가 라우팅 계층으로 **확인된** 것만
    if mtype != "cell_capability":
        if issuer == ROUTER_SYSTEM:
            pass
        elif issuer == ORCHESTRATOR_SYSTEM:
            return                         # 오케스트레이션 발행분 — 자체 처리
        else:
            d.issuer_check_required = True # 확인 불가 — 자동 중계하지 않고 운영 로그
            return
    d.relay = True
    d.relay_direction = "orchestration_to_router"
    d.relay_to = {"system": rel.get("to_system", ROUTER_SYSTEM),
                  "node_id": rel.get("to_node_id", ROUTER_NODE_ID)}
    d.relayed_envelope = _apply_relay(e, d.relay_to, _LAYER_NODE[layer])
    d.relay_changed_keys = sorted(["to", *rel.get("add_field", {"relayed_by": ""}).keys()])
