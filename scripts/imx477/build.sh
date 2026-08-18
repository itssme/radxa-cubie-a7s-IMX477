#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
kver=$(uname -r)
kbuild=/lib/modules/$kver/build

test -d "$kbuild"

make -C "$kbuild" M="$repo/drivers/vin/modules/sensor" modules
make -C "$kbuild" M="$repo/drivers/vin" modules

sha256sum \
	"$repo/drivers/vin/modules/sensor/imx477_mipi.ko" \
	"$repo/drivers/vin/vin_v4l2.ko"
