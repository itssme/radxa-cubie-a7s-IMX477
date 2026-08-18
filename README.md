# Radxa Cubie A7S IMX477 bring-up

This repository is a bring-up snapshot for an Arducam B0242 / Sony IMX477
camera on the Radxa Cubie A7S (Allwinner A733).

The current driver produces coherent 1920x1080 and 3840x2160 NV12 frames from
four-lane RAW10 sensor streams. The UHD mode uses a centered sensor-side crop
of 108 pixels per horizontal edge and 440 per vertical edge. Capture uses
the A733's paired TDM/ISP large-image path. This is not yet a production-quality
camera stack:
the closed Allwinner ISP602 userspace library has no IMX477 tuning profile, so
the image is dark, magenta, and vertically banded.

## Tested platform

- Board: Radxa Cubie A7S / A733
- Camera: Arducam B0242 / Sony IMX477
- Kernel: `5.15.147-21-a733`
- Sensor mode: 1920x1080, RAW10, four CSI-2 lanes, nominal 60 fps
- Capture output: 1920x1080 NV12 on `/dev/video0`; 3840x2160 NV12 on
  `/dev/video1`
- Upstream BSP base: `radxa/allwinner-bsp` commit
  `c8fb29d68c58ae557e8fb96ae829ca2693936a75`

## What is implemented

- IMX477 chip-ID validation.
- 1920x1080 RAW10 four-lane sensor mode.
- 3840x2160 RAW10 four-lane sensor mode, nominal 30 fps. The sensor performs
  the centered crop before transmitting CSI-2 data.
- Discrete V4L2 frame-size enumeration for both sensor modes.
- Paired ISP/TDM large-image capture for the 3840-pixel-wide mode.
- Per-register sensor-table writes with error reporting and short pacing for
  the A733 TWI controller.
- A VIN workaround that prevents AWISP's 2-in-1 DMA merge request from making
  TDM program half-width 1128x1080 buffers for a 1920x1080 linear stream.
- Runtime TDM configuration logging for bring-up diagnostics.
- Large-image sensor exposure/gain updates are temporarily frozen. The two
  ISP instances otherwise issue updates to the same sensor and trigger A733
  TWI failures during full-frame streaming.
- Device-tree overlay: `dts/cubie-a7a-arducam-imx477.dts`.

## Known limitations

- The first stream after boot currently fails. Stop it and open the camera a
  second time; the retry produces a valid frame.
- Consecutive captures can alternate between about 60 fps and about 30 fps.
  AWISP still announces `STITCH_2IN1_LINNER`; a 30 fps capture is the reliable
  fallback.
- A 180-frame end-to-end sink test completes in about 6.2 seconds, or roughly
  29 fps, through the paired merge path. A 30-frame raw capture has no missing
  pixels in either 1920-pixel half.
- Do not switch directly between the 1920x1080 and 3840x2160 paths. Opening
  `/dev/video1` after a `/dev/video0` capture can hard-lock the current VIN
  stack. Reboot before changing modes; clean-boot full-frame capture is
  repeatable.
- Automatic exposure/gain is disabled only for large-image mode pending
  synchronized sensor control. The mode-table values remain active.
- ISP output is not calibrated. The installed
  `libAWIspApi-isp-602-arm64` package only contains IMX214, IMX219, and IMX415
  profiles. An ISP602 IMX477 profile is still required for correct exposure,
  white balance, black level, color, and lens-shading correction.
- The fallback OV13850 profile enables strong ISP temporal denoising, which
  produces faded copies of moving objects. Prefix capture commands with
  `LD_PRELOAD=/usr/local/lib/libisp_no3dn.so` to bypass the ISP602 D3D stage.
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
scripts/imx477/libisp_no3dn.so
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

Capture one UHD NV12 image through the paired large-image node:

```sh
./scripts/imx477/test-full-frame.sh imx477-3840x2160.nv12
```

The equivalent pipeline is:

```sh
gst-launch-1.0 -e \
  v4l2src device=/dev/video1 io-mode=2 num-buffers=1 \
  ! 'video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1' \
  ! filesink location=imx477-3840x2160.nv12
```

Each complete UHD NV12 image is 12,441,600 bytes.

For motion tests and streaming, bypass the fallback profile's temporal filter:

```sh
LD_PRELOAD=/usr/local/lib/libisp_no3dn.so gst-launch-1.0 -e \
  v4l2src device=/dev/video1 io-mode=2 num-buffers=1 \
  ! 'video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1' \
  ! fakesink sync=false
```

The preload hook only changes the ISP602 D3D bypass bit. It does not alter the
sensor mode, frame geometry, exposure, or spatial denoising.

## Installed reference hashes

The tested snapshot used these module hashes:

```text
b75284693ebd905851adb8ad87f03fdcce52b0cc0232b60e52e35888c3a09277  imx477_mipi.ko
57de17d1422b1472f29dde27ce79212960743b0e9888adca81ba063245614d9a  vin_v4l2.ko
```

The modules are not committed because they are kernel-version-specific and can
be reproduced from source.
