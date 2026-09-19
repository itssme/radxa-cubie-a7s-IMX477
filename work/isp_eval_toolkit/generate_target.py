#!/usr/bin/env python3
"""Generate an A4 ISP RGB calibration target as PNG + exact-size PDF + JSON config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from calibration_common import aruco_dictionary, save_json


PAGE_MM = (210.0, 297.0)
ARUCO_DICT = "DICT_4X4_50"
MARKER_IDS = [10, 20, 30, 40]  # TL, TR, BR, BL
MARKER_SIZE_MM = 28.0
MARKER_POSITIONS_MM = [
    (7.0, 7.0),
    (175.0, 7.0),
    (175.0, 262.0),
    (7.0, 262.0),
]

# 36 patches: neutral ramp, half/full primaries & secondaries, hue/saturation
# samples, and useful natural-ish colors. All values are 8-bit sRGB intent values.
PATCHES = [
    (0, 0, 0), (32, 32, 32), (64, 64, 64), (128, 128, 128), (192, 192, 192), (255, 255, 255),
    (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0), (0, 128, 128), (128, 0, 128),
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255),
    (192, 64, 64), (192, 128, 64), (128, 192, 64), (64, 192, 128), (64, 128, 192), (128, 64, 192),
    (224, 128, 128), (224, 176, 128), (176, 224, 128), (128, 224, 176), (128, 176, 224), (176, 128, 224),
    (214, 160, 125), (160, 105, 75), (92, 64, 51), (72, 110, 55), (92, 148, 200), (230, 175, 60),
]


def mm_to_px(v: float, dpi: int) -> int:
    return int(round(v / 25.4 * dpi))


def draw_centered_text(img, text, center_x, baseline_y, font_scale, thickness=1):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    x = int(round(center_x - tw / 2))
    cv2.putText(img, text, (x, int(round(baseline_y))), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)


def make_target(dpi: int):
    width_px = mm_to_px(PAGE_MM[0], dpi)
    height_px = mm_to_px(PAGE_MM[1], dpi)
    img = np.full((height_px, width_px, 3), 255, np.uint8)
    dictionary = aruco_dictionary(ARUCO_DICT)

    markers_cfg = []
    for marker_id, (x_mm, y_mm) in zip(MARKER_IDS, MARKER_POSITIONS_MM):
        marker_px = mm_to_px(MARKER_SIZE_MM, dpi)
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, marker_px)
        x, y = mm_to_px(x_mm, dpi), mm_to_px(y_mm, dpi)
        img[y:y + marker_px, x:x + marker_px] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
        markers_cfg.append({
            "id": marker_id,
            "rect_mm": [x_mm, y_mm, MARKER_SIZE_MM, MARKER_SIZE_MM],
            "center_mm": [x_mm + MARKER_SIZE_MM / 2, y_mm + MARKER_SIZE_MM / 2],
        })

    # Color grid geometry.
    cols, rows = 6, 6
    patch_w_mm = patch_h_mm = 24.0
    grid_x_mm = 23.0
    grid_y_mm = 46.0
    col_pitch_mm = 28.0
    row_pitch_mm = 38.0
    label_offset_mm = 1.8

    patches_cfg = []
    font_scale = 0.54 * (dpi / 300.0)
    font_thickness = max(1, int(round(dpi / 300.0)))

    for i, rgb in enumerate(PATCHES):
        row, col = divmod(i, cols)
        x_mm = grid_x_mm + col * col_pitch_mm
        y_mm = grid_y_mm + row * row_pitch_mm
        x0, y0 = mm_to_px(x_mm, dpi), mm_to_px(y_mm, dpi)
        x1, y1 = mm_to_px(x_mm + patch_w_mm, dpi), mm_to_px(y_mm + patch_h_mm, dpi)
        r, g, b = rgb
        cv2.rectangle(img, (x0, y0), (x1, y1), (b, g, r), -1)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), max(1, int(round(dpi / 300.0))))
        label = f"P{i:02d} {r:03d},{g:03d},{b:03d}"
        draw_centered_text(
            img,
            label,
            (x0 + x1) / 2,
            mm_to_px(y_mm - label_offset_mm, dpi),
            font_scale,
            font_thickness,
        )
        patches_cfg.append({
            "id": f"P{i:02d}",
            "rgb": [r, g, b],
            "rect_mm": [x_mm, y_mm, patch_w_mm, patch_h_mm],
        })

    # Header/footer deliberately kept small to preserve a clean chart area.
    header = "ISP RGB CALIBRATION TARGET - print at 100% / Actual Size"
    draw_centered_text(img, header, width_px / 2, mm_to_px(40.0, dpi), 0.56 * (dpi / 300.0), font_thickness)
    footer = "Expected values are 8-bit sRGB intent values; printer/paper are not colorimetric references."
    draw_centered_text(img, footer, width_px / 2, mm_to_px(293.0, dpi), 0.36 * (dpi / 300.0), font_thickness)

    cfg = {
        "schema": "isp-color-chart-v1",
        "version": 1,
        "page_mm": list(PAGE_MM),
        "source_color_space": "sRGB 8-bit RGB intent",
        "aruco": {
            "dictionary": ARUCO_DICT,
            "marker_order": MARKER_IDS,
            "marker_order_names": ["top-left", "top-right", "bottom-right", "bottom-left"],
        },
        "markers": markers_cfg,
        "sampling": {
            "method": "per-channel median",
            "inset_fraction": 0.25,
            "description": "Samples the central 50% width/height of each patch (25% inset on every side).",
        },
        "similarity": {
            "formula": "100 * (1 - mean(abs(measured_rgb - expected_rgb)) / 255), clipped to 0..100",
        },
        "patches": patches_cfg,
    }
    return img, cfg


def write_pdf_from_png(png_path: Path, pdf_path: Path):
    # Raster was generated by OpenCV; PDF only fixes the physical page size to exact A4.
    c = canvas.Canvas(str(pdf_path), pagesize=A4, pageCompression=1)
    page_w_pt, page_h_pt = A4
    c.drawImage(ImageReader(str(png_path)), 0, 0, width=page_w_pt, height=page_h_pt, preserveAspectRatio=False, mask=None)
    c.showPage()
    c.save()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dpi", type=int, default=300, help="Raster DPI used inside the PDF/PNG (default: 300)")
    ap.add_argument("--output-dir", default=".", help="Output directory")
    ap.add_argument("--prefix", default="isp_color_target", help="Output file prefix")
    args = ap.parse_args()

    if args.dpi < 150:
        ap.error("Use at least 150 DPI; 300 DPI is recommended for reliable ArUco printing.")

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    png_path = outdir / f"{args.prefix}.png"
    pdf_path = outdir / f"{args.prefix}.pdf"
    cfg_path = outdir / f"{args.prefix}_config.json"

    img, cfg = make_target(args.dpi)
    if not cv2.imwrite(str(png_path), img):
        raise RuntimeError(f"Failed to write {png_path}")
    write_pdf_from_png(png_path, pdf_path)
    save_json(cfg_path, cfg)

    print(f"Wrote {png_path}")
    print(f"Wrote {pdf_path} (exact A4 page size)")
    print(f"Wrote {cfg_path}")
    print("Print the PDF at 100% / Actual Size. Disable Fit-to-page / Shrink-to-fit.")


if __name__ == "__main__":
    main()
