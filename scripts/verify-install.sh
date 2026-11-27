#!/bin/sh
# Verify the installed forwarding state from the host.
set -eu
case "$(/usr/bin/uname -s)" in
    Darwin) operator=/Library/PrivilegedHelperTools/outbound-proxy/outbound-proxy ;;
    Linux) operator=/usr/local/bin/outbound-proxy ;;
    *) exit 1 ;;
esac
panel=$("$operator" ui)
page=$(curl -q --silent --show-error --noproxy '*' --max-time 5 --output /dev/null --write-out '%{http_code}' "$panel")
private=$(curl -q --silent --show-error --noproxy '*' --max-time 5 --output /dev/null --write-out '%{http_code}' "${panel}api/state")
printf 'Panel page and unauthenticated API status: %s %s\n' "$page" "$private"
test "$page" = 200 && test "$private" = 403 || exit 1
state=$("$operator" status)
printf '%s\n' "$state"
port=$(printf '%s\n' "$state" | /usr/bin/python3 -IS -c 'import json,sys; s=json.load(sys.stdin); assert s["enabled"] == s["running"] == s["boot_enabled"] and not s["recovery_pending"], "Unexpected service state"; print(s["port"] if s["enabled"] else "off")')
if [ "$port" != off ]; then
    outcome=$(curl -q --silent --show-error --max-time 20 --noproxy "" --proxy "http://127.0.0.1:$port" --output /dev/null --write-out '%{http_connect} %{http_code}' https://example.com/)
    printf 'CONNECT and target HTTP status: %s\n' "$outcome"
    test "$outcome" = '200 200'
fi
