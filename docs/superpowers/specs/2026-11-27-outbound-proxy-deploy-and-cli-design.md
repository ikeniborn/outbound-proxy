---
topic: outbound-proxy-deploy-and-cli
chain:
  intent: docs/superpowers/intents/2026-11-27-outbound-proxy-deploy-and-cli-intent.md
review:
  spec_hash: 78d573aa58b61176
  last_run: '2026-11-27'
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
  findings: []
---
# Single-container delivery design

**Status:** Proposed delivery details under the approved single-container scope.

## R1. Repository and knowledge ownership

Extract the complete native package and its support files from MiniPC, then add the container edition. The import must include working-tree service changes not yet in the source branch. Preserve historical source revisions and identify the source of each imported component. Product runbooks, design decisions, task records and test evidence belong in the outbound-proxy iwiki domain. MiniPC host deployment evidence remains attributable to that host. Verify a fresh independent clone before retiring any source copy.

## R2. Runtime and access

Use one Compose service and one Debian slim image. The entry point supervises the panel and the existing Tinyproxy/stunnel pipeline. Retain CLI operations through the container, private state in a named volume and separate forwarding and panel lifetimes. Serve local HTML and JSON from the panel. Keep a strict Host allowlist, same-origin mutation checks and automatic CSRF sessions in the container edition. The native package retains its own lifecycle and pairing contract.

Publish the panel only on host IPv4 loopback at 18890. Inside the container bind the panel only to its management-network address. Exclude clients from that dedicated network. Bind forwarding only to its designated data interfaces. Publish forwarding on loopback and, when selected, on the specific LAN host address and port (default 18888). The installer applies the selected LAN source subnet at the real host ingress using the packet-filter path appropriate to Engine or Desktop. Disable LAN publication if the policy cannot be enforced. Never infer the client subnet from HTTP headers or a translated gateway address. Enforce Docker-client opt-in independently of LAN access. Verify the actual separation, including Desktop host-gateway paths and attempts to reach management through forwarding.

The host port, LAN address and subnet are deployment settings. The installer or reconfigure script changes these settings and reapplies Compose. The panel displays the effective host endpoint and manages forwarding and upstreams. An occupied replacement port must leave the prior configuration recoverable. Save the last working mapping and firewall configuration and verify both on rollback. Operator acceptance of this port-setting workflow is required before implementation. No permanent host program is installed.

If optional live Docker network inventory is enabled, use an isolated process inside the container with access only through a fixed local inventory operation. Its only query is inspection of the named dedicated network, with projected network and member fields. The nonprivileged panel and forwarding processes must have no daemon access. Treat the daemon socket as a privileged credential even when mounted read-only. Omit the mount by default and display unavailable when inventory cannot be read.

## R3. Delivery channels

Use master for stable and test for integration. Short-lived development branches target test through PRs. A PR build produces a multi-platform OCI index. For trusted, same-repository PRs publish a preview indexed by PR number and tested Git revision in GHCR; expose the registry digest in the PR checks. Publish the test channel only from a passing integration commit. Promote to stable through a reviewed PR into master and a versioned release. Verify the promoted image and revision agree. Keep executable scripts tied to the selected release or reviewed checkout.

Build untrusted PRs with read-only permissions and no registry credentials. Provide downloadable CI artifacts for those builds. Publishing uses a trusted workflow with packages write permission, exact workflow/run/artifact provenance checks, and no execution of PR code with write credentials. Package visibility is an explicit repository-owner choice. Private installs use the operator's read access.

## Acceptance

From the destination checkout, run the service suite and confirm no source dependency on the MiniPC checkout. Verify one running service container on Linux Engine, Windows Desktop and macOS Desktop. Prove local access with forwarding stopped, permitted LAN HTTPS forwarding, rejection of management from LAN and ordinary containers, denied out-of-scope clients, credential redaction, selection rollback, opt-in failover and persistence across recreation. Verify a trusted test PR is installable before merge, a fork cannot update registry tags and the installer resolves the chosen immutable digest. Record unavailable platforms as pending.
