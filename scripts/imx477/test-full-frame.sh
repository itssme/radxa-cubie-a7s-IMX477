#!/bin/sh
set -eu

output=${1:-imx477-3840x2160.nv12}

gst-launch-1.0 -e \
  v4l2src device=/dev/video1 io-mode=2 num-buffers=1 \
  ! 'video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1' \
  ! filesink location="$output"

expected=12441600
actual=$(stat -c %s "$output")
if [ "$actual" -ne "$expected" ]; then
  echo "unexpected file size: got $actual bytes, expected $expected" >&2
  exit 1
fi

echo "captured $output ($actual bytes)"
