#!/bin/sh
set -eu

start=$(date +%s%N)

timeout 20s gst-launch-1.0 -e \
	v4l2src device=/dev/video0 io-mode=2 num-buffers=300 \
	! 'video/x-raw,format=NV12,width=1920,height=1080,framerate=60/1' \
	! fakesink sync=false

end=$(date +%s%N)
elapsed_ms=$(((end - start) / 1000000))
fps=$(awk -v ms="$elapsed_ms" 'BEGIN { printf "%.3f", 300000 / ms }')

echo "300 frames in ${elapsed_ms} ms (${fps} end-to-end fps)"
