#!/usr/bin/env bash
# fdw-router 로컬 실행 (Linux / macOS / 원격 GPU 호스트) — run_local.ps1 과 같은 일을 한다.
# 사용: bash deploy/run_local.sh [--skip-tests] [--router-only] [--no-fetch] [--recreate]
#   환경변수: FDW_SPEC_ROOT (규격 v2.2 폴더), FDW_OPS_ROOT (fdw-ops 소스), PYTHON (기준 python, 기본 python3)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONIOENCODING=utf-8

SKIP_TESTS=0; ROUTER_ONLY=0; NO_FETCH=0; RECREATE=0
for a in "$@"; do
  case "$a" in
    --skip-tests) SKIP_TESTS=1 ;;
    --router-only) ROUTER_ONLY=1 ;;
    --no-fetch) NO_FETCH=1 ;;
    --recreate) RECREATE=1 ;;
    *) echo "모르는 옵션: $a" >&2; exit 2 ;;
  esac
done

# 1) 규격 정본
if [ -z "${FDW_SPEC_ROOT:-}" ]; then
  SPEC_REL="FDW_Y1_DataDefinition_v2.2"
  for base in "$(dirname "$(dirname "$REPO")")" "$(dirname "$REPO")"; do
    if [ -d "$base/$SPEC_REL" ]; then export FDW_SPEC_ROOT="$base/$SPEC_REL"; break; fi
  done
fi
if [ ! -d "${FDW_SPEC_ROOT:-/nonexistent}" ]; then
  echo "규격 패키지가 없습니다. FDW_SPEC_ROOT 에 FDW_Y1_DataDefinition_v2.2 폴더 경로를 넣으세요." >&2; exit 1
fi

# 2) fdw-ops 소스
export FDW_OPS_ROOT="${FDW_OPS_ROOT:-$(dirname "$REPO")/external/fdw-ops}"
if [ ! -d "$FDW_OPS_ROOT/fdw_sim" ] && [ "$NO_FETCH" -eq 0 ]; then
  echo "== fdw-ops 소스가 없어 GitHub 에서 받습니다 → $FDW_OPS_ROOT"
  git clone -q --depth 1 -b fix/fdw-quality-disposition-base-pose https://github.com/opt1064/fdw-ops.git "$FDW_OPS_ROOT"
fi

# 3) 가상환경 — 다른 PC 에서 만든 .venv 는 기준 python 이 없어 실패하므로 감지해서 새로 만든다
VENV="$REPO/.venv"; PY="$VENV/bin/python"
if [ "$RECREATE" -eq 1 ] || { [ -d "$VENV" ] && ! "$PY" -c "import sys" >/dev/null 2>&1; }; then
  echo "== 기존 .venv 가 이 PC 에서 동작하지 않거나 --recreate 지정 → 다시 만듭니다"; rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.11
  else
    BASE="${PYTHON:-python3}"
    "$BASE" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "Python 3.10 이상이 필요합니다 (PYTHON=<경로> 로 지정 가능)" >&2; exit 1; }
    "$BASE" -m venv "$VENV"
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" -q -r requirements.txt -r requirements-dev.txt paho-mqtt
else
  "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt -r requirements-dev.txt paho-mqtt
fi

echo; echo "== Python        = $("$PY" -c 'import sys; print(sys.version.split()[0])') ($PY)"
echo "== FDW_SPEC_ROOT = $FDW_SPEC_ROOT"; echo "== FDW_OPS_ROOT  = $FDW_OPS_ROOT"; echo

# 4) 시험 전건
[ "$SKIP_TESTS" -eq 1 ] || "$PY" -X utf8 -m pytest -q

# 5) fdw-ops 위에서 라우터 배정
if [ -d "$FDW_OPS_ROOT/fdw_sim" ]; then
  ARGS=(-X utf8 -m fdw_router.integrations.fdwops --mode discrete)
  [ "$ROUTER_ONLY" -eq 1 ] && ARGS+=(--router-only)
  "$PY" "${ARGS[@]}"
else
  echo "fdw-ops 소스가 없어 통합 실행을 건너뜁니다 ($FDW_OPS_ROOT)" >&2
fi
