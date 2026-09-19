#!/usr/bin/env python3
"""Run the corrected 5 x 4 x 4 IMX477 CCM/WB sweep on the Radxa.

The script is intentionally self-contained and resumable. It changes only
bank 2's nine CCM coefficients and the Bayer R/G and B/G gains in each
profile. Capture artifacts and evaluator reports remain on the Radxa under
fixed-ccm-sweep/.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tqdm import tqdm


HEADER_SIZE = 4 + 20 + 50
PAYLOAD_SIZE = 0x1C63C
CCM_OFFSETS = (0x154AA, 0x154C2, 0x154DA)
BANK2 = CCM_OFFSETS[2]
FRAME_COUNT = 10
WIDTH, HEIGHT, FPS = 3840, 2160, 30
ALPHAS = (0.0, 0.25, 0.5, 0.75, 1.0)
RG_GAINS = (0.65, 0.80, 0.95, 1.10)
BG_GAINS = (1.00, 1.15, 1.30, 1.45)
FALLBACK_BANK2 = (397, -141, 0, -68, 442, -118, 0, -140, 396, 0, 0, 0)
IMX477_BANK2 = (394, -51, 14, -52, 433, -116, -85, -126, 358, 0, 0, 0)


def dbg(msg: str) -> None:
    print(f"[debug] {msg}", flush=True)


def run(cmd: list[str], *, cwd: Path | None = None,
        stdout=None, stderr=None, check: bool = True) -> subprocess.CompletedProcess:
    dbg("$ " + shlex.join(cmd))
    result = subprocess.run(cmd, cwd=cwd, stdout=stdout, stderr=stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sample_id(alpha: float, rg: float, bg: float) -> str:
    return f"a{round(alpha*100):03d}_rg{round(rg*100):03d}_bg{round(bg*100):03d}"


def rounded_ccm(alpha: float) -> tuple[int, ...]:
    return tuple(round(a + alpha * (b - a)) for a, b in zip(FALLBACK_BANK2[:9], IMX477_BANK2[:9])) + (0, 0, 0)


def replace_phone_config(nominal: Path, phone_stats: Path, output: Path) -> None:
    cfg = json.loads(nominal.read_text())
    stats = json.loads(phone_stats.read_text())
    phone_by_id = {str(item["id"]): item["median_rgb"] for item in stats["results"]}
    for patch in cfg["patches"]:
        if patch["id"] not in phone_by_id:
            raise ValueError(f"phone stats missing patch {patch['id']}")
        patch["rgb"] = [int(round(x)) for x in phone_by_id[patch["id"]]]
    cfg["reference_source"] = str(phone_stats)
    cfg["source_color_space"] = "phone-observed RGB medians"
    output.write_text(json.dumps(cfg, indent=2) + "\n")


def summary_from(path: Path) -> dict:
    data = json.loads(path.read_text())
    return data.get("summary", {})


def flatten_row(row: dict) -> dict:
    n = row.get("nominal", {})
    p = row.get("phone", {})
    c = row["ccm"]
    return {
        "sample_id": row["sample_id"], "alpha": row["alpha"],
        "rg_gain": row["rg_gain"], "bg_gain": row["bg_gain"],
        **{f"ccm_{name}": c[i] for i, name in enumerate((
            "m00", "m01", "m02", "m10", "m11", "m12", "m20", "m21", "m22", "offR", "offG", "offB"))},
        "nominal_mean_similarity": n.get("mean_similarity_percent"),
        "nominal_median_similarity": n.get("median_similarity_percent"),
        "nominal_mae": n.get("mean_patch_mae"),
        "nominal_rmse": n.get("mean_patch_rmse"),
        "nominal_bias_r": (n.get("mean_signed_bias_rgb") or [None]*3)[0],
        "nominal_bias_g": (n.get("mean_signed_bias_rgb") or [None]*3)[1],
        "nominal_bias_b": (n.get("mean_signed_bias_rgb") or [None]*3)[2],
        "phone_mean_similarity": p.get("mean_similarity_percent"),
        "phone_median_similarity": p.get("median_similarity_percent"),
        "phone_mae": p.get("mean_patch_mae"),
        "phone_rmse": p.get("mean_patch_rmse"),
        "phone_bias_r": (p.get("mean_signed_bias_rgb") or [None]*3)[0],
        "phone_bias_g": (p.get("mean_signed_bias_rgb") or [None]*3)[1],
        "phone_bias_b": (p.get("mean_signed_bias_rgb") or [None]*3)[2],
        "capture_filename": row["capture_filename"],
        "profile_filename": row["profile_filename"],
        "report_filename": row["report_filename"],
    }


def write_tables(root: Path, rows: list[dict]) -> None:
    out = root / "sweep-results.json"
    out.write_text(json.dumps(rows, indent=2) + "\n")
    flat = [flatten_row(r) for r in rows]
    if flat:
        with (root / "sweep-results.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(flat[0]))
            writer.writeheader(); writer.writerows(flat)


def mean(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def analyse(root: Path, rows: list[dict], previous: dict | None) -> None:
    flat = [flatten_row(r) for r in rows]
    phone_rows = [r for r in flat if r.get("phone_mean_similarity") is not None]
    nominal_rows = [r for r in flat if r.get("nominal_mean_similarity") is not None]
    top_phone = sorted(phone_rows, key=lambda r: r["phone_mean_similarity"], reverse=True)[:10]
    top_nominal = sorted(nominal_rows, key=lambda r: r["nominal_mean_similarity"], reverse=True)[:10]
    trends = {}
    for dim in ("alpha", "rg_gain", "bg_gain"):
        vals = sorted({r[dim] for r in flat})
        trends[dim] = {str(v): {
            "count": sum(r[dim] == v for r in flat),
            "phone_mean_similarity": mean([r for r in flat if r[dim] == v], "phone_mean_similarity"),
            "nominal_mean_similarity": mean([r for r in flat if r[dim] == v], "nominal_mean_similarity"),
        } for v in vals}
    analysis = {"schema": "fixed-ccm-sweep-analysis-v1", "count": len(flat),
                "top10_phone": top_phone, "top10_nominal": top_nominal,
                "trends": trends, "previous_best": previous}
    (root / "reports" / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
    lines = ["# Corrected CCM 80-point sweep", "", f"Completed samples: {len(flat)} / 80", "",
             "The sweep changes only bank 2, R/G Bayer gain, and B/G Bayer gain. CCM packing is 3×3 + RGB offsets; offsets are zero.", "",
             "## Top 10 by phone-reference similarity", "", "| sample | alpha | R/G | B/G | phone similarity | phone MAE | bias RGB |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in top_phone:
        lines.append(f"| {r['sample_id']} | {r['alpha']:.2f} | {r['rg_gain']:.2f} | {r['bg_gain']:.2f} | {r['phone_mean_similarity']:.3f} | {r['phone_mae']:.3f} | ({r['phone_bias_r']:.1f}, {r['phone_bias_g']:.1f}, {r['phone_bias_b']:.1f}) |")
    lines += ["", "## Top 10 by nominal printed-RGB similarity", "", "| sample | alpha | R/G | B/G | nominal similarity | nominal MAE | bias RGB |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in top_nominal:
        lines.append(f"| {r['sample_id']} | {r['alpha']:.2f} | {r['rg_gain']:.2f} | {r['bg_gain']:.2f} | {r['nominal_mean_similarity']:.3f} | {r['nominal_mae']:.3f} | ({r['nominal_bias_r']:.1f}, {r['nominal_bias_g']:.1f}, {r['nominal_bias_b']:.1f}) |")
    lines += ["", "## Marginal trends", ""]
    for dim, vals in trends.items():
        lines += [f"### {dim}", "", "value | phone average | nominal average", "---:|---:|---:"]
        for v, x in vals.items(): lines.append(f"{v} | {x['phone_mean_similarity'] if x['phone_mean_similarity'] is not None else 'n/a'} | {x['nominal_mean_similarity'] if x['nominal_mean_similarity'] is not None else 'n/a'}")
        lines.append("")
    if top_phone:
        best = top_phone[0]
        lines += ["## Boundary check", "", f"Best phone sample: `{best['sample_id']}`."]
        for dim, values in (("alpha", ALPHAS), ("rg_gain", RG_GAINS), ("bg_gain", BG_GAINS)):
            edge = best[dim] in (min(values), max(values))
            lines.append(f"- `{dim}` = `{best[dim]}` is {'on' if edge else 'inside'} the tested range.")
    lines += ["", "## Previous-best comparison", ""]
    if previous:
        lines += [f"Previous broken-packing best nominal similarity: {previous.get('mean_similarity_percent', 'n/a'):.3f}%, MAE {previous.get('mean_patch_mae', 'n/a'):.3f}, bias {previous.get('bias', 'n/a')}."]
        if top_nominal:
            b = top_nominal[0]
            lines.append(f"Best corrected-packing nominal result: {b['nominal_mean_similarity']:.3f}%, MAE {b['nominal_mae']:.3f}, bias ({b['nominal_bias_r']:.2f}, {b['nominal_bias_g']:.2f}, {b['nominal_bias_b']:.2f}).")
    else:
        lines.append("Previous-best metrics were not available to this run.")
    (root / "README.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/home/radxa/imx477-build/work/fixed-ccm-sweep"))
    ap.add_argument("--payload", type=Path, default=Path("/home/radxa/imx477-build/work/ov13850-fallback-active.cfg"))
    ap.add_argument("--builder", type=Path, default=Path("/home/radxa/imx477-build/work/build-imx477-isp-bin.py"))
    ap.add_argument("--toolkit", type=Path, default=Path("/home/radxa/imx477-build/work/isp_eval_toolkit"))
    ap.add_argument("--sudo-password", default=os.environ.get("IMX477_SUDO_PASSWORD", "radxa"))
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    root = args.root; profiles = root / "profiles"; captures = root / "captures"; reports = root / "reports"; scripts = root / "scripts"
    for d in (profiles, captures, reports, scripts): d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.builder, scripts / "build-imx477-isp-bin.py")
    shutil.copy2(Path(__file__), scripts / Path(__file__).name)
    fallback_copy = root / "fallback-payload.cfg"
    if not fallback_copy.exists(): shutil.copy2(args.payload, fallback_copy)
    phone_stats = root / "phone-reference-stats.json"
    nominal_cfg = args.toolkit / "isp_color_target_config.json"
    phone_cfg = root / "phone-reference-config.json"
    if not phone_cfg.exists(): replace_phone_config(nominal_cfg, phone_stats, phone_cfg)
    marker_json = args.toolkit / "markers.json"
    vpy = args.toolkit / ".venv/bin/python3"
    checkpoint_path = root / "checkpoint.json"
    state = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() and args.resume else {"version": 1, "samples": {}}
    active_backup = root / "backups/active-before-sweep.bin"; active_backup.parent.mkdir(parents=True, exist_ok=True)
    if not active_backup.exists():
        p = subprocess.run(["sudo", "-S", "-p", "", "cp", "/mnt/isp_param_config.bin", str(active_backup)], input=(args.sudo_password + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode: raise RuntimeError("active profile backup failed: " + p.stderr.decode(errors="replace"))
    if not (root / "backups/active-before-sweep.sha256").exists():
        (root / "backups/active-before-sweep.sha256").write_text(sha256(active_backup) + "\n")
    previous = {"mean_similarity_percent": 76.05, "mean_patch_mae": 61.08, "bias": [-22.22, -21.25, -3.72]}
    rows_by_id = {r["sample_id"]: r for r in state.get("rows", [])}
    candidates = [(a, rg, bg) for a in ALPHAS for rg in RG_GAINS for bg in BG_GAINS]
    try:
        for alpha, rg, bg in tqdm(candidates, desc="fixed CCM sweep", unit="sample"):
            sid = sample_id(alpha, rg, bg); d = captures / sid; d.mkdir(parents=True, exist_ok=True)
            if state["samples"].get(sid, {}).get("status") == "done" and (d / "nominal_eval.json").exists():
                dbg(f"skip completed {sid}"); continue
            ccm = rounded_ccm(alpha)
            profile = profiles / f"{sid}.bin"
            if not profile.exists():
                run([sys.executable, str(args.builder), str(fallback_copy), str(profile), "--blend-imx477", str(alpha), "--rg-gain", str(rg), "--bg-gain", str(bg), "--bank", "2"])
            data = profile.read_bytes()
            actual = tuple(__import__("struct").unpack_from("<12h", data, HEADER_SIZE + BANK2))
            if actual != ccm: raise RuntimeError(f"CCM sanity mismatch for {sid}: {actual} != {ccm}")
            state["samples"][sid] = {"status": "profile_ready", "alpha": alpha, "rg_gain": rg, "bg_gain": bg, "ccm": ccm, "profile": str(profile), "profile_sha256": sha256(profile)}
            checkpoint_path.write_text(json.dumps(state, indent=2) + "\n")
            p = subprocess.run(["sudo", "-S", "-p", "", "install", "-m", "0644", str(profile), "/mnt/isp_param_config.bin"], input=(args.sudo_password + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode: raise RuntimeError(p.stderr.decode(errors="replace"))
            run(["v4l2-ctl", "-d", "/dev/video1", "--set-ctrl", "white_balance_automatic=0,white_balance_temperature=5600"])
            raw = Path("/tmp/fixed-ccm-sweep.nv12")
            run(["rm", "-f", str(raw)])
            env = os.environ.copy(); env["LD_PRELOAD"] = "/usr/local/lib/libisp_no3dn.so"
            with (d / "capture.log").open("w") as log:
                r = subprocess.run(["gst-launch-1.0", "-e", "v4l2src", "device=/dev/video1", "io-mode=2", "num-buffers=10", "!", "video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1", "!", "filesink", f"location={raw}"], env=env, stdout=log, stderr=subprocess.STDOUT)
            if r.returncode: raise RuntimeError(f"gstreamer failed: {r.returncode}")
            if raw.stat().st_size != WIDTH * HEIGHT * 3 // 2 * FRAME_COUNT: raise RuntimeError(f"bad raw size for {sid}")
            state["samples"][sid].update({"status": "captured", "frames": 0}); checkpoint_path.write_text(json.dumps(state, indent=2) + "\n")
            capture = d / "capture.jpg"
            for i in range(FRAME_COUNT):
                target = d / ("capture.jpg" if i == FRAME_COUNT - 1 else f".frame-{i:02d}.jpg")
                run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pixel_format", "nv12", "-video_size", "3840x2160", "-framerate", "30", "-i", str(raw), "-vf", f"select=eq(n\\,{i})", "-frames:v", "1", "-q:v", "2", str(target)])
                state["samples"][sid]["frames"] = i + 1; state["samples"][sid]["last_frame"] = i
                checkpoint_path.write_text(json.dumps(state, indent=2) + "\n")
                if i != FRAME_COUNT - 1: target.unlink(missing_ok=True)
            raw.unlink(missing_ok=True)
            run([str(vpy), str(args.toolkit / "eval.py"), str(capture), "--config", str(nominal_cfg), "--manual-json", str(marker_json), "--debug-jpeg", str(d / "nominal_debug.jpg"), "--report-json", str(d / "nominal_eval.json")], stdout=(d / "nominal_eval.txt").open("w"), stderr=subprocess.STDOUT)
            run([str(vpy), str(args.toolkit / "eval.py"), str(capture), "--config", str(phone_cfg), "--manual-json", str(marker_json), "--debug-jpeg", str(d / "phone_debug.jpg"), "--report-json", str(d / "phone_eval.json")], stdout=(d / "phone_eval.txt").open("w"), stderr=subprocess.STDOUT)
            row = {"sample_id": sid, "alpha": alpha, "rg_gain": rg, "bg_gain": bg, "ccm": ccm, "nominal": summary_from(d / "nominal_eval.json"), "phone": summary_from(d / "phone_eval.json"), "capture_filename": str(capture), "profile_filename": str(profile), "report_filename": str(d / "nominal_eval.json")}
            rows_by_id[sid] = row; state["rows"] = list(rows_by_id.values()); state["samples"][sid].update({"status": "done", "row": row}); checkpoint_path.write_text(json.dumps(state, indent=2) + "\n"); write_tables(root, list(rows_by_id.values())); dbg(f"completed {sid}")
    finally:
        p = subprocess.run(["sudo", "-S", "-p", "", "install", "-m", "0644", str(active_backup), "/mnt/isp_param_config.bin"], input=(args.sudo_password + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode: print("WARNING: restore failed: " + p.stderr.decode(errors="replace"), file=sys.stderr)
        else: print("Restored active profile: " + sha256(active_backup), flush=True)
    rows = list(rows_by_id.values()); write_tables(root, rows); analyse(root, rows, previous)
    print(f"dataset ready: {root} ({len(rows)}/80 samples)", flush=True)


if __name__ == "__main__":
    main()
