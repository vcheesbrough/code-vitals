#!/bin/sh
# Rewrites .release-tag, on a deployment, to the version of the build being
# deployed for CI_COMMIT_SHA. Ported from bored (bored card #461).
#
# Runs after find-commit-tag.sh, which writes .release-tag and
# .release-tag-reused:
#
#   - reused=true: the commit already carries a semver git tag, because a
#     deploy of it succeeded before. Deploy that same build again, so a commit
#     never runs under two versions and its git tag always names what is
#     deployed (the plugin's push-tag refuses a second tag on one commit).
#   - reused=false: the commit has never been deployed. Builds take the
#     pipeline number as their patch (compute-release-tag.sh), so nothing in
#     git names the build to use; publish-image also pushes each green
#     build as :commit-<sha>, so read the version from that image's label: it
#     is the latest green build of this commit.
#
# Found through the commit, not CI_PIPELINE_PARENT, because a restarted
# deployment and a deployment promoted from another deployment both have a
# deployment pipeline as their parent, not the push pipeline that built the
# image.
#
# Usage: resolve-deploy-tag.sh   (in the workspace; needs a logged-in docker)
# Reads CI_COMMIT_SHA, and IMAGE_REPO (default registry.desync.link/code-vitals-collector).

set -eu

image_repo="${IMAGE_REPO:-registry.desync.link/code-vitals-collector}"
sha="${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}"

reused="$(cat .release-tag-reused 2>/dev/null || true)"

# Fail closed on anything but find-commit-tag.sh's two literal answers: guessing
# "never deployed" for a deployed commit would deploy a different build from
# the one its git tag names, and the conflict would only surface at
# tag-release, after the deploy.
case "$reused" in
    true | false) ;;
    *)
        echo "ERROR: .release-tag-reused is '$reused', expected 'true' or 'false' from find-commit-tag." >&2
        echo "Cannot tell whether commit $sha was deployed before; refusing to guess which build to deploy." >&2
        exit 1
        ;;
esac

if [ "$reused" = true ]; then
    tag="$(cat .release-tag)"
    echo "commit $sha was deployed before as $tag; deploying that build again"
else
    commit_image="$image_repo:commit-$sha"
    if ! docker pull "$commit_image" >/dev/null; then
        echo "ERROR: could not pull $commit_image (docker's error is above)." >&2
        echo "If it is 'not found' / 'manifest unknown', no push pipeline for commit $sha has gone green:" >&2
        echo "push the commit (or re-run its push pipeline) and let build and publish-image pass, then re-run this deployment." >&2
        exit 1
    fi
    tag="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$commit_image")"
    if [ -z "$tag" ] || [ "$tag" = "<no value>" ]; then
        echo "ERROR: $commit_image has no org.opencontainers.image.version label" >&2
        exit 1
    fi
    echo "commit $sha has not been deployed; deploying its latest green build, $tag"
fi

echo "$tag" > .release-tag
