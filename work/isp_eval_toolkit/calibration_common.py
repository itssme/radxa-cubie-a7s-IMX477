#!/usr/bin/env python3
"""Shared helpers for the ISP color calibration target tools."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import cv2
import numpy as np


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("schema") != "isp-color-chart-v1":
        raise ValueError(f"Unsupported config schema: {cfg.get('schema')!r}")
    return cfg


def save_json(path: str | Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def aruco_dictionary(name: str):
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("This OpenCV build has no cv2.aruco module. Install opencv-contrib-python.")
    if not hasattr(cv2.aruco, name):
        raise ValueError(f"Unknown ArUco dictionary {name!r}")
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


def normalize_angle_deg(angle: float) -> float:
    """Normalize an angle to [-180, 180)."""
    return float((float(angle) + 180.0) % 360.0 - 180.0)


def circular_mean_deg(angles: Iterable[float]) -> float:
    angles = [float(a) for a in angles]
    if not angles:
        return 0.0
    radians = np.deg2rad(angles)
    x = float(np.mean(np.cos(radians)))
    y = float(np.mean(np.sin(radians)))
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return 0.0
    return normalize_angle_deg(np.rad2deg(np.arctan2(y, x)))


def detect_markers(image_bgr: np.ndarray, cfg: dict) -> Tuple[Dict[int, dict], list]:
    """Detect ArUco markers and preserve center, corners, and marker rotation.

    OpenCV returns the four corners in marker-canonical order.  The angle from
    corner 0 to corner 1 therefore gives the in-image rotation of the printed
    marker/chart (0=up, +90=clockwise in image coordinates).
    """
    dictionary = aruco_dictionary(cfg["aruco"]["dictionary"])
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, params)
    corners, ids, rejected = detector.detectMarkers(image_bgr)
    found: Dict[int, dict] = {}
    if ids is not None:
        for marker_corners, marker_id in zip(corners, ids.flatten()):
            pts = marker_corners.reshape(4, 2).astype(np.float64)
            edge = pts[1] - pts[0]
            rotation_deg = normalize_angle_deg(np.degrees(np.arctan2(edge[1], edge[0])))
            found[int(marker_id)] = {
                "center_px": pts.mean(axis=0),
                "corners_px": pts,
                "rotation_deg": rotation_deg,
            }
    return found, rejected


def detect_marker_centers(image_bgr: np.ndarray, cfg: dict) -> Tuple[Dict[int, np.ndarray], list]:
    """Compatibility wrapper returning only marker centers."""
    markers, rejected = detect_markers(image_bgr, cfg)
    return {mid: data["center_px"] for mid, data in markers.items()}, rejected


def expected_marker_centers_mm(cfg: dict) -> Dict[int, np.ndarray]:
    return {
        int(m["id"]): np.array(m["center_mm"], dtype=np.float64)
        for m in cfg["markers"]
    }


def homography_from_centers(cfg: dict, centers_px: Dict[int, Iterable[float]]) -> np.ndarray:
    expected = expected_marker_centers_mm(cfg)
    order = [int(x) for x in cfg["aruco"]["marker_order"]]
    missing = [mid for mid in order if mid not in centers_px]
    if missing:
        raise ValueError(f"Missing marker centers for IDs: {missing}")
    src = np.array([expected[mid] for mid in order], dtype=np.float64)
    dst = np.array([centers_px[mid] for mid in order], dtype=np.float64)
    H = cv2.getPerspectiveTransform(src.astype(np.float32), dst.astype(np.float32))
    if not np.all(np.isfinite(H)):
        raise ValueError("Could not compute a valid homography")
    return H


def project_points_mm(H_mm_to_px: np.ndarray, points_mm: np.ndarray) -> np.ndarray:
    pts = np.asarray(points_mm, dtype=np.float32).reshape(1, -1, 2)
    return cv2.perspectiveTransform(pts, H_mm_to_px).reshape(-1, 2)


def rect_corners_mm(rect_mm: Iterable[float], inset_fraction: float = 0.0) -> np.ndarray:
    x, y, w, h = map(float, rect_mm)
    inset_fraction = float(inset_fraction)
    if not 0.0 <= inset_fraction < 0.5:
        raise ValueError("inset_fraction must be in [0, 0.5)")
    dx, dy = w * inset_fraction, h * inset_fraction
    return np.array([
        [x + dx, y + dy],
        [x + w - dx, y + dy],
        [x + w - dx, y + h - dy],
        [x + dx, y + h - dy],
    ], dtype=np.float32)


def sample_polygon_rgb(image_bgr: np.ndarray, polygon_px: np.ndarray) -> dict:
    h, w = image_bgr.shape[:2]
    poly = np.round(polygon_px).astype(np.int32)
    x, y, bw, bh = cv2.boundingRect(poly)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + bw), min(h, y + bh)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Sample polygon is outside the image")

    roi = image_bgr[y0:y1, x0:x1]
    shifted = poly - np.array([x0, y0], dtype=np.int32)
    mask = np.zeros(roi.shape[:2], dtype=np.uint8)
    cv2.fillConvexPoly(mask, shifted, 255)
    pixels_bgr = roi[mask > 0]
    if len(pixels_bgr) < 16:
        raise ValueError(f"Sample region has too few pixels ({len(pixels_bgr)})")

    pixels_rgb = pixels_bgr[:, ::-1].astype(np.float64)
    median = np.median(pixels_rgb, axis=0)
    mean = np.mean(pixels_rgb, axis=0)
    std = np.std(pixels_rgb, axis=0)
    return {
        "median_rgb": median,
        "mean_rgb": mean,
        "std_rgb": std,
        "n_pixels": int(len(pixels_rgb)),
    }


def similarity_from_rgb(expected_rgb: np.ndarray, measured_rgb: np.ndarray) -> Tuple[float, np.ndarray, float, float]:
    """Return similarity %, signed delta, MAE, and RMSE.

    Similarity is defined as 100 * (1 - mean absolute channel error / 255),
    clipped to [0, 100]. This is deliberately simple and directly interpretable
    in 8-bit RGB units.
    """
    expected = np.asarray(expected_rgb, dtype=np.float64)
    measured = np.asarray(measured_rgb, dtype=np.float64)
    delta = measured - expected
    mae = float(np.mean(np.abs(delta)))
    rmse = float(np.sqrt(np.mean(delta * delta)))
    similarity = float(np.clip(100.0 * (1.0 - mae / 255.0), 0.0, 100.0))
    return similarity, delta, mae, rmse


def bgr(rgb: Iterable[int]) -> Tuple[int, int, int]:
    r, g, b_ = [int(round(float(x))) for x in rgb]
    return b_, g, r


def fit_image(image: np.ndarray, max_w: int, max_h: int) -> Tuple[np.ndarray, float]:
    h, w = image.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale >= 0.9999:
        return image.copy(), 1.0
    out = cv2.resize(image, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    return out, scale


def load_marker_override(path: str | Path, cfg: dict) -> tuple[Dict[int, np.ndarray], dict]:
    """Load a manual or auto-saved marker JSON.

    v1 files contain ``marker_centers_px`` only.  v2 files may additionally
    contain per-marker corners/rotation and a chart rotation.  The evaluator
    only needs centers for the homography, so both formats remain compatible.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    centers = data.get("marker_centers_px")
    if not isinstance(centers, dict):
        # Also accept centers nested inside the v2 marker records.
        markers = data.get("markers")
        if isinstance(markers, dict):
            centers = {
                str(mid): marker["center_px"]
                for mid, marker in markers.items()
                if isinstance(marker, dict) and "center_px" in marker
            }
    if not isinstance(centers, dict):
        raise ValueError("Marker JSON has no 'marker_centers_px' object")
    result = {int(k): np.array(v, dtype=np.float64) for k, v in centers.items()}
    required = [int(x) for x in cfg["aruco"]["marker_order"]]
    missing = [mid for mid in required if mid not in result]
    if missing:
        raise ValueError(f"Marker JSON is missing marker IDs: {missing}")
    return result, data


