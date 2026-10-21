#!/bin/sh
# Pushes the image this push pipeline built and tested, under its version and
# as the commit's latest green build. Ported from bored's
# scripts/publish-release-image.sh (bored card #461).
#
#   1. Refuse if the registry already holds the version. Versions are
#      MAJOR.MINOR.<pipeline number>, unique only while Woodpecker's pipeline
#      counter is: if the repo is re-created in Woodpecker or its DB restored
#      from an old backup, numbers repeat, and pushing would silently replace
#      an image prod or a git tag may name. Only the registry's explicit
#      "no such manifest" / "manifest unknown" lets the push go ahead (zot
#      answers the former). Any other probe failure (auth, network, 5xx, a
#      credential helper "executable file not found") refuses, since it
#      cannot rule the version out. bored also accepts a bare "not found";
#      that is narrowed here because it matches those other failures too.
#   2. Push :<version>.
#   3. Push the same image as :commit-<sha>, which a deployment of a commit
#      never deployed before resolves to. A re-run of the commit's push
#      pipeline moves it to the newer build.
#
# Usage: publish-release-image.sh <version> <commit-sha>
# Reads IMAGE_REPO (default registry.desync.link/code-vitals-collector); needs
# a logged-in docker holding the built <repo>:<version> image.

set -eu

version="${1:?usage: publish-release-image.sh <version> <commit-sha>}"
sha="${2:?usage: publish-release-image.sh <version> <commit-sha>}"
image_repo="${IMAGE_REPO:-registry.desync.link/code-vitals-collector}"

image="$image_repo:$version"
commit_image="$image_repo:commit-$sha"

if probe="$(docker manifest inspect "$image" 2>&1 >/dev/null)"; then
    echo "ERROR: $image already exists in the registry; Woodpecker's pipeline numbers have repeated." >&2
    echo "Refusing to overwrite it. Start the next iteration — its new minor (AGENTS.md: minor = iteration) is a" >&2
    echo "fresh version line — rather than bumping the minor mid-iteration." >&2
    exit 1
fi
case "$probe" in
    *"no such manifest"* | *"manifest unknown"*) ;;
    *)
        echo "ERROR: could not tell whether $image already exists: $probe" >&2
        echo "Refusing to push over a version that may exist; re-run publish-image once the registry answers." >&2
        exit 1
        ;;
esac

docker push "$image"
docker tag "$image" "$commit_image"
docker push "$commit_image"
