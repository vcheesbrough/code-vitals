#!/bin/sh
# Assert that the collector image runs and carries the measuring tools at their
# pinned versions, and that its build identity matches what was built.
#
#   - tokei at TOKEI_VERSION from tool-versions.env
#   - lizard at the exact `lizard==` pin in pyproject.toml
#   - a working git
#   - `code-vitals --version` reports <expected-version> (<expected-revision>)
#
# Usage: check-image-tools.sh <image-ref> <expected-version> <expected-revision>
# Run from the repository root; needs a docker holding <image-ref>.
set -eu

IMAGE="${1:?usage: check-image-tools.sh <image-ref> <expected-version> <expected-revision>}"
EXPECTED_VERSION="${2:?usage: check-image-tools.sh <image-ref> <expected-version> <expected-revision>}"
EXPECTED_REVISION="${3:?usage: check-image-tools.sh <image-ref> <expected-version> <expected-revision>}"

tokei_version=$(sed -n 's/^TOKEI_VERSION=//p' tool-versions.env)
lizard_version=$(sed -n 's/^[[:space:]]*"lizard==\([^"]*\)".*/\1/p' pyproject.toml)
if [ -z "$tokei_version" ] || [ -z "$lizard_version" ]; then
  echo "ERROR: could not read the pinned tokei/lizard versions" >&2
  exit 1
fi

failures=0
check() {
  name="$1"
  expected="$2"
  actual="$3"
  case "$actual" in
    *"$expected"*) echo "  ok   $name: $actual" ;;
    *)
      echo "  FAIL $name: expected '$expected' in '$actual'" >&2
      failures=$((failures + 1))
      ;;
  esac
}

run_in_image() {
  docker run --rm --entrypoint "$1" "$IMAGE" "$2" 2>&1 || true
}

echo "==> tools in $IMAGE"
check tokei "tokei $tokei_version" "$(run_in_image tokei --version)"
check lizard "$lizard_version" "$(run_in_image lizard --version)"
check git "git version" "$(run_in_image git --version)"

version_out=$(docker run --rm "$IMAGE" --version 2>&1 || true)
check "code-vitals --version" "code-vitals $EXPECTED_VERSION ($EXPECTED_REVISION)" "$version_out"

if [ "$failures" -ne 0 ]; then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "==> OK"
