# -*- coding: utf-8 -*-
"""봉투 스키마 검증 (M축 1단계).

`routing_rules.validation_order` 가 못박은 순서를 지킨다 —
**먼저 스키마로 검증하고, 통과한 것만 세 규칙에 넣는다.**
거절된 봉투는 폐기·중계·폴백 어느 판정도 받지 않는다.

`00_OVERVIEW.md` §2 가 경고한 두 가지를 반드시 지킨다.
  1. `schemas/` 전부를 Registry 에 올린다 — 빠뜨리면 형제 스키마 참조가 풀리지 않는다.
  2. `FormatChecker` 를 함께 넘긴다 — 빠뜨리면 UUID·ISO8601 검사가 통지 없이 생략된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jsonschema
from referencing import Registry, Resource

from .. import spec


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - 편의
        return self.ok


@lru_cache(maxsize=1)
def _registry() -> Registry:
    reg = Registry()
    for name, doc in spec.schemas().items():
        res = Resource.from_contents(doc)
        reg = reg.with_resource(name, res)
        if "$id" in doc:
            reg = reg.with_resource(doc["$id"], res)
    return reg


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        spec.schemas()["envelope.schema.json"],
        format_checker=jsonschema.FormatChecker(),
        registry=_registry(),
    )


def validate(envelope: dict) -> ValidationResult:
    """봉투 하나를 규격 스키마로 검증한다."""
    errs = tuple(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _validator().iter_errors(envelope)
    )
    return ValidationResult(ok=not errs, errors=errs)


def is_valid(envelope: dict) -> bool:
    return validate(envelope).ok
