# Standalone outbound proxy

Use one local HTTP listener for applications on a Debian, Ubuntu or macOS host. Tinyproxy and stunnel connect to authenticated HTTPS upstreams. Fresh installs start with forwarding off. The operator can use the CLI or the browser page to change the same settings.

## Install

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
