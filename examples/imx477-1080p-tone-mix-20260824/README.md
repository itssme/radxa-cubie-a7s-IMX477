# IMX477 1080p tone-mix reference capture

This example was captured on the Radxa Cubie A7S with the IMX477 at
1920×1080 through `/dev/video0`, using the current `tone-mix` ISP profile.

Capture settings:

- forced sensor exposure: `122880` 16-line units
- forced sensor gain: `16`
- Bayer mode: RGGB RAW10, processed to NV12 by ISP602
- output: 1920×1080 JPEG converted from the final NV12 frame
- profile: tone-mix calibration profile, SHA-256
  `081ad06baa820e4884c6a1818c4eaf9bbe56a92ebccea656f4f4df8e041c97b0`

The debug image uses automatic ArUco detection on this same frame and shows
the sampled patch polygons, measured similarity labels, and rectified chart.
The resulting evaluator summary is approximately 67.91% similarity and
81.83 RGB MAE. That numerical score is not directly comparable to the
earlier 4K chart runs because this 1080p frame was captured under a different
exposure/rendering condition; the purpose of this example is to record the
visually realistic 1080p result and its geometry/evaluation overlay.