def load_manual_centers(path: str | Path, cfg: dict) -> Dict[int, np.ndarray]:
    centers, _ = load_marker_override(path, cfg)
    return centers


def marker_json_payload(
    cfg: dict,
    source_image: str,
    centers_px: Dict[int, Iterable[float]],
    marker_details: Dict[int, dict] | None = None,
    chart_rotation_deg: float | None = None,
    source: str = "manual",
) -> dict:
    """Build a marker override JSON readable by ``eval.py --manual-json``."""
    order = [int(x) for x in cfg["aruco"]["marker_order"]]
    centers_obj = {
        str(mid): [float(centers_px[mid][0]), float(centers_px[mid][1])]
        for mid in order
    }

    markers_obj = {}
    rotations = []
    marker_details = marker_details or {}
    for mid in order:
        details = marker_details.get(mid, {})
        center = np.asarray(centers_px[mid], dtype=np.float64)
        item = {"center_px": [float(center[0]), float(center[1])]}
        if "corners_px" in details and details["corners_px"] is not None:
            corners = np.asarray(details["corners_px"], dtype=np.float64).reshape(4, 2)
            item["corners_px"] = [[float(x), float(y)] for x, y in corners]
        if "rotation_deg" in details and details["rotation_deg"] is not None:
            rot = normalize_angle_deg(float(details["rotation_deg"]))
            item["rotation_deg"] = rot
            rotations.append(rot)
        markers_obj[str(mid)] = item

    if chart_rotation_deg is None and rotations:
        chart_rotation_deg = circular_mean_deg(rotations)

    payload = {
        "schema": "isp-color-chart-marker-override-v2",
        "source_image": str(source_image),
        "source": str(source),
        "marker_order": order,
        "marker_centers_px": centers_obj,
        "markers": markers_obj,
    }
    if chart_rotation_deg is not None:
        payload["chart_rotation_deg"] = normalize_angle_deg(float(chart_rotation_deg))
    return payload




