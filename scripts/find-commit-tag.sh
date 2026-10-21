#!/bin/sh
# On a deployment, looks up the semver git tag already on CI_COMMIT_SHA and
# writes what resolve-deploy-tag.sh reads. Ported from bored (bored card #461).
#
#   - tagged (deployed before):  .release-tag = that tag, .release-tag-reused = true
#   - untagged (never deployed): .release-tag empty,      .release-tag-reused = false
#
# The release-versions plugin's `compute` mode did this lookup before, but on
# an untagged commit it also "allocates" max(tag)+1 and logs it
# ("release tag allocated: 1.69.2"), a version no build ever has under
# pipeline-number patches. This logs only the real answer.
#
# Both tag forms are matched: an annotated tag's commit is on its peeled
# `refs/tags/<t>^{}` line, a lightweight tag's on its own line. Only plain
# MAJOR.MINOR.PATCH tags count (legacy `v1.x` tags are ignored). More than one
# on the commit is refused: a commit runs under one version.
#
# Usage: find-commit-tag.sh
# Reads CI_COMMIT_SHA, CI_REPO (owner/name), GITHUB_TOKEN; needs git.

set -eu

sha="${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}"
repo="${CI_REPO:?CI_REPO is required}"
token="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"

listing="$(git ls-remote --tags "https://x-access-token:$token@github.com/$repo.git")" || {
    echo "ERROR: could not list git tags of $repo" >&2
    exit 1
}

# "<sha>\trefs/tags/<name>" or "<sha>\trefs/tags/<name>^{}" → names on $sha.
tags="$(
    printf '%s\n' "$listing" \
        | awk -v sha="$sha" '$1 == sha { sub(/^refs\/tags\//, "", $2); sub(/\^\{\}$/, "", $2); print $2 }' \
        | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -u
)" || true

count="$(printf '%s' "$tags" | grep -c . || true)"

case "$count" in
    0)
        : > .release-tag
        echo false > .release-tag-reused
        echo "commit $sha has no release tag: it has not been deployed before"
        ;;
    1)
        echo "$tags" > .release-tag
        echo true > .release-tag-reused
        echo "commit $sha is tagged $tags: it was deployed before as that version"
        ;;
    *)
        echo "ERROR: commit $sha carries several release tags: $(printf '%s\n' "$tags" | tr '\n' ' ')" >&2
        echo "A commit must run under one version; delete all but the one that names its deployed build." >&2
        exit 1
        ;;
esac
