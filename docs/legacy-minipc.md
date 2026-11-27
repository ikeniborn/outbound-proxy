# Legacy MiniPC service

The following procedures apply while the original MiniPC service owns the listener.

Tinyproxy accepts HTTP proxy requests. It adds the upstream credentials and forwards to stunnel on 127.0.0.1:18889. Stunnel verifies the upstream TLS chain and hostname. Use http://127.0.0.1:18888 from host services and host-network Jellyfin. Use http://192.168.70.1:18888 from Grafana and rubfx on the dedicated minipc-outbound-proxy bridge. Keep the original bypass settings in each client.

## Rotate the upstream

Edit /etc/minipc-proxy/upstream.url as root. Keep its owner root and its mode 0600. Enter one or more distinct authenticated HTTPS URIs on one line, separated by commas. Use an explicit port for compatibility with the original MiniPC helper. The updated helper defaults to 443 when the port is omitted. Manual apply selects the first entry. Use percent-encoding for any comma within a credential. Use a protected editor and keep the URI out of command arguments and logs.

```bash
sudoedit /etc/minipc-proxy/upstream.url
sudo /usr/local/sbin/minipc-proxy-apply
curl -q --noproxy "" --proxy http://127.0.0.1:18888 --silent --show-error --max-time 20 --output /dev/null --write-out '%{http_connect} %{http_code}\n' https://example.com/
```

Expect two 200 codes. The helper tests the proposed upstream on temporary loopback ports before switching the two systemd services. It records the new generation after the stable endpoint passes its probe. It restores the previous generation if activation fails. Correct an unapplied source edit before retrying. Keep any saved copy of the previous URI root-only.

## Automatic failover

Enable minipc-proxy-failover.timer after applying the list. It checks the active local path about once a minute. After two failed probes separated by three seconds, it tries the other entries in file order on temporary loopback ports. It activates the first candidate that passes both CONNECT and the target request. If all candidates fail verification, it retains the verified generation. An activation failure stops the scan and triggers rollback. A working alternate stays active until it fails or an operator manually applies the first URL.

Manual apply and automatic checks share one lock. Activation restarts the local proxy pair. Existing tunnels can close during this change. Consumers keep their configured addresses and running processes.

```bash
systemctl list-timers minipc-proxy-failover.timer
sudo journalctl -u minipc-proxy-failover.service -n 10
```

### Change the check interval

The timer starts its next check one minute after the previous check finishes. OnUnitInactiveSec in minipc-proxy-failover.timer controls this interval. The apply helper controls the separate three-second confirmation retry.

Run the following command to edit the timer drop-in.

```bash
sudo systemctl edit minipc-proxy-failover.timer
```

To check two minutes after each completed check, add the following settings. The empty assignment clears the original schedule entry.

```ini
[Timer]
OnUnitInactiveSec=
OnUnitInactiveSec=2min
```

Restart the timer and verify the next run.

```bash
sudo systemctl restart minipc-proxy-failover.timer
systemctl list-timers minipc-proxy-failover.timer
```

## Check access and service recovery

```bash
systemctl is-active minipc-proxy-network minipc-proxy-stunnel minipc-proxy-tinyproxy
sudo nft list table inet minipc_proxy
sudo docker network inspect minipc-outbound-proxy --format '{{range .Containers}}{{.Name}} {{end}}'
sudo ss -H -ltn '( sport = :18888 or sport = :18889 )'
```

Expect Grafana and rubfx as the bridge members. Expect 18888 on loopback and the bridge gateway, and 18889 on loopback. A peer on the LAN or another Docker bridge must fail to connect to the local proxy. See the host outbound proxy inventory runbook in iwiki for migration evidence and protected backup locations.
