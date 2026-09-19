#!/bin/sh
set -eu

output=${1:-imx477-tuning-frame.png}
frame_size=12441600
frame_count=10
expected_size=$((frame_size * frame_count))
raw=$(mktemp "${TMPDIR:-/tmp}/imx477-tuning-XXXXXXXX.nv12")

cleanup()
{
	rm -f "$raw"
}
trap cleanup EXIT HUP INT TERM

LD_PRELOAD=/usr/local/lib/libisp_no3dn.so gst-launch-1.0 -e \
	v4l2src device=/dev/video1 io-mode=2 num-buffers=$frame_count \
	! 'video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1' \
	! filesink location="$raw"

actual_size=$(stat -c %s "$raw")
if [ "$actual_size" -ne "$expected_size" ]; then
	echo "unexpected capture size: got $actual_size bytes, expected $expected_size" >&2
	exit 1
fi

ffmpeg -hide_banner -loglevel error -y \
	-f rawvideo \
	-pixel_format nv12 \
	-video_size 3840x2160 \
	-framerate 30 \
	-i "$raw" \
	-vf 'select=eq(n\,9)' \
	-frames:v 1 \
	"$output"

echo "captured frame 10 of $frame_count as $output"
