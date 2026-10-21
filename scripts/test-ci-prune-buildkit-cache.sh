#!/bin/sh
# Tests for scripts/ci-prune-buildkit-cache.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH answers `buildx du` with the report in
# $WORK_DIR/du (or fails when $WORK_DIR/du-fails exists) and records every
# call, so a test can tell whether a prune happened and with which filter.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/ci-prune-buildkit-cache.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
if [ "\$1 \$2" = "buildx du" ]; then
    [ -f "$WORK_DIR/du-fails" ] && exit 1
    cat "$WORK_DIR/du"
fi
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# run <max-mib> <du report> — fresh call log; the script's exit status is
# returned and its output kept in $WORK_DIR/out.
run() {
    rm -f "$WORK_DIR/du-fails"
    : > "$WORK_DIR/docker-calls"
    printf '%s\n' "$2" > "$WORK_DIR/du"
    PATH="$WORK_DIR/bin:$PATH" CI_BUILDKIT_CACHE_MAX_MIB="$1" CI_BUILDKIT_CACHE_ID=code-vitals-uv-cache \
        sh "$UNDER_TEST" > "$WORK_DIR/out" 2>&1
}

pruned() { grep -q "^buildx prune" "$WORK_DIR/docker-calls"; }

# Two records of the mount, in different units: 600 MB + 0.5 GB = 1.1e9 bytes
# = 1049 MiB.
REPORT="ID: a
Description: mount / from exec /bin/sh -c uv sync with id \"/code-vitals-uv-cache\"
Size: 600MB

ID: b
Description: mount / from exec /bin/sh -c uv sync with id \"/code-vitals-uv-cache\"
Size: 0.5GB"

if run 2048 "$REPORT" && ! pruned && grep -q "is 1049 MiB, ceiling 2048 MiB — keeping cache" "$WORK_DIR/out"; then
    pass "under the ceiling: sizes summed across units, cache kept"
else
    fail "under the ceiling: $(cat "$WORK_DIR/out") / calls: $(cat "$WORK_DIR/docker-calls")"
fi

if run 1000 "$REPORT" && pruned && grep -q "pruned" "$WORK_DIR/out"; then
    pass "over the ceiling: cache pruned"
else
    fail "over the ceiling: $(cat "$WORK_DIR/out") / calls: $(cat "$WORK_DIR/docker-calls")"
fi

# The filter must name this mount and nothing that merely starts with it.
if grep -q 'buildx prune --force --filter description~=code-vitals-uv-cache\[^0-9A-Za-z._-\]' "$WORK_DIR/docker-calls"; then
    pass "prune and measure use the mount-id filter"
else
    fail "prune filter: calls: $(cat "$WORK_DIR/docker-calls")"
fi

if run 1000 "Size: 12XB" && ! pruned && grep -q "could not parse" "$WORK_DIR/out"; then
    pass "unparseable size: cache kept, build not failed"
else
    fail "unparseable size: $(cat "$WORK_DIR/out")"
fi

: > "$WORK_DIR/docker-calls"
touch "$WORK_DIR/du-fails"
if PATH="$WORK_DIR/bin:$PATH" sh "$UNDER_TEST" > "$WORK_DIR/out" 2>&1 && ! pruned \
    && grep -q "could not measure" "$WORK_DIR/out"; then
    pass "measurement failure: cache kept, build not failed"
else
    fail "measurement failure: $(cat "$WORK_DIR/out")"
fi
rm -f "$WORK_DIR/du-fails"

if PATH="$WORK_DIR/bin:$PATH" CI_BUILDKIT_CACHE_MAX_MIB=lots sh "$UNDER_TEST" > /dev/null 2>&1; then
    fail "non-numeric ceiling: expected a refusal"
else
    pass "non-numeric ceiling: refused"
fi

if PATH="$WORK_DIR/bin:$PATH" CI_BUILDKIT_CACHE_ID='a b' sh "$UNDER_TEST" > /dev/null 2>&1; then
    fail "unsafe cache id: expected a refusal"
else
    pass "unsafe cache id: refused"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all ci-prune-buildkit-cache tests passed"
