#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/platforms/build.py" "$@"
