#!/usr/bin/env python3
"""Run a resumable IMX477 ISP sweep and evaluate the printed colour chart.

This script runs *on the Radxa*.  Each candidate gets its own directory under
``samples/`` containing the exact ISP settings, profile binary, captured JPEG,
evaluation JSON, and compressed debug JPEG.  ``checkpoint.json`` is updated
after every frame so an interrupted run can continue without repeating work.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from tqdm import tqdm
except ImportError as exc:  # pragma: no cover - exercised on an unconfigured host
    raise SystemExit("Install dependencies first with: uv sync") from exc


FRAME_COUNT = 10
WIDTH = 3840
HEIGHT = 2160
RAW_FRAME_BYTES = WIDTH * HEIGHT * 3 // 2
RAW_BYTES = RAW_FRAME_BYTES * FRAME_COUNT


@dataclass(frozen=True)
class Candidate:
    blend: float
    awb_mode: str
    awb_temperature: int | None
    rg_gain: float = 1.0
    bg_gain: float = 1.0
    ccm_saturation: float = 1.0
    ccm_hue_degrees: float = 0.0

    @property
    def key(self) -> str:
        temp = "auto" if self.awb_temperature is None else str(self.awb_temperature)
        return (f"blend={self.blend:.4f};awb={self.awb_mode};temp={temp};"
                f"rg={self.rg_gain:.4f};bg={self.bg_gain:.4f};"
                f"sat={self.ccm_saturation:.4f};hue={self.ccm_hue_degrees:.2f}")

    @property
    def tag(self) -> str:
        temp = "auto" if self.awb_temperature is None else str(self.awb_temperature)
        return (f"b{self.blend:.3f}-rg{self.rg_gain:.3f}-bg{self.bg_gain:.3f}-"
                f"s{self.ccm_saturation:.3f}-h{self.ccm_hue_degrees:+.1f}-"
                f"{self.awb_mode}-{temp}").replace(".", "p").replace("+", "")


def debug(message: str) -> None:
    print(f"[debug] {message}", flush=True)


def run(command: list[str], *, check: bool = True,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    debug("$ " + shlex.join(command))
    if stdout_path is None and stderr_path is None:
        result = subprocess.run(command, text=True, capture_output=True)
        if result.stdout.strip():
            print(result.stdout.rstrip(), flush=True)
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr, flush=True)
    else:
        stdout_stream = stdout_path.open("w") if stdout_path else subprocess.PIPE
        stderr_stream = stderr_path.open("w") if stderr_path else subprocess.PIPE
        try:
            result = subprocess.run(command, text=True,
                                    stdout=stdout_stream, stderr=stderr_stream)
        finally:
            if stdout_path:
                stdout_stream.close()
            if stderr_path:
                stderr_stream.close()
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result


def floats(start: float, stop: float, step: float) -> list[float]:
    if step <= 0 or stop < start:
        raise ValueError("invalid blend range")
    values: list[float] = []
    value = start
    while value <= stop + 1e-9:
        values.append(round(value, 6))
        value += step
    return values


class Sweep:
    def __init__(self, args: argparse.Namespace) -> None:
        self.toolkit = Path(__file__).resolve().parent
        self.project = self.toolkit.parent.parent
        self.work = self.project / "work"
        self.builder = self.work / "build-imx477-isp-bin.py"
        self.payload = self.work / "ov13850-fallback-active.cfg"
        self.config = self.resolve_toolkit_path(args.config)
        self.markers = self.resolve_toolkit_path(args.markers)
        self.output = Path(args.output).expanduser().resolve()
        self.samples = self.output / "samples"
        self.output.mkdir(parents=True, exist_ok=True)
        self.samples.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.output / "checkpoint.json"
        self.summary_path = self.output / "summary.tsv"
        # Preserve a completed v1 sweep rather than appending v2 rows under a
        # stale header.  This makes it safe to point the expanded runner at
        # the previous dataset while keeping both result tables readable.
        if self.summary_path.exists():
            first_line = self.summary_path.read_text().splitlines()[:1]
            if first_line and "rg_gain" not in first_line[0]:
                self.summary_path = self.output / "summary-expanded.tsv"
                debug(f"legacy summary detected; writing {self.summary_path.name}")
        self.sudo_password = args.sudo_password
        self.video_device = args.video_device
        self.state: dict[str, Any] = self.load_checkpoint()

        if not self.builder.exists():
            raise SystemExit(f"ISP profile builder not found: {self.builder}")
        if not self.payload.exists():
            raise SystemExit(f"ISP payload not found: {self.payload}")
        if not self.config.exists():
            raise SystemExit(f"Chart config not found: {self.config}")
        if not self.markers.exists():
            raise SystemExit(f"Marker override not found: {self.markers}")

    def resolve_toolkit_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() and not path.exists():
            path = self.toolkit / path
        return path.resolve()

    def load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {"version": 1, "created": time.time(), "candidates": {}}
        state = json.loads(self.checkpoint_path.read_text())
        if state.get("version") != 1:
            raise SystemExit(f"unsupported checkpoint version: {state.get('version')}")
        debug(f"loaded checkpoint with {len(state.get('candidates', {}))} candidates")
        return state

    def save_checkpoint(self) -> None:
        temporary = self.checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.checkpoint_path)

    def state_for(self, candidate: Candidate, index: int) -> dict[str, Any]:
        candidates = self.state.setdefault("candidates", {})
        return candidates.setdefault(candidate.key, {
            "index": index,
            "parameters": asdict(candidate),
            "status": "pending",
            "processed_frames": [],
        })

    def sample_dir(self, index: int, candidate: Candidate) -> Path:
        return self.samples / f"{index:04d}-{candidate.tag}"

    def write_settings(self, sample: Path, candidate: Candidate,
                       state: dict[str, Any], profile: Path) -> None:
        settings = {
            "schema": "imx477-isp-sweep-sample-v1",
            "candidate": asdict(candidate),
            "candidate_key": candidate.key,
            "capture": {
                "device": self.video_device,
                "width": WIDTH,
                "height": HEIGHT,
                "framerate": "30/1",
                "format": "NV12",
                "frames": FRAME_COUNT,
                "d3d_preload": "/usr/local/lib/libisp_no3dn.so",
            },
            "chart": {
                "config": str(self.config),
                "markers": str(self.markers),
            },
            "profile": {
                "path": str(profile),
                "sha256": state.get("profile_sha256"),
            },
            "created": state.get("started", time.time()),
        }
        (sample / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    def install_profile_and_set_awb(self, profile: Path, candidate: Candidate) -> None:
        install_command = ["install", "-m", "0644", str(profile), "/mnt/isp_param_config.bin"]
        debug(f"installing {profile.name} as /mnt/isp_param_config.bin")
        password = self.sudo_password.encode() + b"\n"
        result = subprocess.run(
            ["sudo", "-S", "-p", ""] + install_command,
            input=password, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, result.args,
                                                result.stdout, result.stderr)
        if candidate.awb_mode == "auto":
            controls = "white_balance_automatic=1"
        else:
            controls = (
                "white_balance_automatic=0,"
                f"white_balance_temperature={candidate.awb_temperature}"
            )
        control_command = [
            "v4l2-ctl", "-d", self.video_device, "--set-ctrl", controls,
        ]
        result = run(control_command, check=False)
        if result.returncode:
            debug("direct V4L2 control failed; retrying with sudo")
            password = self.sudo_password.encode() + b"\n"
            result = subprocess.run(
                ["sudo", "-S", "-p", ""] + control_command,
                input=password, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, result.args,
                                                    result.stdout, result.stderr)

    def build_profile(self, candidate: Candidate, sample: Path) -> Path:
        profile = sample / "isp_profile.bin"
        if not profile.exists():
            run([
                sys.executable, str(self.builder), str(self.payload), str(profile),
                "--blend-imx477", f"{candidate.blend:.6f}",
                "--rg-gain", f"{candidate.rg_gain:.6f}",
                "--bg-gain", f"{candidate.bg_gain:.6f}",
                "--ccm-saturation", f"{candidate.ccm_saturation:.6f}",
                "--ccm-hue-degrees", f"{candidate.ccm_hue_degrees:.6f}",
            ])
        return profile

    def capture_raw(self, candidate: Candidate, sample: Path,
                    state: dict[str, Any], profile: Path) -> Path:
        raw = sample / "capture.nv12"
        if raw.exists() and raw.stat().st_size == RAW_BYTES:
            debug(f"reusing complete raw capture: {raw}")
            return raw
        if raw.exists():
            debug(f"discarding incomplete raw capture ({raw.stat().st_size} bytes)")
            raw.unlink()

        self.install_profile_and_set_awb(profile, candidate)
        command = [
            "gst-launch-1.0", "-e", "v4l2src", f"device={self.video_device}",
            "io-mode=2", f"num-buffers={FRAME_COUNT}", "!",
            f"video/x-raw,format=NV12,width={WIDTH},height={HEIGHT},framerate=30/1",
            "!", "filesink", f"location={raw}",
        ]
        env = os.environ.copy()
        env["LD_PRELOAD"] = "/usr/local/lib/libisp_no3dn.so"
        debug(f"capturing {FRAME_COUNT} frames to {raw}")
        state["status"] = "capturing"
        self.save_checkpoint()
        log_path = sample / "capture.log"
        with log_path.open("w") as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command)
        if not raw.exists() or raw.stat().st_size != RAW_BYTES:
            size = raw.stat().st_size if raw.exists() else 0
            raise RuntimeError(f"unexpected raw size {size}, expected {RAW_BYTES}")
        state["status"] = "raw_captured"
        state["raw"] = str(raw)
        self.save_checkpoint()
        return raw

    def process_frames(self, candidate: Candidate, sample: Path,
                       raw: Path, state: dict[str, Any]) -> Path:
        image = sample / "capture.jpg"
        processed = set(state.setdefault("processed_frames", []))
        if FRAME_COUNT - 1 in processed and not image.exists():
            processed.remove(FRAME_COUNT - 1)
        for frame_index in range(FRAME_COUNT):
            if frame_index in processed:
                continue
            temporary = sample / f".frame-{frame_index:02d}.jpg"
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pixel_format", "nv12",
                "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", "30",
                "-i", str(raw), "-vf", f"select=eq(n\\,{frame_index})",
                "-frames:v", "1", "-q:v", "2", str(temporary),
            ])
            if frame_index == FRAME_COUNT - 1:
                temporary.replace(image)
            else:
                temporary.unlink(missing_ok=True)
            processed.add(frame_index)
            state["processed_frames"] = sorted(processed)
            state["last_frame"] = frame_index
            state["status"] = "processing"
            self.save_checkpoint()
            debug(f"checkpointed {candidate.key}, frame {frame_index + 1}/{FRAME_COUNT}")
        return image

    def evaluate(self, sample: Path, image: Path) -> dict[str, Any]:
        report = sample / "eval.json"
        debug_jpeg = sample / "debug.jpg"
        stdout = sample / "eval.txt"
        command = [
            sys.executable, str(self.toolkit / "eval.py"), str(image),
            "--config", str(self.config), "--manual-json", str(self.markers),
            "--debug-jpeg", str(debug_jpeg), "--report-json", str(report),
        ]
        run(command, stdout_path=stdout, stderr_path=sample / "eval-errors.txt")
        result = json.loads(report.read_text())
        return result

    def append_summary(self, index: int, candidate: Candidate,
                       sample: Path, report: dict[str, Any]) -> None:
        if not self.summary_path.exists():
            self.summary_path.write_text(
                "index\tcandidate\tblend\tawb_mode\tawb_temperature\t"
                "rg_gain\tbg_gain\tccm_saturation\tccm_hue_degrees\t"
                "mean_similarity_percent\tmedian_similarity_percent\t"
                "mean_patch_mae\tmean_patch_rmse\tworst_patch\tsample\n"
            )
        summary = report.get("summary", {})
        worst = summary.get("worst_patch", {})
        lines = self.summary_path.read_text().splitlines()
        if any(len(line.split("\t", 2)) > 1 and line.split("\t", 2)[1] == candidate.key
               for line in lines[1:]):
            return
        temperature = "" if candidate.awb_temperature is None else str(candidate.awb_temperature)
        row = [
            f"{index:04d}", candidate.key, f"{candidate.blend:.6f}",
            candidate.awb_mode, temperature,
            f"{candidate.rg_gain:.6f}", f"{candidate.bg_gain:.6f}",
            f"{candidate.ccm_saturation:.6f}", f"{candidate.ccm_hue_degrees:.4f}",
            f"{summary.get('mean_similarity_percent', float('nan')):.4f}",
            f"{summary.get('median_similarity_percent', float('nan')):.4f}",
            f"{summary.get('mean_patch_mae', float('nan')):.4f}",
            f"{summary.get('mean_patch_rmse', float('nan')):.4f}",
            f"{worst.get('id', '')}:{worst.get('similarity_percent', '')}",
            str(sample),
        ]
        with self.summary_path.open("a") as stream:
            stream.write("\t".join(row) + "\n")

    def run_candidate(self, index: int, candidate: Candidate) -> None:
        state = self.state_for(candidate, index)
        sample = self.sample_dir(index, candidate)
        sample.mkdir(parents=True, exist_ok=True)
        if state.get("status") == "done" and (sample / "eval.json").exists():
            debug(f"skip completed candidate: {candidate.key}")
            return
        debug(f"starting candidate {index}: {candidate.key}")
        state["status"] = "pending"
        state["started"] = state.get("started", time.time())
        state["sample"] = str(sample)
        self.save_checkpoint()
        profile = self.build_profile(candidate, sample)
        state["profile"] = str(profile)
        state["profile_sha256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
        self.write_settings(sample, candidate, state, profile)
        raw = self.capture_raw(candidate, sample, state, profile)
        image = self.process_frames(candidate, sample, raw, state)
        report = self.evaluate(sample, image)
        self.append_summary(index, candidate, sample, report)
        raw.unlink(missing_ok=True)
        state["status"] = "done"
        state["image"] = str(image)
        state["report"] = str(sample / "eval.json")
        state["debug_image"] = str(sample / "debug.jpg")
        state["completed"] = time.time()
        self.save_checkpoint()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="dataset directory; samples are created below it")
    parser.add_argument("--config", default="isp_color_target_config.json")
    parser.add_argument("--markers", default="markers.json")
    parser.add_argument("--video-device", default="/dev/video1")
    parser.add_argument("--sudo-password", default=os.environ.get("IMX477_SUDO_PASSWORD", "radxa"))
    parser.add_argument("--blend-start", type=float, default=0.0)
    parser.add_argument("--blend-stop", type=float, default=1.0)
    parser.add_argument("--blend-step", type=float, default=0.25)
    parser.add_argument("--awb-temperatures", type=int, nargs="+",
                        default=[3200, 4000, 4800, 5600, 6400, 7200, 8000])
    parser.add_argument("--no-auto-awb", action="store_true")
    parser.add_argument("--rg-gains", type=float, nargs="+", default=[1.0],
                        help="red/green Bayer gain ratios to test")
    parser.add_argument("--bg-gains", type=float, nargs="+", default=[1.0],
                        help="blue/green Bayer gain ratios to test")
    parser.add_argument("--ccm-saturations", type=float, nargs="+", default=[1.0],
                        help="CCM saturation multipliers to test")
    parser.add_argument("--ccm-hue-degrees", type=float, nargs="+", default=[0.0],
                        help="CCM hue rotations, in degrees, to test")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    blends = floats(args.blend_start, args.blend_stop, args.blend_step)
    awb_modes: list[tuple[str, int | None]] = []
    if not args.no_auto_awb:
        awb_modes.append(("auto", None))
    awb_modes.extend(("manual", temp) for temp in args.awb_temperatures)
    candidates = [
        Candidate(blend=blend, awb_mode=mode, awb_temperature=temp,
                  rg_gain=rg_gain, bg_gain=bg_gain,
                  ccm_saturation=ccm_saturation,
                  ccm_hue_degrees=ccm_hue_degrees)
        for blend, (mode, temp), rg_gain, bg_gain, ccm_saturation,
        ccm_hue_degrees in itertools.product(
            blends, awb_modes, args.rg_gains, args.bg_gains,
            args.ccm_saturations, args.ccm_hue_degrees)
    ]
    print(f"candidate count: {len(candidates)}", flush=True)
    print("CFA: fixed SRGGB10 (driver-level; not a profile field)", flush=True)
    print("gamma/contrast: not swept; CCM saturation/hue and Bayer gains are swept", flush=True)
    if len(candidates) > 1000:
        print("[warning] this Cartesian grid exceeds 1000 captures; use staged sweeps", flush=True)
    if args.dry_run:
        for index, candidate in enumerate(candidates):
            print(f"{index:04d}\t{candidate.key}")
        return

    sweep = Sweep(args)
    progress = tqdm(enumerate(candidates), total=len(candidates),
                    desc="ISP chart sweep", unit="sample")
    for index, candidate in progress:
        progress.set_postfix_str(candidate.key, refresh=False)
        try:
            sweep.run_candidate(index, candidate)
        except Exception as exc:
            state = sweep.state_for(candidate, index)
            state["status"] = "error"
            state["error"] = repr(exc)
            sweep.save_checkpoint()
            print(f"[error] {candidate.key}: {exc}", file=sys.stderr, flush=True)
            if args.fail_fast:
                raise
    print(f"dataset ready: {sweep.output}", flush=True)


if __name__ == "__main__":
    main()
