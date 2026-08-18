# Radxa Cubie A7S IMX477 bring-up

This repository is a bring-up snapshot for an Arducam B0242 / Sony IMX477
camera on the Radxa Cubie A7S (Allwinner A733).

The current driver produces coherent 1920x1080 NV12 frames from a four-lane
RAW10 sensor stream. It has completed 300-frame captures without TDM, ISP,
parser, or I2C errors. This is not yet a production-quality camera stack: the
closed Allwinner ISP602 userspace library has no IMX477 tuning profile, so the
image is dark, magenta, and vertically banded.

## Tested platform

- Board: Radxa Cubie A7S / A733
- Camera: Arducam B0242 / Sony IMX477
- Kernel: `5.15.147-21-a733`
- Sensor mode: 1920x1080, RAW10, four CSI-2 lanes, nominal 60 fps
- Capture output: 1920x1080 NV12 on `/dev/video0`
- Upstream BSP base: `radxa/allwinner-bsp` commit
  `c8fb29d68c58ae557e8fb96ae829ca2693936a75`

## What is implemented

- IMX477 chip-ID validation.
- 1920x1080 RAW10 four-lane sensor mode.
- Per-register sensor-table writes with error reporting and short pacing for
  the A733 TWI controller.
- A VIN workaround that prevents AWISP's 2-in-1 DMA merge request from making
  TDM program half-width 1128x1080 buffers for a 1920x1080 linear stream.
- Runtime TDM configuration logging for bring-up diagnostics.
- Device-tree overlay: `dts/cubie-a7a-arducam-imx477.dts`.

## Known limitations

- The first stream after boot currently fails. Stop it and open the camera a
  second time; the retry produces a valid frame.
- Consecutive captures can alternate between about 60 fps and about 30 fps.
  AWISP still announces `STITCH_2IN1_LINNER`; a 30 fps capture is the reliable
  fallback.
- ISP output is not calibrated. The installed
  `libAWIspApi-isp-602-arm64` package only contains IMX214, IMX219, and IMX415
  profiles. An ISP602 IMX477 profile is still required for correct exposure,
  white balance, black level, color, and lens-shading correction.
- The DMA-merge workaround is intentionally limited to 1920x1080 and should be
  generalized once the AWISP configuration path is understood.

## Build

On the target board:

```sh
./scripts/imx477/build.sh
```

The build uses the running kernel headers and produces:

```text
drivers/vin/modules/sensor/imx477_mipi.ko
drivers/vin/vin_v4l2.ko
```

Install with backups, then reboot:

```sh
./scripts/imx477/install.sh
sudo reboot
```

## Capture test

Capture 300 frames to a sink:

```sh
./scripts/imx477/test-300-frames.sh
```

Capture one NV12 frame:

```sh
gst-launch-1.0 -e \
  v4l2src device=/dev/video0 io-mode=2 num-buffers=1 \
  ! 'video/x-raw,format=NV12,width=1920,height=1080,framerate=60/1' \
  ! filesink location=imx477-1920x1080.nv12
```

Each complete NV12 frame is 3,110,400 bytes.

## Installed reference hashes

The tested snapshot used these module hashes:

```text
bb748881fb09d2a3f3f1904dd2d2beaf71f947ecec0ffa91292f8e7a538578f5  imx477_mipi.ko
6f246cdcedea6235f520031718706de837042694c015f5fa178fdc17838e34c2  vin_v4l2.ko
```

The modules are not committed because they are kernel-version-specific and can
be reproduced from source.
