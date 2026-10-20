# -*- coding: utf-8 -*-
"""규격이 반드시 거절해야 하는 봉투 41건.

`00_OVERVIEW.md` §0.1 이 못박은 성격 —
*"`negative_fixtures.json` 은 예시가 아니라 「규격이 반드시 거절하여야 하는 봉투」 41건이다.
자기 검증기를 시험하는 용도이며 작성 견본으로 복사하지 않는다."*

통과하는 예시만으로는 검증기가 느슨해진 것을 알 수 없다. **1건이라도 통과하면
우리 검증기가 규격보다 느슨한 것이다.**
"""
from __future__ import annotations

import pytest

from fdw_router import spec
from fdw_router.msg import envelope, rules

CASES = spec.negative_fixtures()


def _ids(cases):
    return [c.get("id", str(i)) for i, c in enumerate(cases)]


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_must_be_rejected(case):
    res = envelope.validate(case["envelope"])
    assert not res.ok, (
        f"통과해서는 안 되는 봉투가 통과했습니다 — {case.get('id')}: "
        f"{case.get('why') or case.get('reason') or ''}"
    )


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_rejected_envelope_gets_no_routing_decision(case):
    """`validation_order` — 거절된 봉투는 폐기·중계·폴백 어느 판정도 받지 않는다."""
    d = rules.evaluate(case["envelope"])
    assert d.schema_reject
    assert not (d.discard or d.relay or d.fallback or d.hold)


def test_all_fixtures_present():
    assert len(CASES) >= 41, f"부정 표본이 {len(CASES)}건뿐입니다 — 배포본을 확인하십시오."


def test_format_checker_is_active():
    """FormatChecker 를 빠뜨리면 형식 오류가 통지 없이 통과한다 — 그 회귀를 막는다."""
    base = dict(spec.message_type_catalog()[0])
    base["envelope_id"] = "not-a-uuid"
    assert not envelope.validate(base).ok, "UUID 형식 검사가 꺼져 있습니다"

    base2 = dict(spec.message_type_catalog()[0])
    base2["timestamp_iso"] = "2026-13-45T99:99:99"
    assert not envelope.validate(base2).ok, "시각 형식 검사가 꺼져 있습니다"


def test_catalog_examples_all_valid():
    """규격이 준 정상 예시 20종은 전건 통과해야 한다 (검증기가 과하게 빡빡하지 않은지)."""
    bad = [
        m["message_type"]
        for m in spec.message_type_catalog()
        if not envelope.validate(m).ok
    ]
    assert not bad, f"정상 예시가 거절되었습니다: {bad}"
