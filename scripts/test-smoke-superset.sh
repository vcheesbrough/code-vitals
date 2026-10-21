#!/bin/sh
# Tests for scripts/smoke-superset.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. Fake `curl` and `docker` first on PATH: curl answers /health with the
# body in $WORK_DIR/health and /login/authentik with "<code> <location>" from
# $WORK_DIR/login; docker's psql succeeds unless the query is listed in
# $WORK_DIR/denied. The smoke must pass on a healthy stack and fail on each
# kind of breakage.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/smoke-superset.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/curl" <<EOF
#!/bin/sh
for last; do :; done
case "\$last" in
  */health) cat "$WORK_DIR/health" ;;
  */login/authentik) cat "$WORK_DIR/login" ;;
esac
EOF
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$* PGPASSWORD=\${PGPASSWORD:-}" >> "$WORK_DIR/docker-calls"
for last; do :; done
grep -qxF "\$last" "$WORK_DIR/denied" && { echo "ERROR:  permission denied for table repo" >&2; exit 1; }
grep -qxF "\$last" "$WORK_DIR/missing" 2>/dev/null && { echo "ERROR:  relation \"repo\" does not exist" >&2; exit 1; }
echo 3
EOF
chmod +x "$WORK_DIR/bin/curl" "$WORK_DIR/bin/docker"

GOOD_LOGIN="302 https://auth.desync.link/application/o/authorize/?response_type=code&client_id=code-vitals-dev&redirect_uri=https%3A%2F%2Fcode-vitals-dev.desync.link%2Foauth-authorized%2Fauthentik&scope=openid+email+profile&state=x"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# setup <health body> <login answer> [denied queries...]
setup() {
    printf '%s' "$1" > "$WORK_DIR/health"
    printf '%s' "$2" > "$WORK_DIR/login"
    shift 2
    : > "$WORK_DIR/denied"
    for q in "$@"; do echo "$q" >> "$WORK_DIR/denied"; done
    : > "$WORK_DIR/docker-calls"
}

run() {
    PATH="$WORK_DIR/bin:$PATH" SMOKE_ATTEMPTS=2 SMOKE_DELAY=0 CODE_VITALS_READER_PASSWORD=rpw \
        sh "$UNDER_TEST" "${1:-dev}" > "$WORK_DIR/out" 2>&1
}

setup OK "$GOOD_LOGIN" "SELECT 1 FROM repo LIMIT 1"
if run && grep -q "smoke checks passed" "$WORK_DIR/out"; then
    pass "healthy stack passes"
else
    fail "healthy stack: $(cat "$WORK_DIR/out")"
fi
if grep -q "^exec -e PGPASSWORD code-vitals-dev-postgres-1 psql .* -U code_vitals_reader .* PGPASSWORD=rpw$" "$WORK_DIR/docker-calls" \
    && ! grep -q "^exec.* rpw " "$WORK_DIR/docker-calls"; then
    pass "reader password passed by environment, not argument"
else
    fail "reader password: $(cat "$WORK_DIR/docker-calls")"
fi

setup "Service Unavailable" "$GOOD_LOGIN" "SELECT 1 FROM repo LIMIT 1"
if run; then fail "unhealthy: expected failure"; elif grep -q "did not answer OK" "$WORK_DIR/out"; then pass "unhealthy superset fails the smoke"; else fail "unhealthy: $(cat "$WORK_DIR/out")"; fi

setup OK "200 " "SELECT 1 FROM repo LIMIT 1"
if run; then fail "no redirect: expected failure"; elif grep -q "expected a 302" "$WORK_DIR/out"; then pass "login without a redirect fails"; else fail "no redirect: $(cat "$WORK_DIR/out")"; fi

setup OK "302 https://auth.desync.link/application/o/authorize/?client_id=code-vitals&redirect_uri=https%3A%2F%2Fcode-vitals-dev.desync.link%2Foauth-authorized%2Fauthentik" "SELECT 1 FROM repo LIMIT 1"
if run; then fail "prod client on dev: expected failure"; elif grep -q "lacks client_id=code-vitals-dev" "$WORK_DIR/out"; then pass "the other environment's client_id fails"; else fail "client_id: $(cat "$WORK_DIR/out")"; fi

setup OK "302 https://auth.desync.link/application/o/authorize/?client_id=code-vitals-dev&redirect_uri=http%3A%2F%2Fcode-vitals-dev.desync.link%2Foauth-authorized%2Fauthentik" "SELECT 1 FROM repo LIMIT 1"
if run; then fail "http redirect_uri: expected failure"; elif grep -q "lacks redirect_uri" "$WORK_DIR/out"; then pass "a non-https redirect_uri fails"; else fail "redirect_uri: $(cat "$WORK_DIR/out")"; fi

setup OK "302 https://evil.example/authorize/?client_id=code-vitals-dev" "SELECT 1 FROM repo LIMIT 1"
if run; then fail "foreign authorize: expected failure"; elif grep -q "expected a 302 to Authentik" "$WORK_DIR/out"; then pass "a redirect elsewhere fails"; else fail "foreign: $(cat "$WORK_DIR/out")"; fi

setup OK "$GOOD_LOGIN"
if run; then fail "reader reads base table: expected failure"; elif grep -q "can read a base table" "$WORK_DIR/out"; then pass "a reader that can read base tables fails"; else fail "base table: $(cat "$WORK_DIR/out")"; fi

setup OK "$GOOD_LOGIN" "SELECT count(*) FROM freshness" "SELECT 1 FROM repo LIMIT 1"
if run; then fail "reader cannot read view: expected failure"; elif grep -q "cannot read the freshness view" "$WORK_DIR/out"; then pass "a reader that cannot read the view fails"; else fail "view: $(cat "$WORK_DIR/out")"; fi

setup OK "$GOOD_LOGIN"
echo "SELECT 1 FROM repo LIMIT 1" > "$WORK_DIR/missing"
if run; then fail "missing table: expected failure"; elif grep -q "not with a permission denial" "$WORK_DIR/out"; then pass "a base-table error other than a permission denial fails"; else fail "missing table: $(cat "$WORK_DIR/out")"; fi
rm -f "$WORK_DIR/missing"

if PATH="$WORK_DIR/bin:$PATH" sh "$UNDER_TEST" staging > /dev/null 2>&1; then fail "unknown env: expected refusal"; else pass "unknown environment refused"; fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all smoke-superset tests passed"
