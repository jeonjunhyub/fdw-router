# -*- coding: utf-8 -*-
"""run_demo_3d.py 가 저장한 Isaac Sim 프레임(원본 · 라우터)을 나란히 붙여 MP4 와 대표 PNG 를 만든다.

실행 (fdw-router .venv — Pillow 필요, ffmpeg 가 PATH 에 있어야 한다):
  ..\\.venv\\Scripts\\python.exe -X utf8 compose_3d.py [--fps 10]
입력: output/frames_3d/{original,router}/f_*.png · output/demo_timeline_{original,router}_3d.json
출력: output/demo_3d.mp4 · output/demo_3d_frame_{start,mid,end}.png · output/demo_3d_router_only.mp4
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("FDW_DEMO_OUT") or HERE / "output")   # run_demo.py 와 같은 규칙
FRAMES = OUT / "frames_3d"
W, H = 1920, 880                       # 출력 캔버스 (16:9 패널 둘 + 머리띠 · 정보줄)
PANEL_W, PANEL_H = 940, 529            # 16:9 패널 둘 (좌: 원본, 우: 라우터) — 홀 전체가 보이는 넓은 구도라 자르지 않는다
TOP = 70                               # 머리띠 (제목 · 시각)
INK, SLATE, LINE = (15, 23, 42), (100, 116, 139), (226, 232, 240)
BLUE, GREEN, RED = (37, 99, 235), (5, 150, 105), (220, 38, 38)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (["malgunbd.ttf", "malgun.ttf"] if bold else ["malgun.ttf"]):
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


F_T, F_L, F_S = font(30, True), font(24, True), font(20)


def load_run(label: str) -> dict:
    d = json.loads((OUT / f"demo_timeline_{label}_3d.json").read_text(encoding="utf-8"))
    files = sorted((FRAMES / label).glob("f_*.png"))
    d["files"] = files
    d["t_of"] = [f["t"] for f in d["frames"]][:len(files)]
    return d


def frame_at(run: dict, t: float) -> tuple[Path, bool]:
    """시각 t 에 해당하는 프레임 파일. 끝난 뒤면 마지막 프레임과 done=True."""
    ts = run["t_of"]
    if not ts:
        raise RuntimeError("no frames")
    if t >= ts[-1]:
        return run["files"][len(ts) - 1], True
    idx = max(i for i, tt in enumerate(ts) if tt <= t)
    return run["files"][idx], False


def completed_at(run: dict, t: float) -> int:
    return sum(1 for e in run["events"] if e["kind"] == "completed" and e["t"] <= t)


def compose(orig: dict, rout: dict, t: float, cache: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, TOP], fill=INK)
    dr.text((24, 18), "AI 라우터 시연 (Isaac Sim 3D) — 같은 주문 8건, 용접대 2 · 검사대 2", font=F_T, fill="white")
    dr.text((W - 24, 20), f"시뮬레이션 {t:6.0f} s", font=F_L, fill=(203, 213, 225), anchor="ra")
    gap = (W - 2 * PANEL_W) // 3
    y0 = TOP + 60
    for k, (run, title, color) in enumerate(((orig, "A. 원본 공장장 — 고정 표 (용접은 전부 WELD_01)", SLATE),
                                            (rout, "B. 라우터 — 할 수 있는 곳 → 급한 순서 → 가장 싼 작업대(비용, 초)", BLUE))):
        x0 = gap + k * (PANEL_W + gap)
        path, done = frame_at(run, t)
        if path not in cache:
            cache.clear()
            cache[path] = Image.open(path).convert("RGB").resize((PANEL_W, PANEL_H), Image.LANCZOS)
        img.paste(cache[path], (x0, y0))
        dr.rectangle([x0, y0 - 44, x0 + PANEL_W, y0 - 4], fill=color)
        dr.text((x0 + 14, y0 - 38), title, font=F_L, fill="white")
        n = completed_at(run, t)
        dr.rectangle([x0, y0 + PANEL_H + 6, x0 + PANEL_W, y0 + PANEL_H + 56], fill=(248, 250, 252), outline=LINE)
        dr.text((x0 + 14, y0 + PANEL_H + 18), f"완료 {n} / 8 건", font=F_L, fill=INK)
        last = run["result"]["last_completion_s"]
        if done:
            dr.text((x0 + PANEL_W - 14, y0 + PANEL_H + 18), f"전부 완료 {last:.0f} s", font=F_L, fill=GREEN if run is rout else RED, anchor="ra")
        else:
            busy = [cid for cid, st in run["frames"][min(int(t), len(run["frames"]) - 1)]["cells"].items() if st == "PROCESSING"]
            dr.text((x0 + PANEL_W - 14, y0 + PANEL_H + 18), "가동: " + (", ".join(busy) if busy else "—"), font=F_S, fill=SLATE, anchor="ra")
    foot = y0 + PANEL_H + 80
    o, r = orig["result"]["last_completion_s"], rout["result"]["last_completion_s"]
    dr.text((gap, foot), f"완료까지  원본 {o:.0f} s  vs  라우터 {r:.0f} s  ({(1 - r / o) * 100:.0f}% 단축)   ·   라우터 결정 {rout['result'].get('router_decisions', 0)}건, \"못 정하겠어요\" 알림(2014) {rout['result'].get('replan_2014_events', 0)}건",
            font=F_L, fill=INK)
    dr.text((gap, foot + 36), "fdw-ops Isaac Sim 모드(Level 2) · 같은 seed · 1 프레임 = 시뮬레이션 1 초 · 화면은 fdw-ops 의 USD 장면(작업대 · AMR · 로봇팔), 배정만 라우터가 한다",
            font=F_S, fill=SLATE)
    return img


def encode(frames_iter, path: Path, fps: int):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = 0
    for im in frames_iter:
        p.stdin.write(im.tobytes()); n += 1
    p.stdin.close(); p.wait()
    return n


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--fps", type=int, default=10); args = ap.parse_args()
    orig, rout = load_run("original"), load_run("router")
    t_max = max(orig["t_of"][-1], rout["t_of"][-1])
    ts = [float(i) for i in range(int(t_max) + 1)] + [t_max] * (2 * args.fps)
    cache: dict = {}

    def gen():
        for t in ts:
            yield compose(orig, rout, t, cache)
    n = encode(gen(), OUT / "demo_3d.mp4", args.fps)
    print("demo_3d.mp4 frames", n, "len", round(n / args.fps, 1), "s")
    for name, t in (("start", 5.0), ("mid", min(200.0, t_max / 2)), ("end", t_max)):
        compose(orig, rout, t, cache).save(OUT / f"demo_3d_frame_{name}.png")
    # 라우터 단독 원본 해상도 영상 (발표 때 크게 보여줄 용도)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(args.fps), "-i", str(FRAMES / "router" / "f_%05d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(OUT / "demo_3d_router_only.mp4")]
    subprocess.run(cmd, check=True)
    print("done →", OUT)


if __name__ == "__main__":
    main()
