# Image channels

Install from `ghcr.io/ikeniborn/outbound-proxy@sha256:<index-digest>`. Copy the full reference from the **Publish verified image** run summary. The digest identifies the same OCI index on Linux AMD64 and ARM64. Use installation scripts from the Git revision in that summary.

| Source | Checks and delivery |
|---|---|
| PR into `test` | **Verify image** checks the exact PR head, runs the service suite and builds both architectures. The run lists the Git revision and OCI index digest. |
| Same-repository PR into `test` | A successful eligible run publishes `pr-<number>-<full-head-sha>`. The **GHCR preview** commit status shows the index digest and links to the publisher. |
| Fork PR into `test` | Download the `oci-<run>-<attempt>-<head-sha>` artifact from the build run. It contains `image.oci.tar`. Sign in to GitHub with repository read access. Artifacts expire after 30 days. |
| Push to `test` | The integrated commit runs the checks again. A successful build for the current branch head publishes `test-<full-sha>` and advances `test`. |
| Promotion from `test` into `master` | The promotion PR runs the checks. The subsequent push to `master` builds and retains a qualified release artifact. |
| Approved release | The publisher uses the successful `master` artifact for the tagged promotion commit. It publishes the version tag. The GitHub release marked latest also advances `stable`. |

Select a PR check in GitHub and open its **Details** link. Copy the index digest to pull the reviewed image before merge. For a private package, sign in to GHCR first using an account with package read access.

```bash
read -r -p 'OCI index digest (sha256:...): ' image_digest
docker pull "ghcr.io/ikeniborn/outbound-proxy@$image_digest"
```

For a fork, open **Actions → Verify image → Artifacts** and download the ZIP. Extract it to a new directory. On a Linux AMD64 host, import the local platform for inspection or testing with Skopeo.

```bash
skopeo --override-os linux --override-arch amd64 copy oci-archive:image.oci.tar docker-daemon:outbound-proxy:review
```

Use `arm64` on an ARM64 host. A fork artifact contains contributor code. Run it on an isolated test machine.

## Bootstrap the trusted publisher

1. Review both workflow files and their publishing helpers through the initial promotion into the default branch, `master`. Synchronize `test` with that baseline. GitHub loads the downstream `workflow_run` workflow from the default branch.
2. Protect `test` and `master`. Require the **tests**, **Windows installer** and **verify** jobs on each branch and reviewed PRs for promotion into `master`. Restrict release-tag creation and changes to the release maintainers.
3. Keep fork PR tokens read-only. Keep fork secrets disabled. Use forks for contributors without repository write authority. Reserve same-repository branches for trusted collaborators.
4. Create the `ghcr-images` and `stable-release` environments. Restrict deployments in both environments to `master`. Configure required reviewers on `stable-release`.
5. Confirm the package visibility, intended readers and the repository's package write access with the owner. For an existing package, grant this repository Actions access in its package settings. Set the repository Actions variable `GHCR_PUBLISH_ENABLED` to `true` only after approval.
6. Open a feature PR into `test`. Compare its checked head, publisher summary and downloaded digest. Repeat with a new revision and pull the earlier digest to verify retention.

The publisher validates the repository, workflow ID, workflow file, event, PR association, commit, run attempt, artifact ID and GitHub archive checksum. It checks both platform configurations and layer digests before copying the local OCI layout into GHCR. It runs the publishing helpers from `master` and treats the artifact as data. Changes to the producer workflow require review and promotion onto `master` before previews using the new rules can publish.

## Release a qualified commit

1. Merge the reviewed `test` promotion into `master`. Wait for **Verify image** on the resulting commit.
2. Publish a GitHub release for a protected `vX.Y.Z` tag at that merge commit. Choose its latest-release status.
3. Open **Actions → Publish verified image → Run workflow**. Select `master` and enter the published tag.
4. Approve the `stable-release` deployment. Check the publisher summary for the commit and exact install reference.

Use the artifact during its 30-day retention period. If it has expired, rerun **Verify image** for the same `master` push and review the new attempt and digest. A revision or release tag already assigned to different content causes publication to fail. Roll back an installation by selecting its previously recorded digest and matching scripts. Keep the referenced package version in GHCR while that rollback is needed.
