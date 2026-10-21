#!/bin/sh
# Bound one of this repository's BuildKit cache mounts on the CI Docker host.
# Ported from sovereign-config's scripts/ci-prune-buildkit-cache.sh.
#
# The image build keeps two BuildKit cache mounts (Dockerfile):
# `code-vitals-cargo-registry` for the tokei build and `code-vitals-uv-cache`
# for Python wheels. Neither tool reaps entries for versions it no longer
# installs, so the mounts only grow — and nothing else on the host bounds them.
#
# This script measures only the named cache mount and removes it once it exceeds
# CI_BUILDKIT_CACHE_MAX_MIB, so the next image build starts cold. It never uses
# `--max-used-space`: that limit is measured against the whole builder, which
# other repositories share on the CI host, so their cache would decide when
# this repository's is deleted. Matching on the mount id in the record's
# description touches this repository's cache mount and nothing else.
#
# A failed measurement or prune never fails the build: the cache is an
# optimisation, and BuildKit rebuilds whatever is missing.
#
# Usage: ci-prune-buildkit-cache.sh
#        CI_BUILDKIT_CACHE_MAX_MIB  ceiling in MiB (default 4096)
#        CI_BUILDKIT_CACHE_ID       cache mount id (default code-vitals-uv-cache)
#        BUILDX_BUILDER             builder to inspect (buildx's own default otherwise)
set -eu

DEFAULT_MAX_MIB=4096
DEFAULT_CACHE_ID=code-vitals-uv-cache

max_mib="${CI_BUILDKIT_CACHE_MAX_MIB:-$DEFAULT_MAX_MIB}"
cache_id="${CI_BUILDKIT_CACHE_ID:-$DEFAULT_CACHE_ID}"

case "$max_mib" in
  '' | *[!0-9]*)
    echo "ci-prune-buildkit-cache: CI_BUILDKIT_CACHE_MAX_MIB must be a whole number of MiB, got '$max_mib'" >&2
    exit 2
    ;;
esac

case "$cache_id" in
  '' | *[!A-Za-z0-9._-]*)
    echo "ci-prune-buildkit-cache: CI_BUILDKIT_CACHE_ID must be a plain cache id, got '$cache_id'" >&2
    exit 2
    ;;
esac

# The description of a cache-mount record ends `with id "/<id>"`. The filter is
# a containerd filter expression, where `/` and `"` start quoted literals, so it
# is written unquoted: the id followed by one character that cannot continue an
# id (in practice the closing quote), so a longer id that merely starts with this
# one is not matched.
filter="description~=${cache_id}[^0-9A-Za-z._-]"

report=$(docker buildx du --verbose --filter "$filter" 2>/dev/null) || {
  echo "ci-prune-buildkit-cache: could not measure the $cache_id cache mount — keeping cache"
  exit 0
}

# `Size:` values use Docker's decimal units (B, kB, MB, GB, TB).
used_mib=$(printf '%s\n' "$report" | awk '
  /^Size:/ {
    value = $2
    unit = value
    sub(/^[0-9.]+/, "", unit)
    sub(/[A-Za-z]+$/, "", value)
    if (value == "") { bad = 1; next }
    if (unit == "B") factor = 1
    else if (unit == "kB") factor = 1000
    else if (unit == "MB") factor = 1000 ^ 2
    else if (unit == "GB") factor = 1000 ^ 3
    else if (unit == "TB") factor = 1000 ^ 4
    else { bad = 1; next }
    bytes += value * factor
  }
  END {
    if (bad) print "unparseable"
    else printf "%d\n", bytes / (1024 * 1024)
  }
')

case "$used_mib" in
  '' | *[!0-9]*)
    echo "ci-prune-buildkit-cache: could not parse the size of the $cache_id cache mount — keeping cache"
    exit 0
    ;;
esac

if [ "$used_mib" -le "$max_mib" ]; then
  echo "ci-prune-buildkit-cache: $cache_id is $used_mib MiB, ceiling $max_mib MiB — keeping cache"
  exit 0
fi

echo "ci-prune-buildkit-cache: $cache_id is $used_mib MiB, over the $max_mib MiB ceiling — pruning"

if docker buildx prune --force --filter "$filter" >/dev/null 2>&1; then
  echo "ci-prune-buildkit-cache: $cache_id pruned, next image build will compile cold"
else
  echo "ci-prune-buildkit-cache: pruning $cache_id failed — keeping whatever remains"
fi
