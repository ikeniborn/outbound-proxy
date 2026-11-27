#!/usr/bin/env bash
# Select the producing run and artifact exclusively from GitHub's event and REST metadata.
set -euo pipefail

repo="repos/$GITHUB_REPOSITORY"
event="$GITHUB_EVENT_PATH"
workflow='pr-image.yml'
workflow_id=$(gh api "$repo/actions/workflows/$workflow" --jq '.id')

case "$GITHUB_EVENT_NAME" in
  workflow_run)
    run_id=$(jq -er '.workflow_run.id' "$event")
    attempt=$(jq -er '.workflow_run.run_attempt' "$event")
    ;;
  workflow_dispatch)
    tag=$(jq -er '.inputs.version' "$event")
    [[ $tag =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
    gh api "$repo/releases/tags/$tag" | jq -e --arg tag "$tag" \
      '.tag_name == $tag and (.draft == false) and (.prerelease == false)' > /dev/null
    release_sha=$(gh api "$repo/commits/$tag" --jq '.sha')
    [[ $release_sha =~ ^[0-9a-f]{40}$ ]]
    gh api "$repo/commits/$release_sha/pulls" | jq -e --arg repo "$GITHUB_REPOSITORY" --arg rev "$release_sha" \
      'any(.[]; .merged_at and .merge_commit_sha == $rev and .head.repo.full_name == $repo and
           .head.ref == "test" and .base.repo.full_name == $repo and .base.ref == "master")' > /dev/null
    run_id=$(gh api "$repo/actions/workflows/$workflow/runs?event=push&branch=master&status=success&head_sha=$release_sha&per_page=1" \
      | jq -er '.workflow_runs[0].id')
    attempt=$(gh api "$repo/actions/runs/$run_id" --jq '.run_attempt')
    ;;
  *) exit 1 ;;
esac

run=$(gh api "$repo/actions/runs/$run_id/attempts/$attempt")
jq -e --arg repo "$GITHUB_REPOSITORY" --argjson id "$workflow_id" --argjson run "$run_id" --argjson attempt "$attempt" \
  '.workflow_id == $id and .path == ".github/workflows/pr-image.yml" and .id == $run and .run_attempt == $attempt
   and .conclusion == "success" and .status == "completed" and .repository.full_name == $repo
   and .head_repository.full_name == $repo' <<< "$run" > /dev/null
rev=$(jq -er '.head_sha' <<< "$run")
[[ $rev =~ ^[0-9a-f]{40}$ ]]

producer_blob() {
  gh api "$repo/contents/.github/workflows/$workflow?ref=$1" --jq '.sha'
}
trusted_blob=$(producer_blob "$GITHUB_SHA")
[[ $(producer_blob "$rev") == "$trusted_blob" ]]

case "$GITHUB_EVENT_NAME:$(jq -r '.event' <<< "$run")" in
  workflow_run:pull_request)
    pr=$(jq -er --arg rev "$rev" '.pull_requests | select(length == 1) | .[0]
      | select(.base.ref == "test" and .head.sha == $rev) | .number' <<< "$run")
    [[ $pr =~ ^[1-9][0-9]*$ ]]
    pull=$(gh api "$repo/pulls/$pr")
    jq -e --arg repo "$GITHUB_REPOSITORY" --arg rev "$rev" \
      '.state == "open" and .base.repo.full_name == $repo and .base.ref == "test"
       and .head.repo.full_name == $repo and .head.sha == $rev' <<< "$pull" > /dev/null
    base_sha=$(jq -er '.pull_requests[0].base.sha' <<< "$run")
    [[ $(producer_blob "$base_sha") == "$trusted_blob" ]]
    tag="pr-$pr-$rev"
    ;;
  workflow_run:push)
    [[ $(jq -r '.head_branch' <<< "$run") == test ]]
    [[ $(gh api "$repo/git/ref/heads/test" --jq '.object.sha') == "$rev" ]]
    tag="test-$rev"
    ;;
  workflow_dispatch:push)
    [[ $(jq -r '.head_branch' <<< "$run") == master && $rev == "$release_sha" ]]
    ;;
  *) exit 1 ;;
esac

# Run attempts share an artifact namespace. Include the attempt and commit in the exact name.
artifact_name="oci-$run_id-$attempt-$rev"
artifact=$(gh api "$repo/actions/runs/$run_id/artifacts?name=$artifact_name" | jq -ce \
  --arg name "$artifact_name" --argjson run "$run_id" --arg rev "$rev" --argjson repo_id "$GITHUB_REPOSITORY_ID" \
  '[.artifacts[] | select(.name == $name and (.expired == false) and .workflow_run.id == $run
      and .workflow_run.head_sha == $rev and .workflow_run.repository_id == $repo_id
      and .workflow_run.head_repository_id == $repo_id)] | select(length == 1) | .[0]')
artifact_id=$(jq -er '.id' <<< "$artifact")
checksum=$(jq -er '.digest' <<< "$artifact")
[[ $artifact_id =~ ^[0-9]+$ && $checksum =~ ^sha256:[0-9a-f]{64}$ ]]

cd "$RUNNER_TEMP"
gh api "$repo/actions/artifacts/$artifact_id/zip" > image.zip
printf '%s  image.zip\n' "${checksum#sha256:}" | sha256sum --check --status
[[ $(unzip -Z -1 image.zip) == image.oci.tar ]]
unzip -p image.zip image.oci.tar > candidate.tar
digest=$(python3 -I "$GITHUB_WORKSPACE/.github/workflows/verify_oci.py" candidate.tar layout "$rev")
printf 'tag=%s\nrevision=%s\ndigest=%s\n' "$tag" "$rev" "$digest" >> "$GITHUB_OUTPUT"
printf 'Build run %s, attempt %s, artifact %s, archive digest **%s**\n' \
  "$run_id" "$attempt" "$artifact_id" "$checksum" >> "$GITHUB_STEP_SUMMARY"
