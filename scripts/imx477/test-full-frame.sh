#!/bin/sh
set -eu

output=${1:-imx477-4056x3040.nv12}

gst-launch-1.0 -e \
  v4l2src device=/dev/video1 io-mode=2 num-buffers=1 \
  ! 'video/x-raw,format=NV12,width=4056,height=3040,framerate=30/1' \
  ! filesink location="$output"

expected=18495360
actual=$(stat -c %s "$output")
if [ "$actual" -ne "$expected" ]; then
  echo "unexpected file size: got $actual bytes, expected $expected" >&2
  exit 1
fi

echo "captured $output ($actual bytes)"
