#!/bin/sh
set -eu

host=${IMX477_HOST:-radxa@192.168.0.41}
remote_repo=${IMX477_REMOTE_REPO:-/home/radxa/imx477-build}
ssh_config=${IMX477_SSH_CONFIG:-/dev/null}
output=${1:-imx477-tuning-frame.png}
remote_output="/tmp/imx477-tuning-frame-$$.png"

cleanup()
{
	ssh -F "$ssh_config" "$host" "rm -f '$remote_output'" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

ssh -F "$ssh_config" "$host" sh -s -- "$remote_repo" "$remote_output" <<'REMOTE'
set -eu
cd "$1"
./scripts/imx477/capture-tuning-frame.sh "$2"
REMOTE

scp -F "$ssh_config" "$host:$remote_output" "$output"
echo "downloaded frame 10 as $output"