def draw_text_box(
    image: np.ndarray,
    text: str,
    org: tuple[int, int],
    font_scale: float = 0.55,
    text_color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    thickness: int = 1,
    padding: int = 4,
    anchor: str = "bl",
) -> None:
    """Draw white text on a black rectangle for improved readability.

    org is interpreted relative to the text bounding box according to anchor:
      - "bl": org is the text baseline-left point
      - "tl": org is the top-left point of the background box
    """
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    x, y = int(org[0]), int(org[1])
    if anchor == "bl":
        box_x0 = x - padding
        box_y0 = y - th - padding
        box_x1 = x + tw + padding
        box_y1 = y + baseline + padding
        text_org = (x, y)
    elif anchor == "tl":
        box_x0 = x
        box_y0 = y
        box_x1 = x + tw + 2 * padding
        box_y1 = y + th + baseline + 2 * padding
        text_org = (x + padding, y + th + padding)
    else:
        raise ValueError(f"Unsupported anchor: {anchor}")

    box_x0 = max(0, box_x0)
    box_y0 = max(0, box_y0)
    box_x1 = min(image.shape[1] - 1, box_x1)
    box_y1 = min(image.shape[0] - 1, box_y1)
    cv2.rectangle(image, (box_x0, box_y0), (box_x1, box_y1), bg_color, -1)
    cv2.putText(image, text, text_org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_color, thickness, cv2.LINE_AA)

