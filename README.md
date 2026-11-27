# Standalone outbound proxy

Run one local web panel and an HTTP forwarding proxy backed by an authenticated HTTPS upstream. The Docker edition runs both processes in a single container. Choose the container edition for an independent installation on a Docker host. The native tools remain available for Debian, Ubuntu and macOS installations.

## Docker installation

Use the deployment script from the same reviewed revision as the image, together with its Compose file. Supply the immutable image reference printed by the build. The installer can publish the forwarding port to a selected LAN using a host address and permitted IPv4 subnet. Linux Engine has been exercised with a second machine. Physical Windows and macOS Desktop acceptance is pending. The deployment script checks host-local management isolation before admitting the installation.

Open [the local panel](http://127.0.0.1:18890/) on the Docker host. The container panel opens without a manual pairing code. Select a verified upstream and enable forwarding. The default client port is 18888. The panel stays up when forwarding is disabled. The deployment script controls host port publication and LAN access.

- [Install, reconfigure, back up, roll back and remove the container](docs/container-install.md)
- [Select a PR preview, integration build or stable release](docs/image-channels.md)

## Native installation

Use system Python 3.9 or later. On Debian or Ubuntu with systemd, install the executable dependencies.

```bash
sudo apt-get install python3 tinyproxy-bin stunnel4 curl ca-certificates openssl
```

The panel uses the system OpenSSL executable to verify browser signatures. On macOS, supply the Apple system Python through Command Line Tools and install the forwarding binaries with Homebrew as your normal user. The installer preflights the required paths and privileges.

```bash
brew install tinyproxy stunnel
```

If this host already runs the legacy MiniPC proxy, follow [the staging and migration procedure](docs/operations.md#migrate-an-existing-minipc). Staging installs the new CLI alongside the running proxy. The operator switches traffic separately.

For a fresh host or an installation with an accepted migration, run the installer from this repository with administrative privileges. Repeating the install preserves the catalog and active settings.

```bash
sudo sh scripts/install-outbound-proxy.sh
```

Open [the local panel](http://127.0.0.1:18890/) on the installed host and pair the browser using the code from the installation terminal. See the [browser guide](docs/operations.md#open-the-browser-panel) for unattended installations.

On Linux the installed command is in `/usr/local/bin`. On macOS, open a new login shell or invoke the command by its full path in `/Library/PrivilegedHelperTools/outbound-proxy/`.

## Operator guides

- [Configure forwarding, manage upstreams and migrate MiniPC](docs/operations.md)
- [Remove the standalone services](docs/operations.md#remove-the-standalone-services)
- [Operate the legacy MiniPC service](docs/legacy-minipc.md)
- [Installation and verification scripts](scripts/)
- [Proxy tests](tests/)
