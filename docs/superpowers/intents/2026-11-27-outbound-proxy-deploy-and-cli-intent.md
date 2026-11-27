---
topic: outbound-proxy-deploy-and-cli
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 8a29551cc5cac4dd
  last_run: '2026-11-27'
  phases:
    structure:
      status: passed
    completeness:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
    alignment:
      status: passed
  findings: []
---
# Standalone repository and single-container outbound proxy

**Status:** Operator-approved scope, 2026-11-27.

## Objective

Install one Linux container from one multi-platform image on a computer running Docker. Serve a local browser panel and provide an operator-enabled HTTP proxy to the selected LAN.

## Desired Outcomes

- Own the complete service, CLI, panel, documentation, installers, native templates and tests in the outbound-proxy repository. Maintain product knowledge in its own iwiki domain.
- Install on Docker Engine for Linux and Docker Desktop in Linux-container mode for Windows and macOS. The host runs Docker and terminal install or removal scripts. Both the panel HTTP server and forwarding processes run inside the container.
- Open the host-local panel without a pairing code. Manage HTTPS upstreams, selection, checks, forwarding and optional failover. Permit intended LAN and Docker clients to use forwarding without granting management access.
- Build an installable AMD64 and ARM64 image for each eligible PR into test. Keep test and stable releases distinguishable. Store settings independently of the image.

## Health Metrics

Open the panel on the host and proxy a fresh HTTPS request from another PC. Verify denial of LAN and peer-container management access. Install a PR image by digest on a clean Docker host. Recreate the container and verify settings survive. Run the transferred suite from an independent checkout.

## Strategic Context

- Interacts with: Docker Engine, Docker Desktop, GitHub Actions, GHCR and the outbound-proxy iwiki domain.
- Priority trade-off: Prefer one container and installation-time host port mapping over a permanently running host helper. The existing native MiniPC installation continues to serve traffic during extraction.

## Constraints

### Steering

Preserve the service README at project root and keep documentation, scripts and tests distinct.

### Hard

Keep credentials out of logs, image layers and PR artifacts. Restrict management to the host. Enable LAN exposure and failover only by operator action. Preserve certificate and hostname validation and the implicit HTTPS upstream port of 443.

## Autonomy Zones

- Full: Prepare the transfer, delivery design and isolated tests.
- Guarded: Verify candidate versions on available test hosts.
- Proposal-first: Change live MiniPC ownership or publish packages publicly.
- No autonomy: Discard the sole working catalog or expose the control API to LAN clients.

## Stop Rules

Stop deployment when access boundaries or recovery checks fail. Done when the independent repository passes its suite and the digest selected by its installer passes platform and isolation checks.
