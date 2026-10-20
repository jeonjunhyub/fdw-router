# -*- coding: utf-8 -*-
"""로컬 배포 zip 생성기.

    uv run python -I tools/make_release.py [--out <폴더>]

산출: <out>/fdw-router_v<VERSION>_<YYYYMMDD>.zip
내용: 저장소 추적 파일 전부(.git 제외) + deploy/ + CHANGELOG + README + 규격 경로 안내.
규격 패키지 자체는 규격 배포본이므로 넣지 않고, README_배포.md 가 경로를 안내한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "logs", "runs", "output"}
EXCLUDE_SUFFIX = {".pyc", ".jsonl", ".parquet"}


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in EXCLUDE_SUFFIX:
                continue
            yield p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "release"))# <저장소 상위>\배포
    args = ap.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"fdw-router_v{version}_{dt.date.today():%Y%m%d}.zip"
    dst = out_dir / name
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for p in iter_files():
            z.write(p, f"fdw-router/{p.relative_to(ROOT).as_posix()}")
    print("saved", dst, f"({dst.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
