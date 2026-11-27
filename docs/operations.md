# Configure forwarding

Run the following sequence on a fresh installation. At the hidden prompt enter an HTTPS proxy URI with a DNS hostname, username and password. An omitted upstream port defaults to 443. Include the port for any other value. For automation, use protected standard input. Keep credentials out of shell arguments and history.

```bash
sudo outbound-proxy add office
sudo outbound-proxy check office
sudo outbound-proxy use office
sudo outbound-proxy enable
sudo outbound-proxy status
```

Point clients at `http://127.0.0.1:18888`. Preserve application-specific bypass settings. The status output includes the effective service state and the latest check. Inspect, edit or remove named entries with the following commands. Select a replacement before deleting the selected entry.

```bash
sudo outbound-proxy list
sudo outbound-proxy update office
sudo outbound-proxy remove retired
```

## Open the browser panel

When upgrading from the foreground-only panel, close any running terminal UI session before installation or removal.

Open [the panel](http://127.0.0.1:18890/) in a browser on the installed host. Enter the one-time pairing code from the installation terminal. A paired browser can reopen the page after a reload, service restart or reboot. The panel starts independently of forwarding. On a staged MiniPC, the panel shows the standalone catalog. An empty catalog imports the legacy entries. To keep staged names and order, enter the complete matching upstream list and select the current legacy route. Use the migration inspection command to view the current legacy selection.

To pair a new browser profile, replace an expired code, or pair after an unattended installation, issue a code in a local administrative terminal.

```bash
sudo outbound-proxy ui pair
```

## Port, failover, containers and boot

Set the forwarding port to an available port between 1024 and 65535. Ports 18889 and 18890 are reserved for the tunnel and browser panel respectively. If an older installation uses 18890 for forwarding, select another forwarding port with the existing CLI before upgrading. After a successful change, update client proxy settings to the displayed port.

```bash
sudo outbound-proxy port 19999
```

Automatic failover requires operator opt-in. The scheduler confirms an active-path failure twice before trying alternates in catalog order. A healthy alternate stays selected.

```bash
sudo outbound-proxy failover on
sudo outbound-proxy failover off
```

For authorized Linux containers, enable dedicated bridge access on a host with Docker Engine, iproute2 and nftables. Attach only intended containers to `minipc-outbound-proxy`. Membership grants access to the gateway at `192.168.70.1` on the configured port. Disabling access retains attached containers and their network. The macOS release supports loopback forwarding.

```bash
sudo outbound-proxy docker on
sudo outbound-proxy docker off
```

On Linux, open **Docker bridge** in the paired browser panel. Use **Refresh status** to read the current link state, subnet, gateway and attached containers with their IPv4 addresses. The list shows network membership even when forwarding or Docker access is off. A missing network or an unavailable Docker daemon has its own status.

Disable forwarding and boot startup together. The catalog, selection and failover preference persist. Enable resumes forwarding and boot startup. Validate the host path after installation or restart.

```bash
sudo outbound-proxy disable
sudo outbound-proxy enable
sudo sh scripts/verify-install.sh
```

## Migrate an existing MiniPC

Stage the package only after reviewing the standalone platform results and migration rehearsal. Staging installs the new tools alongside the old owner of the port. Inspect the selected upstream, current dedicated bridge members and failover settings. Review any differences from the expected inventory.

```bash
sudo sh scripts/install-outbound-proxy.sh --stage
sudo outbound-proxy migrate inspect
```

After the independent integration review, run cutover. The operation saves a private backup, probes the candidate, quiesces the old units and verifies fresh host and bridge requests. With an empty catalog, it generates names such as `proxy-1`. With a populated catalog, it preserves the staged names and order after checking every authenticated endpoint, the selected route, port, bridge and schedule. Add the optional flag `--expect-selected NAME` to require a specific route before handover. A brief interruption can close existing connections.

```bash
sudo outbound-proxy migrate cutover
sudo sh scripts/verify-install.sh
```

Verify APT and Docker connectivity, application health, existing bypass routes and denial from outside the authorized network. After all required checks pass, record acceptance. If any check fails or inspection reports an interrupted cutover, invoke rollback. The backup remains under `/etc/outbound-proxy/legacy-backup`.

```bash
sudo outbound-proxy migrate accept
```

```bash
sudo outbound-proxy migrate rollback
```

## Remove the standalone services

From the repository root, run the removal script. It stops the standalone panel and forwarding services, removes their native registrations and cancels their automatic startup. Complete or roll back a migration in progress before removal.

```bash
sudo sh scripts/uninstall-outbound-proxy.sh
```

The private catalog, selection and MiniPC recovery backup remain on disk. Removal resets the forwarding port to 18888 if a pre-panel installation saved the now-reserved port 18890. A later installation starts with forwarding disabled. Pair the browser again to manage the reinstalled service. The same script can finish an interrupted removal.
