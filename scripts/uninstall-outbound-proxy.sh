#!/bin/sh
# Run from an operator-reviewed checkout.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONHOME PYTHONPATH
if [ "$(id -u)" -ne 0 ]; then
    echo "Run the outbound proxy removal script as root." >&2
    exit 1
fi
if [ ! -x /usr/bin/python3 ]; then
    echo "Missing /usr/bin/python3. Install system Python or macOS Command Line Tools." >&2
    exit 1
fi
scripts=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 -IS - "$scripts" "$@" <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Requires system Python 3.9 or later")
sys.path.insert(0, sys.argv.pop(1))
from outbound_proxy.uninstall import main
main()
PY
