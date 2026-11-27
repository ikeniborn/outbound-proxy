---
topic: outbound-proxy-deploy-and-cli
chain:
  intent: docs/superpowers/intents/2026-11-27-outbound-proxy-deploy-and-cli-intent.md
  spec: docs/superpowers/specs/2026-11-27-outbound-proxy-deploy-and-cli-design.md
review:
  plan_hash: 0cd8feeafd4d909a
  last_run: '2026-11-27'
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    dependencies:
      status: passed
    verifiability:
      status: passed
    consistency:
      status: passed
  findings: []
---
# Independent outbound proxy repository and container delivery plan

**Status:** Proposed implementation sequence. No migration or publication has run.

**Goal:** Install the complete service from ikeniborn/outbound-proxy with one multi-platform image, one running container and a locally accessible control page.

**Architecture:** Docker Engine or Docker Desktop runs the service. Compose and short terminal scripts configure host port mappings and network policy. The proxy catalog persists in a volume.

**Tech stack:** Python, Tinyproxy, stunnel, Debian slim, Docker Compose, Buildx, GitHub Actions and GitHub Container Registry.

**Spec:** [Single-container delivery design](../specs/2026-11-27-outbound-proxy-deploy-and-cli-design.md).

## Global constraints

Keep ft4 live during extraction. Retain local-only management, HTTPS upstream verification, explicit forwarding and failover controls. Keep product wiki writes in outbound-proxy. Select the immutable image digest for actual installation.

## 1. Import the entire service (R1)

- [ ] Inventory the MiniPC Git revision and uncommitted changes under docs/services/outbound-proxy/, scripts/outbound_proxy/, scripts/outbound-proxy and scripts/minipc_proxy_apply.py. Follow imports, template discovery, shell entry points, test fixtures and runbook links. Identify MiniPC-specific callers separately. Exclude generated caches, private state and credentials.
- [ ] Move the service README to the new project root. Bring over docs/, scripts/ (including native and legacy definitions) and tests/. Bring the Python package, executable launcher and required shared implementation into appropriate project paths. Preserve license notices and record the source revision and local-diff fingerprint. Replace repository-relative imports, four-level parent lookups and old install paths.
- [ ] Verify from a clean destination checkout with the source repository unavailable. Run the complete transferred service suite, including browser tests. Run the installer from the published tree in an isolated host. Reconcile imported file hashes or document the layout-specific differences. Update remaining source-repository callers or point them to a pinned release before retiring the old product copy. Any live MiniPC change is a separate checkpoint.

## 2. Transfer product documentation (R1)

- [ ] Inventory existing MiniPC product wiki pages, sections, chain documents, task history and backlinks. Read their current revisions. Migrate reusable guidance into outbound-proxy. Preserve history and source attribution and link the original records. Keep host inventory and actual MiniPC cutover evidence distinguishable from portable product instructions.
- [ ] Make the target runbook cover install, update, uninstall, backup, rollback, network scope and image channels. Verify the target binding reports session and primary outbound-proxy, check page retrieval and links, and run wiki lint. Retire old operational instructions only after their replacements can be found. Keep a redirect or explicit handoff where the store supports it.

## 3. Build and verify the container (R2)

- [ ] Add Dockerfile, compose.yaml and the container runtime entry point. Reuse the catalog, operations, web assets, TLS checks and activation rollback. Persist state in a named volume. Verify one service in Compose and one service container at runtime; stop forwarding and confirm the management page still responds.
- [ ] Configure distinct management and forwarding network bindings. Publish the UI at 127.0.0.1:18890 only. Add terminal install, reconfigure and removal scripts for PowerShell and POSIX shells. Show the deployment port in the panel. Use short OS-specific setup steps for firewall rules; the panel receives no host execution or Docker management authority.
- [ ] Confirm the operator accepts port and subnet changes through the deployment scripts. Test failed port changes, firewall rollback and restart. Verify the optional Docker inventory path with the feature on and off. Inspect the effective process identities and daemon access.
- [ ] Build linux/amd64 and linux/arm64 images under one OCI index using Buildx. Verify both manifest entries, run a TLS forwarding smoke test and reopen the same catalog after replacement.

## 4. Establish master and test (R3)

- [ ] Initialize test from the verified master baseline by authorized repository operation. Route feature PRs into test and promotion PRs from test into master. Require passing checks on both. Bootstrap the trusted publishing workflow on the default branch through review before relying on PR previews.
- [ ] Verify both remote heads and branch policies, and check the PR event base filter. The test image channel can move only after checks on the integrated test revision. PR previews cannot move test or stable.

## 5. Build a downloadable image on PR to test (R3)

- [ ] Add .github/workflows/pr-image.yml to test the proposed revision and build the multi-platform OCI artifact on pull_request targeting test. Identify the exact head or synthetic merge revision actually tested. Include that revision in image labels, artifact metadata and the check summary.
- [ ] Add a trusted publish workflow for successful eligible runs. Validate repository, target branch, source repository, workflow identity, PR association, tested revision and immutable artifact identity before upload. Run no code from the PR in the publishing job. For same-repository PRs publish ghcr.io/ikeniborn/outbound-proxy:pr-<number>-<revision> and return the full index digest. For external forks keep the downloadable OCI artifact and read-only permissions.
- [ ] On a passing push to test publish the integration revision and test tag. On approved stable release publish the versioned tag for the qualified content and record its digest. Before enabling external publication, confirm package visibility and intended readers with the repository owner.
- [ ] Open a test PR during implementation. On a clean host pull the exact GHCR digest from the checks before merge. Verify that a newer PR revision leaves the recorded digest reproducible, and a failed build or a fork cannot update the test or stable channels.

## 6. Install the chosen revision and accept platforms (R2, R3)

- [ ] Ship version-matched installation scripts. Allow a PR preview digest, integration version or stable release. Resolve a channel to its image digest before startup and display that digest in status. On pull failure preserve the previous instance and catalog. Verify update, rollback, removal twice and reinstall without rebuilding from source.
- [ ] On Linux Engine, Windows Desktop and macOS Desktop verify host-local management from a fresh browser, HTTPS forwarding from a second authorized LAN PC, rejection from another subnet and management denial from the LAN and both Docker networks, including host-gateway and proxy paths. Record each platform and CPU architecture actually tested.
- [ ] Check DNS and certificate failures, occupied ports, failed activation and Docker restarts. Publish actual results and pending platform evidence in project iwiki. If a platform cannot enforce the access boundary, withhold LAN availability there until verified.

## Review boundary

This plan moves source ownership and defines delivery. Production migration, modifying remote branch policies, triggering publication and package visibility changes are execution actions. MiniPC remains on its existing native service until a separate operator cutover.
