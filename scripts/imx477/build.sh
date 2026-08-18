#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
kver=$(uname -r)
kbuild=/lib/modules/$kver/build

test -d "$kbuild"

make -C "$kbuild" M="$repo/drivers/vin/modules/sensor" modules
make -C "$kbuild" M="$repo/drivers/vin" modules
gcc -nostdlib -shared -fPIC -O2 \
	"$repo/scripts/imx477/isp_no3dn_preload.c" \
	-Wl,-soname,libisp_no3dn.so \
	-o "$repo/scripts/imx477/libisp_no3dn.so"

sha256sum \
	"$repo/drivers/vin/modules/sensor/imx477_mipi.ko" \
	"$repo/drivers/vin/vin_v4l2.ko" \
	"$repo/scripts/imx477/libisp_no3dn.so"
