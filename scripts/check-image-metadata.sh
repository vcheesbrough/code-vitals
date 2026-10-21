#!/bin/sh
# Assert required OCI image labels on the code-vitals collector image.
# Static labels come from the Dockerfile; dynamic labels (version, revision,
# created) are injected via `docker build --label` in .woodpecker/build.yml.
set -eu

IMAGE="${1:?usage: check-image-metadata.sh <image-ref> [expected-version] [expected-revision]}"
EXPECTED_VERSION="${2:-}"
EXPECTED_REVISION="${3:-}"

label() {
  docker image inspect --format "{{ index .Config.Labels \"$1\" }}" "$IMAGE" 2>/dev/null || true
}

require_label() {
  key="$1"
  value=$(label "$key")
  if [ -z "$value" ]; then
    echo "ERROR: missing label $key on $IMAGE" >&2
    exit 1
  fi
  echo "  $key=$value"
}

echo "==> OCI labels on $IMAGE"
require_label org.opencontainers.image.title
require_label org.opencontainers.image.description
require_label org.opencontainers.image.licenses
require_label org.opencontainers.image.source
require_label org.opencontainers.image.url
require_label org.opencontainers.image.documentation
require_label org.opencontainers.image.authors
require_label org.opencontainers.image.vendor
require_label org.opencontainers.image.base.name
require_label org.opencontainers.image.base.digest

VERSION=$(label org.opencontainers.image.version)
REVISION=$(label org.opencontainers.image.revision)
CREATED=$(label org.opencontainers.image.created)

if [ -z "$VERSION" ]; then
  echo "ERROR: missing org.opencontainers.image.version" >&2
  exit 1
fi
if [ -z "$REVISION" ]; then
  echo "ERROR: missing org.opencontainers.image.revision" >&2
  exit 1
fi
if [ -z "$CREATED" ]; then
  echo "ERROR: missing org.opencontainers.image.created" >&2
  exit 1
fi

echo "  org.opencontainers.image.version=$VERSION"
echo "  org.opencontainers.image.revision=$REVISION"
echo "  org.opencontainers.image.created=$CREATED"

if [ -n "$EXPECTED_VERSION" ] && [ "$VERSION" != "$EXPECTED_VERSION" ]; then
  echo "ERROR: version label '$VERSION' != expected '$EXPECTED_VERSION'" >&2
  exit 1
fi

if [ -n "$EXPECTED_REVISION" ] && [ "$REVISION" != "$EXPECTED_REVISION" ]; then
  echo "ERROR: revision label '$REVISION' != expected '$EXPECTED_REVISION'" >&2
  exit 1
fi

echo "==> OK"
