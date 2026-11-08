#!/bin/sh
set -eu
. "$(dirname -- "$0")/mac_env.sh"
exec "$BOT_PYTHON" -m scripts.mac_runtime backup