def draw_projected_grid(image_bgr: np.ndarray, cfg: dict, H: np.ndarray, results: List[dict] | None = None) -> np.ndarray:
    out = image_bgr.copy()
    result_by_id = {r["id"]: r for r in (results or [])}

    # Marker center crosshairs.
    marker_centers = expected_marker_centers_mm(cfg)
    for mid, pt_mm in marker_centers.items():
        p = project_points_mm(H, np.array([pt_mm], dtype=np.float32))[0]
        x, y = map(int, np.round(p))
        cv2.drawMarker(out, (x, y), (255, 255, 255), cv2.MARKER_CROSS, 22, 4, cv2.LINE_AA)
        cv2.drawMarker(out, (x, y), (0, 0, 0), cv2.MARKER_CROSS, 22, 1, cv2.LINE_AA)
        draw_text_box(out, f"ID {mid}", (x + 10, max(18, y - 10)), font_scale=0.72, thickness=2)

    for patch in cfg["patches"]:
        full = project_points_mm(H, rect_corners_mm(patch["rect_mm"], 0.0))
        sample = project_points_mm(H, rect_corners_mm(patch["rect_mm"], cfg["sampling"]["inset_fraction"]))
        full_i = np.round(full).astype(np.int32)
        sample_i = np.round(sample).astype(np.int32)

        cv2.polylines(out, [full_i], True, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.polylines(out, [full_i], True, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.polylines(out, [sample_i], True, (255, 255, 0), 2, cv2.LINE_AA)

        r = result_by_id.get(patch["id"])
        if r is not None:
            center = full.mean(axis=0)
            cx, cy = map(int, np.round(center))
            # Expected-color swatch next to patch center, sized relative to projected patch.
            patch_w = max(12.0, np.linalg.norm(full[1] - full[0]))
            sw = int(np.clip(round(patch_w * 0.19), 10, 36))
            sx, sy = cx + sw, cy - sw // 2
            cv2.rectangle(out, (sx, sy), (sx + sw, sy + sw), bgr(patch["rgb"]), -1)
            cv2.rectangle(out, (sx, sy), (sx + sw, sy + sw), (255, 255, 255), 2)
            label = f"{patch['id']} {r['similarity_percent']:.1f}%"
            org = (sx + sw + 8, sy + sw)
            draw_text_box(out, label, org, font_scale=0.62, thickness=2)

    return out


def make_rectified_debug(image_bgr: np.ndarray, cfg: dict, H_mm_to_px: np.ndarray, results: List[dict], px_per_mm: float = 5.0) -> np.ndarray:
    page_w, page_h = map(float, cfg["page_mm"])
    dst_w, dst_h = int(round(page_w * px_per_mm)), int(round(page_h * px_per_mm))
    S = np.array([[px_per_mm, 0, 0], [0, px_per_mm, 0], [0, 0, 1]], dtype=np.float64)
    H_img_to_dst = S @ np.linalg.inv(H_mm_to_px)
    rectified = cv2.warpPerspective(image_bgr, H_img_to_dst, (dst_w, dst_h), flags=cv2.INTER_LINEAR, borderValue=(235, 235, 235))

    by_id = {r["id"]: r for r in results}
    for patch in cfg["patches"]:
        x, y, w, h = map(float, patch["rect_mm"])
        p0 = (int(round(x * px_per_mm)), int(round(y * px_per_mm)))
        p1 = (int(round((x + w) * px_per_mm)), int(round((y + h) * px_per_mm)))
        inset = float(cfg["sampling"]["inset_fraction"])
        s0 = (int(round((x + w * inset) * px_per_mm)), int(round((y + h * inset) * px_per_mm)))
        s1 = (int(round((x + w * (1 - inset)) * px_per_mm)), int(round((y + h * (1 - inset)) * px_per_mm)))
        cv2.rectangle(rectified, p0, p1, (0, 0, 0), 1)
        cv2.rectangle(rectified, s0, s1, (255, 255, 0), 1)

        # Expected swatch occupies a small corner of each measured patch.
        sw = max(10, int(round(min(w, h) * px_per_mm * 0.28)))
        sx, sy = p0[0] + 2, p0[1] + 2
        cv2.rectangle(rectified, (sx, sy), (sx + sw, sy + sw), bgr(patch["rgb"]), -1)
        cv2.rectangle(rectified, (sx, sy), (sx + sw, sy + sw), (255, 255, 255), 1)

        r = by_id.get(patch["id"])
        if r:
            txt = f"{patch['id']} {r['similarity_percent']:.1f}%"
            ty = max(16, p0[1] - 6)
            draw_text_box(rectified, txt, (p0[0], ty), font_scale=0.48, thickness=1)

    title = "Rectified view: cyan=sample area, small swatch=expected RGB"
    draw_text_box(rectified, title, (18, dst_h - 18), font_scale=0.62, thickness=2)
    return rectified


def compose_debug(original_overlay: np.ndarray, rectified: np.ndarray) -> np.ndarray:
    left, _ = fit_image(original_overlay, 1500, 1800)
    right, _ = fit_image(rectified, 1300, 1800)
    target_h = max(left.shape[0], right.shape[0])

    def pad_to_h(im: np.ndarray, h: int) -> np.ndarray:
        if im.shape[0] == h:
            return im
        pad = np.full((h - im.shape[0], im.shape[1], 3), 235, np.uint8)
        return np.vstack([im, pad])

    left = pad_to_h(left, target_h)
    right = pad_to_h(right, target_h)
    gap = np.full((target_h, 20, 3), 32, np.uint8)
    return np.hstack([left, gap, right])
