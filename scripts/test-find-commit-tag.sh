#!/bin/sh
# Tests for scripts/find-commit-tag.sh (ported from bored, bored card #461).
#
# Plain POSIX sh with no harness dependency, wired into the Dockerfile like the
# other script tests. A fake `git` first on PATH prints $WORK_DIR/ls-remote as
# the `git ls-remote --tags` listing and records its arguments. Expected
# values are literal strings.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/find-commit-tag.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/git" <<EOF
#!/bin/sh
echo "\$*" > "$WORK_DIR/git-args"
[ -f "$WORK_DIR/ls-remote-fails" ] && exit 128
cat "$WORK_DIR/ls-remote"
EOF
chmod +x "$WORK_DIR/bin/git"

SHA=0123456789abcdef0123456789abcdef01234567
OTHER=fedcba9876543210fedcba9876543210fedcba98
TAB="$(printf '\t')"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# run <listing lines...> — a fresh workspace in $ws, the script run there.
case_n=0
run() {
    case_n=$((case_n + 1))
    ws="$WORK_DIR/ws$case_n"
    mkdir -p "$ws"
    rm -f "$WORK_DIR/ls-remote-fails"
    : > "$WORK_DIR/ls-remote"
    for line in "$@"; do
        echo "$line" >> "$WORK_DIR/ls-remote"
    done
    (cd "$ws" && PATH="$WORK_DIR/bin:$PATH" CI_COMMIT_SHA="$SHA" CI_REPO=owner/code-vitals GITHUB_TOKEN=tok \
        sh "$UNDER_TEST" > "$ws/out" 2> "$ws/err")
}

# expect <name> <tag> <reused>
expect() {
    if [ "$(cat "$ws/.release-tag")" = "$2" ] && [ "$(cat "$ws/.release-tag-reused")" = "$3" ]; then
        pass "$1"
    else
        fail "$1: got tag '$(cat "$ws/.release-tag" 2>/dev/null)' reused '$(cat "$ws/.release-tag-reused" 2>/dev/null)' / $(cat "$ws/err")"
    fi
}

# --- annotated tag: matched on its peeled line -------------------------------
run "aaaa${TAB}refs/tags/1.69.347" "$SHA${TAB}refs/tags/1.69.347^{}" \
    "bbbb${TAB}refs/tags/1.69.0" "$OTHER${TAB}refs/tags/1.69.0^{}" || true
expect "annotated tag on the commit is found" "1.69.347" true

# --- lightweight tag ---------------------------------------------------------
run "$SHA${TAB}refs/tags/1.68.12" || true
expect "lightweight tag on the commit is found" "1.68.12" true

# --- untagged commit: never deployed, and no invented version logged --------
run "aaaa${TAB}refs/tags/1.69.1" "$OTHER${TAB}refs/tags/1.69.1^{}" || true
expect "untagged commit: no tag, not reused" "" false
if grep -q "1.69.2\|allocated" "$ws/out"; then
    fail "untagged commit: logged an invented version: $(cat "$ws/out")"
else
    pass "untagged commit: logs no invented version"
fi

# --- legacy and non-semver tags on the commit are ignored -------------------
run "$SHA${TAB}refs/tags/v1.23.4" "$SHA${TAB}refs/tags/nightly" || true
expect "legacy v-prefixed and non-semver tags are ignored" "" false

# --- the repo URL carries the token and repo --------------------------------
if [ "$(cat "$WORK_DIR/git-args")" = "ls-remote --tags https://x-access-token:tok@github.com/owner/code-vitals.git" ]; then
    pass "lists the tags of CI_REPO with the token"
else
    fail "git args: $(cat "$WORK_DIR/git-args")"
fi

# --- two release tags on one commit: refused --------------------------------
if run "$SHA${TAB}refs/tags/1.69.339" "$SHA${TAB}refs/tags/1.69.352"; then
    fail "two release tags: expected a refusal"
elif grep -q "several release tags" "$ws/err"; then
    pass "two release tags on one commit: refused"
else
    fail "two release tags: refused, but stderr: $(cat "$ws/err")"
fi

# --- ls-remote fails: refused, not read as "untagged" -----------------------
# Reading a failed listing as "never deployed" would deploy the commit's
# latest build even when its tag names another.
case_n=$((case_n + 1)); ws="$WORK_DIR/ws$case_n"; mkdir -p "$ws"; touch "$WORK_DIR/ls-remote-fails"
if (cd "$ws" && PATH="$WORK_DIR/bin:$PATH" CI_COMMIT_SHA="$SHA" CI_REPO=owner/code-vitals GITHUB_TOKEN=tok \
    sh "$UNDER_TEST" > "$ws/out" 2> "$ws/err"); then
    fail "ls-remote failure: expected a refusal"
elif grep -q "could not list git tags" "$ws/err" && [ ! -e "$ws/.release-tag-reused" ]; then
    pass "ls-remote failure: refused, nothing written"
else
    fail "ls-remote failure: stderr: $(cat "$ws/err")"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all find-commit-tag tests passed"
