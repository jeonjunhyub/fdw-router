# -*- coding: utf-8 -*-
"""규격 패키지 로더.

라우터는 규격을 **복사하지 않는다.** 배포된 `FDW_Y1_DataDefinition_v2.x/` 를 그대로 읽는다.
규격이 개정되면 이 경로만 새 배포본으로 바꾸면 되고, 라우터 코드는 손대지 않는다.

현행 기준선은 **v2.2 (2026-09-20 배포, `schema_version = fdw.v1.1`)** 다.
v2.1 은 `fdw.v1.0` 기준선으로 동결되었고(CHANGELOG v2.2), 폴백 후보로만 남겨 둔다.

경로 우선순위
  1. 환경변수 ``FDW_SPEC_ROOT``
  2. 기본 경로 — 저장소 한 단계 위 또는 두 단계 위에 놓인 `FDW_Y1_DataDefinition_v2.2` 폴더
     (v2.2 를 먼저 찾고, 없으면 v2.1)
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_BASES = [Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[3]]   # 저장소 한 단계 위, 두 단계 위
_DEFAULT_CANDIDATES = [b / name for name in ("FDW_Y1_DataDefinition_v2.2", "FDW_Y1_DataDefinition_v2.1") for b in _BASES]


class SpecNotFound(RuntimeError):
    pass


@lru_cache(maxsize=1)
def spec_root() -> Path:
    env = os.environ.get("FDW_SPEC_ROOT")
    if env:
        p = Path(env)
        if not p.is_dir():
            raise SpecNotFound(f"FDW_SPEC_ROOT 가 가리키는 경로가 없습니다: {p}")
        return p
    for c in _DEFAULT_CANDIDATES:
        if c.is_dir():
            return c
    raise SpecNotFound(
        "규격 패키지를 찾지 못했습니다. 환경변수 FDW_SPEC_ROOT 에 "
        "FDW_Y1_DataDefinition_v2.2 폴더 경로를 지정하십시오."
    )


def _load_json(rel: str):
    p = spec_root() / rel
    if not p.is_file():
        raise SpecNotFound(f"규격 파일이 없습니다: {p}")
    with p.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def index() -> dict:
    """`00_INDEX.json` — 패키지 진입 인덱스. 정본 파일의 위치를 여기서 읽는다."""
    return _load_json("00_INDEX.json")


def package_version() -> str:
    """패키지 버전 (예: `"2.2"`)."""
    return str(index().get("package_version", ""))


def schema_version() -> str:
    """메시지 규격 버전 (예: `"fdw.v1.1"`). 정본은 `envelope.schema.json` 의 enum 이다."""
    prop = schemas()["envelope.schema.json"]["properties"]["schema_version"]
    if "const" in prop:
        return prop["const"]
    enum = prop.get("enum") or []
    if len(enum) != 1:
        raise SpecNotFound(f"envelope.schema.json 의 schema_version 이 단일값이 아닙니다: {enum}")
    return enum[0]


def guideline_md() -> Path:
    """공통 규격 본문(`T2_guideline_v*.md`). INDEX 의 `canonical_sources.common_spec` 가 정본."""
    rel = (index().get("canonical_sources") or {}).get("common_spec")
    if rel:
        p = spec_root() / rel
        if p.is_file():
            return p
    cands = sorted((spec_root() / "T2_guideline").glob("T2_guideline_v*.md"))
    if not cands:
        raise SpecNotFound("T2_guideline/T2_guideline_v*.md 를 찾지 못했습니다.")
    return cands[-1]          # 파일명 정렬상 가장 높은 버전


@lru_cache(maxsize=1)
def routing_rules() -> dict:
    """`T2_guideline/routing_rules.json` — M축 판정의 정본."""
    return _load_json("T2_guideline/routing_rules.json")


@lru_cache(maxsize=1)
def schemas() -> dict[str, dict]:
    """`T2_guideline/schemas/*.schema.json` 전부. 파일명 -> 내용."""
    out = {}
    for p in sorted((spec_root() / "T2_guideline" / "schemas").glob("*.schema.json")):
        with p.open(encoding="utf-8") as f:
            out[p.name] = json.load(f)
    if not out:
        raise SpecNotFound("schemas/ 에 스키마가 없습니다.")
    return out


@lru_cache(maxsize=1)
def negative_fixtures() -> list[dict]:
    """규격이 반드시 거절해야 하는 봉투 (v2.1: 41건, v2.2: 44건)."""
    return _load_json("T2_guideline/examples/negative_fixtures.json")["cases"]


@lru_cache(maxsize=1)
def truth_table() -> list[dict]:
    """M축 판정 진리표 (v2.1: 49건, v2.2: 64건). 건수는 규격이 정한다."""
    return routing_rules()["truth_table"]


@lru_cache(maxsize=1)
def message_type_catalog() -> list[dict]:
    return _load_json("T2_guideline/examples/message_type_catalog.json")["envelopes"]
