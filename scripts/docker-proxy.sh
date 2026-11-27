#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/docker_deploy.py" "$@"
