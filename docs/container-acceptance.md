# Container acceptance, 2026-11-27

## Build under test

The standalone checkout at d77993ad19bb6725b9eb487e696e4130a90d55f8 produced a single OCI index for linux/amd64 and linux/arm64. Its digest is `sha256:50f1bb130ac44c8939f08e29c715f5efabbc1e4b9c7a4610ab5cad52d03d5a05`. The OCI tar file has SHA-256 `15e3b74d7d4ce8386af713ec3347d5e9f8afa3f29f905ea8a9dc59879bb8cffa`. The verifier checked both platform manifests, configurations, revision labels and layers.

## Install the local archive on Ubuntu

Run from the matching checkout. The following commands passed on Docker Engine 29.1.3 with the containerd image store. Copy the OCI archive into dist/ first when using an extracted source ZIP.

```bash
sudo docker image load --input dist/outbound-proxy-d77993a.oci.tar
sudo sh scripts/docker-proxy.sh install --local-only --image sha256:50f1bb130ac44c8939f08e29c715f5efabbc1e4b9c7a4610ab5cad52d03d5a05
```

Open [the panel on the installing host](http://127.0.0.1:18890/). To publish forwarding to a chosen LAN, follow [container installation](container-install.md). For other Docker image stores, see the platform archive import in [image channels](image-channels.md), then pass the loaded image's local ID.

## Verification

| Scope | Observed result |
|---|---|
| Standalone import | The clean extraction passed all 129 original tests with the MiniPC checkout hidden. |
| Final service tests | 162 pytest tests and 2 subtests passed, including browser and TLS coverage. The later OCI smoke-only change did not modify the service or its tests. |
| Actual product archive | Installed into Ubuntu 24.04.5 with Docker Engine 29.1.3. Chromium opened a fresh loopback session without pairing. A tokenless write was rejected. A signed change was visible through the CLI. The service was removed and its catalog retained. |
| Linux LAN and HTTPS | Two Ubuntu KVM machines shared a separate LAN. An isolated test upstream required TLS and authentication. A fixture image added only its private test CA. Host, permitted peer and opted-in Docker client completed CONNECT and HTTPS requests. The excluded source and direct LAN access to the panel were denied. |
| Maintenance with a saved catalog | Verified port change, occupied-port rejection, rollback, local-only configuration, two removals and reinstall with the selection retained. An unavailable upstream stopped replacement before the previous container was disturbed. |
| Linux host reboot | The host restored its scoped nftables rules and Compose service. The permitted peer again completed an HTTPS request. The excluded peer address and LAN management remained blocked. |
| ARM64 | Docker ran the ARM64 runtime module under QEMU emulation from the final multi-platform index. |
| Windows and macOS | PowerShell installer logic and simulated Firewall/PF tests passed. Real Desktop networking and reboot checks on these platforms remain open. |
| Delivery | Local archive and version-matched scripts available. Remote branch protection, PR preview publication, registry pull and promotion require repository-owner configuration and live acceptance. |

The operating MiniPC remains on its native installation. Its cutover requires an operator decision.
