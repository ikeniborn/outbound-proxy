# LAN forwarding on Windows Docker Desktop

Open an elevated PowerShell session as the same Windows user who owns the deployment and Docker Desktop context. Select the host's active Ethernet or Wi-Fi IPv4 address and a permitted source CIDR within its attached subnet. Replace the example addresses below.

```powershell
.\scripts\docker-proxy.ps1 reconfigure -Lan 192.0.2.10 -Subnet 192.0.2.64/26 -Port 18888
```

For a fresh installation, use `install` with the same options and the reviewed `-Image`. Management stays at `http://127.0.0.1:18890/`. The client proxy URL uses the selected LAN IP and forwarding port. Enable forwarding from the local panel after selecting a working HTTPS upstream.

Windows Defender Firewall must be enabled in the Domain, Private and Public profiles. The installer checks that the selected adapter participates in filtering. It installs two persistent rules for the selected IPv4 address and TCP port. One permits the specified subnet on the selected adapter. The other blocks the remaining IPv4 source ranges. The installer checks the effective rules in ActiveStore and rejects applicable authenticated bypass rules. Docker Desktop accepts published connections through its Windows backend. [Microsoft firewall rule behavior](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/rules) · [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)

Before starting the service with the catalog, the installer uses an empty HTTP listener on the selected LAN endpoint. It checks the local HTTP response, the owning Windows process, and denial from ordinary containers. It also checks the loopback management boundary. A failed replacement restores the previous settings when their checks pass. If recovery fails, the deployment journal remains pending.

The rules persist across a reboot. Docker uses the service's configured restart policy. After changing Windows Firewall policy, the network adapter or the Desktop backend, reconfigure and repeat the checks. To withdraw LAN access, run the following command in the same elevated session. It stops publication before removing the deployment-owned firewall rules.

```powershell
.\scripts\docker-proxy.ps1 reconfigure -LocalOnly
```

## Qualify a Windows host and backend

Use an existing client inside the allowed subnet and another routed client outside it. These are release acceptance checks. The installer performs its own host checks on each deployment. Record the Windows and Docker Desktop versions, backend, image index digest, addresses and results.

1. Enable forwarding and confirm the host-local panel opens.
2. From the permitted client, make a fresh HTTPS request through the proxy. Expect `200 200`, for CONNECT and the HTTPS response.
3. From the excluded client, repeat the request. Expect the TCP connection to the proxy to fail. Use a known routed client so a missing network route cannot explain the result.
4. From both clients, check the management address. Expect the TCP connection to fail.
5. Restart Docker Desktop or reboot Windows. Repeat these checks and verify that the selected image and catalog remain available.

Run these examples on Windows client machines. On POSIX clients use `curl` and replace `NUL` with `/dev/null`.

```powershell
curl.exe --disable --silent --show-error --noproxy unused.invalid --proxy http://192.0.2.10:18888 --max-time 15 --output NUL --write-out '%{http_connect} %{http_code}' https://example.com/
curl.exe --disable --noproxy '*' --connect-timeout 4 http://192.0.2.10:18890/
```

Live Windows Desktop LAN acceptance remains pending. The repository includes PowerShell tests for source ranges, effective policy, rollback, rule ownership and service readiness.
