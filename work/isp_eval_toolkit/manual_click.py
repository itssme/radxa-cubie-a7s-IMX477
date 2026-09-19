#!/usr/bin/env python3
"""Interactively click the four marker centers and save a marker override JSON.

The click order is always the four corners as they appear in the IMAGE:
top-left -> top-right -> bottom-right -> bottom-left.  This works for portrait
and landscape captures.  If the printed chart is rotated, use T/G to rotate the
chart mapping until the expected-color grid lines up with the photographed
chart, then save.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from calibration_common import (
    bgr,
    draw_text_box,
    fit_image,
    homography_from_centers,
    load_config,
    marker_json_payload,
    project_points_mm,
    rect_corners_mm,
    save_json,
)


WINDOW = "Manual marker override"
IMAGE_CORNERS = ["image top-left", "image top-right", "image bottom-right", "image bottom-left"]


def auto_rotation_deg(image: np.ndarray, cfg: dict) -> int:
    """Choose a useful initial 0/90-degree orientation from aspect ratios."""
    image_landscape = image.shape[1] >= image.shape[0]
    page_w, page_h = map(float, cfg["page_mm"])
    chart_landscape = page_w >= page_h
    return 0 if image_landscape == chart_landscape else 90


def ids_for_image_corners(marker_ids: list[int], rotation_deg: int) -> list[int]:
    """Map image TL/TR/BR/BL clicks to chart marker IDs.

    ``rotation_deg`` is clockwise chart rotation in image coordinates.
    """
    k = (int(rotation_deg) // 90) % 4
    return [marker_ids[(j - k) % 4] for j in range(4)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", help="Image to annotate")
    ap.add_argument("--config", required=True, help="Target config JSON")
    ap.add_argument("--output", default="manual_markers.json", help="Output JSON")
    ap.add_argument(
        "--rotation",
        default="auto",
        choices=["auto", "0", "90", "180", "270"],
        help="Initial clockwise chart rotation in the image. Default: auto from image/page orientation.",
    )
    args = ap.parse_args()

    image_path = Path(args.image)
    original = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if original is None:
        ap.error(f"Could not read image: {image_path}")
    cfg = load_config(args.config)
    marker_ids = [int(x) for x in cfg["aruco"]["marker_order"]]

    if args.rotation == "auto":
        rotation_deg = auto_rotation_deg(original, cfg)
    else:
        rotation_deg = int(args.rotation)

    # Landscape-friendly display bound; portrait images are still fitted correctly.
    display, scale = fit_image(original, 1700, 1000)
    clicks: list[np.ndarray] = []  # image-corner order, in ORIGINAL image pixels

    def current_image_corner_ids() -> list[int]:
        return ids_for_image_corners(marker_ids, rotation_deg)

    def centers_by_id() -> dict[int, np.ndarray]:
        ids = current_image_corner_ids()
        return {ids[i]: clicks[i] for i in range(4)}

    def redraw():
        canvas = display.copy()
        image_corner_ids = current_image_corner_ids()

        for i, p_orig in enumerate(clicks):
            p = np.round(p_orig * scale).astype(int)
            cv2.circle(canvas, tuple(p), 8, (0, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(canvas, tuple(p), 11, (0, 0, 0), 2, cv2.LINE_AA)
            label = f"{i+1}: {IMAGE_CORNERS[i]} -> ID {image_corner_ids[i]}"
            draw_text_box(canvas, label, (p[0] + 14, max(24, p[1] - 10)), font_scale=0.62, thickness=1)

        if len(clicks) == 4:
            centers = centers_by_id()
            H = homography_from_centers(cfg, centers)
            # Display inferred patch grid and expected-color swatches.  Cycling
            # rotation changes which IDs the four clicks represent, making a
            # wrong CW/CCW landscape orientation immediately visible.
            for patch in cfg["patches"]:
                poly = project_points_mm(H, rect_corners_mm(patch["rect_mm"])) * scale
                poly_i = np.round(poly).astype(np.int32)
                cv2.polylines(canvas, [poly_i], True, (255, 255, 255), 3, cv2.LINE_AA)
                cv2.polylines(canvas, [poly_i], True, (0, 0, 0), 1, cv2.LINE_AA)
                c = np.round(poly.mean(axis=0)).astype(int)
                projected_w = float(np.linalg.norm(poly[1] - poly[0]))
                sw = int(np.clip(round(projected_w * 0.13), 8, 22))
                cv2.rectangle(canvas, (c[0] - sw, c[1] - sw), (c[0] + sw, c[1] + sw), bgr(patch["rgb"]), -1)
                cv2.rectangle(canvas, (c[0] - sw, c[1] - sw), (c[0] + sw, c[1] + sw), (255, 255, 255), 1)

        if len(clicks) < 4:
            instruction = f"Click marker CENTER at {IMAGE_CORNERS[len(clicks)]}"
        else:
            instruction = "Check grid/colors. T=rotate CW 90deg, G=rotate CCW 90deg, S/Enter=save, U=undo, R=restart, Q=quit"

        mapping = ", ".join(f"{IMAGE_CORNERS[i].replace('image ', '')}=ID {mid}" for i, mid in enumerate(image_corner_ids))
        draw_text_box(canvas, instruction, (8, 8), font_scale=0.64, thickness=1, anchor="tl")
        draw_text_box(canvas, f"Chart rotation: {rotation_deg} deg CW | {mapping}", (8, 48), font_scale=0.54, thickness=1, anchor="tl")
        cv2.imshow(WINDOW, canvas)

    def on_mouse(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 4:
            clicks.append(np.array([x / scale, y / scale], dtype=np.float64))
            redraw()

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW, on_mouse)
    redraw()

    saved = False
    while True:
        key = cv2.waitKey(0) & 0xFF
        if key in (ord("q"), 27):
            break
        if key in (ord("u"), 8, 127) and clicks:
            clicks.pop()
            redraw()
        elif key == ord("r"):
            clicks.clear()
            redraw()
        elif key == ord("t"):
            rotation_deg = (rotation_deg + 90) % 360
            redraw()
        elif key == ord("g"):
            rotation_deg = (rotation_deg - 90) % 360
            redraw()
        elif key in (ord("s"), 13, 10) and len(clicks) == 4:
            centers = centers_by_id()
            marker_details = {
                mid: {"rotation_deg": float(rotation_deg)}
                for mid in marker_ids
            }
            data = marker_json_payload(
                cfg,
                source_image=str(image_path),
                centers_px=centers,
                marker_details=marker_details,
                chart_rotation_deg=float(rotation_deg),
                source="manual_click",
            )
            data["click_order"] = IMAGE_CORNERS
            data["image_corner_marker_ids"] = current_image_corner_ids()
            save_json(args.output, data)
            print(f"Saved {args.output}")
            print(f"Chart rotation: {rotation_deg} deg clockwise")
            saved = True
            break

    cv2.destroyAllWindows()
    if not saved:
        print("No override file saved.")


if __name__ == "__main__":
    main()
