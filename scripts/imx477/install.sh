#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
kver=$(uname -r)
sensor_new=$repo/drivers/vin/modules/sensor/imx477_mipi.ko
vin_new=$repo/drivers/vin/vin_v4l2.ko
preload_new=$repo/scripts/imx477/libisp_no3dn.so
sensor_inst=/lib/modules/$kver/extra/imx477_mipi.ko
vin_inst=/lib/modules/$kver/kernel/bsp/drivers/vin/vin_v4l2.ko.xz
preload_inst=/usr/local/lib/libisp_no3dn.so
stamp=$(date +%Y%m%d-%H%M%S)
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT HUP INT TERM

test -f "$sensor_new"
test -f "$vin_new"
test -f "$preload_new"
test -f "$sensor_inst"
test -f "$vin_inst"

sudo cp -a "$sensor_inst" "$sensor_inst.before-imx477-$stamp"
sudo cp -a "$vin_inst" "$vin_inst.before-imx477-$stamp"

xz -C crc32 -c "$vin_new" >"$tmpdir/vin_v4l2.ko.xz"
sudo install -o root -g root -m 0644 "$sensor_new" "$sensor_inst"
sudo install -o root -g root -m 0644 "$tmpdir/vin_v4l2.ko.xz" "$vin_inst"
sudo install -o root -g root -m 0755 "$preload_new" "$preload_inst"
sudo depmod -a "$kver"

sha256sum "$sensor_inst"
xz -dc "$vin_inst" | sha256sum
sha256sum "$preload_inst"
echo "Installation complete. Reboot before using the camera."
