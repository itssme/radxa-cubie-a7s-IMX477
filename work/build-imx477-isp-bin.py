#!/usr/bin/env python3
"""Build an A733 ISP602 binary profile from an embedded profile payload.

The vendor binary format is: uint32 payload_size, 20-byte metadata,
50-byte profile name, followed by the 0x1c63c-byte parameter payload.
"""
from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

PAYLOAD_SIZE = 0x1C63C


# A733 stores nine Q8 matrix coefficients followed by three additive RGB
# offsets. Keep this flat representation in that order everywhere.
IMX477_Q8 = (394, -51, 14,
             -52, 433, -116,
             -85, -126, 358,
             0, 0, 0)


CCM_OFFSETS = (0x154AA, 0x154C2, 0x154DA)
BAYER_GAIN_OFFSET = 0xC24
BAYER_GAIN_UNITY = 0x400


def matrix_mul(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [[sum(left[i][k] * right[k][j] for k in range(3))
             for j in range(3)] for i in range(3)]


def ccm_transform(ccm: tuple[int, ...], saturation: float,
                  hue_degrees: float) -> tuple[int, ...]:
    """Apply output-space saturation and hue rotation to a Q8 3x3 CCM.

    The A733 matrices map sensor RGB to output RGB.  The transform therefore
    left-multiplies only the first nine coefficients and leaves the final
    three offsets unchanged. This is deliberately kept in floating point
    until final Q8 rounding.
    """
    if saturation < 0:
        raise ValueError("CCM saturation must be non-negative")
    angle = math.radians(hue_degrees)
    # Rec. 601 luma axis.  Rodrigues rotation gives a smooth, reversible hue
    # control without changing the neutral axis.
    axis = (0.299, 0.587, 0.114)
    norm = math.sqrt(sum(v * v for v in axis))
    x, y, z = (v / norm for v in axis)
    c, s = math.cos(angle), math.sin(angle)
    one_c = 1.0 - c
    hue = [
        [c + x * x * one_c, x * y * one_c - z * s, x * z * one_c + y * s],
        [y * x * one_c + z * s, c + y * y * one_c, y * z * one_c - x * s],
        [z * x * one_c - y * s, z * y * one_c + x * s, c + z * z * one_c],
    ]
    lum = [
        [0.299, 0.587, 0.114],
        [0.299, 0.587, 0.114],
        [0.299, 0.587, 0.114],
    ]
    sat = [[lum[i][j] + saturation * ((1.0 if i == j else 0.0) - lum[i][j])
            for j in range(3)] for i in range(3)]
    transform = matrix_mul(hue, sat)
    source = [[ccm[i * 3 + j] / 256.0 for j in range(3)] for i in range(3)]
    result = matrix_mul(transform, source)
    offsets = list(ccm[9:12])
    values: list[int] = []
    for row in range(3):
        values.extend(max(-32768, min(32767, round(result[row][col] * 256.0)))
                      for col in range(3))
    values.extend(offsets)
    return tuple(values)


def make_profile(payload_path: Path, output_path: Path,
                 matrix: str | None, blend_imx477: float | None,
                 rg_gain: float, bg_gain: float,
                 ccm_saturation: float, ccm_hue_degrees: float,
                 bank: int | None) -> None:
    payload = bytearray(payload_path.read_bytes())
    if len(payload) != PAYLOAD_SIZE:
        raise SystemExit(f"payload is {len(payload)} bytes, expected {PAYLOAD_SIZE}")

    if matrix and blend_imx477 is not None:
        raise SystemExit("use either --matrix or --blend-imx477")

    if matrix:
        # Use the Raspberry Pi IMX477 sRGB matrix for the selected bank(s).
        matrices = {
            "imx477": IMX477_Q8,
            "identity": (256, 0, 0, 0, 256, 0, 0, 0, 256, 0, 0, 0),
            "swap-rb": (0, 0, 256, 0, 256, 0, 256, 0, 0, 0, 0, 0),
        }
        ccm = matrices[matrix]
        offsets = CCM_OFFSETS if bank is None else (CCM_OFFSETS[bank],)
        for offset in offsets:
            struct.pack_into("<12h", payload, offset, *ccm)

    if blend_imx477 is not None:
        if not 0.0 <= blend_imx477 <= 1.0:
            raise SystemExit("--blend-imx477 must be between 0 and 1")
        # Keep each fallback CCT bank's gains and interpolate only toward the
        # Q8-scaled Raspberry IMX477 CCM.  This is intentionally conservative:
        # it avoids throwing away the fallback profile's AWB/CCT structure.
        offsets = CCM_OFFSETS if bank is None else (CCM_OFFSETS[bank],)
        for offset in offsets:
            base = struct.unpack_from("<12h", payload, offset)
            # Interpolate coefficients only. Offsets are deliberately reset
            # to zero for generated calibration profiles.
            blended = [round(a + blend_imx477 * (b - a))
                       for a, b in zip(base[:9], IMX477_Q8[:9])] + [0, 0, 0]
            struct.pack_into("<12h", payload, offset, *blended)

    if rg_gain <= 0 or bg_gain <= 0:
        raise SystemExit("--rg-gain and --bg-gain must be positive")
    gains = (round(BAYER_GAIN_UNITY * rg_gain), BAYER_GAIN_UNITY,
             BAYER_GAIN_UNITY, round(BAYER_GAIN_UNITY * bg_gain))
    struct.pack_into("<4I", payload, BAYER_GAIN_OFFSET, *gains)

    if ccm_saturation != 1.0 or ccm_hue_degrees != 0.0:
        offsets = CCM_OFFSETS if bank is None else (CCM_OFFSETS[bank],)
        for offset in offsets:
            base = struct.unpack_from("<12h", payload, offset)
            transformed = ccm_transform(base, ccm_saturation, ccm_hue_degrees)
            struct.pack_into("<12h", payload, offset, *transformed)

    name = b"imx477_mipi_3840_2160_30_0"
    header = struct.pack("<I", PAYLOAD_SIZE) + bytes(20) + name.ljust(50, b"\0")
    output_path.write_bytes(header + payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--matrix", choices=("imx477", "identity", "swap-rb"))
    parser.add_argument("--blend-imx477", type=float,
                        help="interpolate fallback CCM banks toward IMX477 Q8 CCM")
    parser.add_argument("--rg-gain", type=float, default=1.0,
                        help="red/green Bayer gain ratio (unity is 1.0)")
    parser.add_argument("--bg-gain", type=float, default=1.0,
                        help="blue/green Bayer gain ratio (unity is 1.0)")
    parser.add_argument("--ccm-saturation", type=float, default=1.0,
                        help="output-space CCM saturation multiplier")
    parser.add_argument("--ccm-hue-degrees", type=float, default=0.0,
                        help="output-space CCM hue rotation in degrees")
    parser.add_argument("--bank", type=int, choices=(0, 1, 2),
                        help="modify only this CCM bank (default: all banks)")
    args = parser.parse_args()
    make_profile(args.payload, args.output, args.matrix, args.blend_imx477,
                 args.rg_gain, args.bg_gain, args.ccm_saturation,
                 args.ccm_hue_degrees, args.bank)
