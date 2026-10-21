#!/bin/sh
# Tests for scripts/with-test-postgres.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH plays the daemon: `ps` lists the
# containers in $WORK_DIR/containers ("<id> <started-epoch>" per line),
# `inspect` answers their start label (or the new container's bridge IP),
# `exec` reports ready, and every call is recorded. Like the real daemon it
# rejects an `until` filter on `ps`.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/with-test-postgres.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
case "\$1" in
  ps)
    case "\$*" in *until=*) echo "Error response from daemon: invalid filter 'until'" >&2; exit 1 ;; esac
    cut -d' ' -f1 "$WORK_DIR/containers"
    ;;
  inspect)
    for last; do :; done
    case "\$*" in
      *IPAddress*) echo 172.17.0.9 ;;
      *) grep "^\$last " "$WORK_DIR/containers" | cut -d' ' -f2 ;;
    esac
    ;;
esac
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

now=$(date +%s)
printf '%s\n' "old $((now - 7200))" "young $((now - 60))" "nolabel " > "$WORK_DIR/containers"
: > "$WORK_DIR/docker-calls"

if PATH="$WORK_DIR/bin:$PATH" sh "$UNDER_TEST" sh -c 'echo "$CODE_VITALS_TEST_DATABASE_URL" > "$0/url"' "$WORK_DIR" \
    > "$WORK_DIR/out" 2>&1; then
    pass "runs the command"
else
    fail "runs the command: $(cat "$WORK_DIR/out")"
fi

if grep -q "^rm -f old$" "$WORK_DIR/docker-calls" \
    && ! grep -q "^rm -f young$" "$WORK_DIR/docker-calls" \
    && ! grep -q "^rm -f nolabel$" "$WORK_DIR/docker-calls"; then
    pass "reaps only labelled containers started over an hour ago"
else
    fail "reaper: calls: $(cat "$WORK_DIR/docker-calls")"
fi

if ! grep -q "until=" "$WORK_DIR/docker-calls"; then
    pass "never asks docker ps for an until filter"
else
    fail "until filter used: $(cat "$WORK_DIR/docker-calls")"
fi

if grep -q "^run -d --rm --name code-vitals-test-postgres-.* --label code-vitals.test-postgres=1 --label code-vitals.test-postgres.started=[0-9]" "$WORK_DIR/docker-calls"; then
    pass "the new container carries its start time as a label"
else
    fail "start label: $(cat "$WORK_DIR/docker-calls")"
fi

if [ "$(cat "$WORK_DIR/url" 2>/dev/null)" = "postgresql://code_vitals:code-vitals-ci-test@172.17.0.9:5432/code_vitals" ]; then
    pass "exports the throwaway server's URL to the command"
else
    fail "url: '$(cat "$WORK_DIR/url" 2>/dev/null)'"
fi

if tail -n 1 "$WORK_DIR/docker-calls" | grep -q "^rm -f code-vitals-test-postgres-"; then
    pass "removes its own container afterwards"
else
    fail "cleanup: $(tail -n 1 "$WORK_DIR/docker-calls")"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all with-test-postgres tests passed"
