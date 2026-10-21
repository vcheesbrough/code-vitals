#!/bin/sh
# Tests for scripts/resolve-deploy-tag.sh (ported from bored, bored card #461).
#
# Plain POSIX sh with no harness dependency, wired into the Dockerfile like the
# other script tests. A fake `docker` first on PATH stands in for the registry:
# it knows the images listed in $WORK_DIR/registry (one "<ref> <version label>"
# per line), fails `pull` for any other, and records every call. Each case runs
# in its own workspace holding find-commit-tag.sh's .release-tag/.release-tag-reused.
# Expected tags are literal strings.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/resolve-deploy-tag.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
case "\$1" in
    pull)
        grep -q "^\$2 " "$WORK_DIR/registry"
        ;;
    image)
        # image inspect -f <format> <ref>: print the ref's version label.
        ref="\$5"
        grep "^\$ref " "$WORK_DIR/registry" | cut -d' ' -f2-
        ;;
    *)
        exit 2
        ;;
esac
EOF
chmod +x "$WORK_DIR/bin/docker"

SHA=0123456789abcdef0123456789abcdef01234567
REPO=registry.example/code-vitals-collector

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# setup <found-tag> <reused> <registry lines...> — a fresh workspace in $ws.
case_n=0
setup() {
    case_n=$((case_n + 1))
    ws="$WORK_DIR/ws$case_n"
    mkdir -p "$ws"
    echo "$1" > "$ws/.release-tag"
    echo "$2" > "$ws/.release-tag-reused"
    shift 2
    : > "$WORK_DIR/registry"
    : > "$WORK_DIR/docker-calls"
    for line in "$@"; do
        echo "$line" >> "$WORK_DIR/registry"
    done
}

# run — the script in $ws; stdout/stderr to $ws/out, $ws/err.
run() {
    (cd "$ws" && PATH="$WORK_DIR/bin:$PATH" CI_COMMIT_SHA="$SHA" IMAGE_REPO="$REPO" \
        sh "$UNDER_TEST" > "$ws/out" 2> "$ws/err")
}

# --- never deployed: the commit image's version label wins -------------------
# Whatever .release-tag holds when the commit is untagged must be discarded.
setup 1.69.342 false "$REPO:commit-$SHA 1.69.341"
if run && [ "$(cat "$ws/.release-tag")" = "1.69.341" ]; then
    pass "undeployed commit: .release-tag is the commit image's version label"
else
    fail "undeployed commit: got '$(cat "$ws/.release-tag")' / $(cat "$ws/err")"
fi

# --- already deployed: the existing git tag wins -----------------------------
# Even though a later re-run published a newer build as :commit-<sha>, the
# commit must keep running the build its git tag names, and docker is not
# consulted at all.
setup 1.69.339 true "$REPO:commit-$SHA 1.69.352"
if run && [ "$(cat "$ws/.release-tag")" = "1.69.339" ] && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "deployed commit: its existing git tag is kept, registry untouched"
else
    fail "deployed commit: got '$(cat "$ws/.release-tag")', docker calls: $(cat "$WORK_DIR/docker-calls") / $(cat "$ws/err")"
fi

# --- never deployed and no green build: refused ------------------------------
setup 1.69.342 false
if run; then
    fail "no green build: expected a refusal, got '$(cat "$ws/.release-tag")'"
elif grep -q "could not pull $REPO:commit-$SHA" "$ws/err" && grep -q "no push pipeline for commit $SHA has gone green" "$ws/err"; then
    pass "no green build: refused, naming the commit"
else
    fail "no green build: refused, but stderr: $(cat "$ws/err")"
fi

# --- commit image without a version label: refused ---------------------------
setup 1.69.342 false "$REPO:commit-$SHA <no value>"
if run; then
    fail "unlabelled image: expected a refusal, got '$(cat "$ws/.release-tag")'"
elif grep -q "no org.opencontainers.image.version label" "$ws/err"; then
    pass "unlabelled image: refused"
else
    fail "unlabelled image: refused, but stderr: $(cat "$ws/err")"
fi

# --- missing or unexpected reused marker: refused, not guessed ---------------
# Treating it as "never deployed" could deploy a different build from the one
# the commit's git tag names.
setup 1.69.339 false "$REPO:commit-$SHA 1.69.352"
rm "$ws/.release-tag-reused"
if run; then
    fail "no .release-tag-reused: expected a refusal, got '$(cat "$ws/.release-tag")'"
elif grep -q "refusing to guess" "$ws/err"; then
    pass "no .release-tag-reused: refused"
else
    fail "no .release-tag-reused: refused, but stderr: $(cat "$ws/err")"
fi

setup 1.69.339 True "$REPO:commit-$SHA 1.69.352"
if run; then
    fail "unexpected .release-tag-reused: expected a refusal, got '$(cat "$ws/.release-tag")'"
elif grep -q "refusing to guess" "$ws/err"; then
    pass "unexpected .release-tag-reused ('True'): refused"
else
    fail "unexpected .release-tag-reused: refused, but stderr: $(cat "$ws/err")"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all resolve-deploy-tag tests passed"
