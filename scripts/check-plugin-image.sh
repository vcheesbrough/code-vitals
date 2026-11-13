#!/bin/sh
# Assert that the code-vitals-plugin image is the plugin of the expected build
# (card #564):
#   - its entrypoint is `code-vitals plugin`, run as root (the docker socket is
#     root's on the host);
#   - the docker CLI it drives the collector with runs;
#   - it is built on the collector of the same version, which is the one it
#     starts;
#   - a step without settings fails with a clear message, before docker.
#
# Usage: check-plugin-image.sh <image-ref> <expected-version>
set -eu

IMAGE="${1:?usage: check-plugin-image.sh <image-ref> <expected-version>}"
VERSION="${2:?usage: check-plugin-image.sh <image-ref> <expected-version>}"

echo "==> plugin $IMAGE"
entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$IMAGE")
if [ "$entrypoint" != '["code-vitals","plugin"]' ]; then
  echo "ERROR: entrypoint is $entrypoint, not code-vitals plugin" >&2
  exit 1
fi
user=$(docker image inspect -f '{{.Config.User}}' "$IMAGE")
if [ "$user" != root ]; then
  echo "ERROR: runs as '$user', not root: it could not use the docker socket" >&2
  exit 1
fi
echo "  entrypoint $entrypoint, user $user"

docker run --rm --entrypoint docker "$IMAGE" --version

version=$(docker run --rm --entrypoint code-vitals "$IMAGE" --version)
case "$version" in
  "code-vitals $VERSION "*) echo "  $version" ;;
  *)
    echo "ERROR: '$version' is not code-vitals $VERSION" >&2
    exit 1
    ;;
esac
started=$(docker run --rm --entrypoint python "$IMAGE" -c \
  'from code_vitals import plugin; from code_vitals.build_info import load_build_info; print(plugin.collector_image(load_build_info()))')
if [ "$started" != "registry.desync.link/code-vitals-collector:$VERSION" ]; then
  echo "ERROR: it would start $started" >&2
  exit 1
fi
echo "  starts $started"

status=0
out=$(docker run --rm "$IMAGE" 2>&1) || status=$?
if [ "$status" -ne 2 ] || ! printf '%s' "$out" | grep -q "code-vitals plugin: invalid step: trunk must be a branch name"; then
  echo "ERROR: a step without settings did not fail as expected (exit $status): $out" >&2
  exit 1
fi
echo "  refuses a step without settings"
echo "==> OK"
