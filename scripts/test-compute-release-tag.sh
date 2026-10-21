#!/bin/sh
# Tests for scripts/compute-release-tag.sh (ported from bored).
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage so every image build checks it. Expected tags are literal strings, not
# recomputed from the fixture.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/compute-release-tag.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

failures=0

pass() {
    echo "ok   - $1"
}

fail() {
    echo "FAIL - $1" >&2
    failures=$((failures + 1))
}

# A manifest shaped like the real one: a tool table *before* [project] and a
# dependency group after it, both with their own `version =`, so only the
# [project] version may be read.
cat >"$WORK_DIR/pyproject.toml" <<'EOF'
[tool.example]
version = "9.9.9"

[project]
name = "code-vitals"
version = "0.1.0"
requires-python = "==3.13.*"

[tool.other]
version = "8.8.8"
EOF

# run <event> <number> [pyproject] — stdout to $WORK_DIR/out,
# stderr to $WORK_DIR/err; returns the script's exit status.
run() {
    CI_PIPELINE_EVENT="$1" CI_PIPELINE_NUMBER="$2" \
        sh "$UNDER_TEST" "${3:-$WORK_DIR/pyproject.toml}" >"$WORK_DIR/out" 2>"$WORK_DIR/err"
}

# expect_tag <name> <expected> <event> <number>
expect_tag() {
    name="$1"
    expected="$2"
    shift 2
    if run "$@" && [ "$(cat "$WORK_DIR/out")" = "$expected" ]; then
        pass "$name"
    else
        fail "$name: expected '$expected', got '$(cat "$WORK_DIR/out")' / $(cat "$WORK_DIR/err")"
    fi
}

# expect_refusal <name> <stderr-substring> <event> <number> [pyproject]
expect_refusal() {
    name="$1"
    needle="$2"
    shift 2
    if run "$@"; then
        fail "$name: expected a refusal, got '$(cat "$WORK_DIR/out")'"
    elif grep -q "$needle" "$WORK_DIR/err"; then
        pass "$name"
    else
        fail "$name: refused, but stderr lacks '$needle': $(cat "$WORK_DIR/err")"
    fi
}

expect_tag "push: patch is this pipeline's number" "0.1.339" push 339
expect_tag "manual: patch is this pipeline's number" "0.1.340" manual 340

expect_refusal "push with an empty number is refused" "not a pipeline number" push ""
expect_refusal "push with a non-numeric number is refused" "not a pipeline number" push "12a"
expect_refusal "push with a leading-zero number is refused" "not a pipeline number" push "012"
# Deployments find their build through the commit; numbering one here would
# name an image no pipeline built.
expect_refusal "deployment is refused" "unsupported CI_PIPELINE_EVENT" deployment 341
expect_refusal "unknown event is refused" "unsupported CI_PIPELINE_EVENT" pull_request 5

printf '[project]\nname = "code-vitals"\n' >"$WORK_DIR/no-version.toml"
expect_refusal "manifest without a project version is refused" "no MAJOR.MINOR.PATCH" push 5 "$WORK_DIR/no-version.toml"

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all compute-release-tag tests passed"
