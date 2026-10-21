#!/bin/sh
# Tests for scripts/deploy-stack.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH records every call; `ps` reports the
# prod container running only when $WORK_DIR/prod-running exists. The calls
# are checked in order, which is what the script's guarantees rest on.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/deploy-stack.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
if [ "\$1" = ps ] && [ -f "$WORK_DIR/prod-running" ]; then
    echo 0123abcd
fi
if [ "\$1" = run ]; then
    # The secrets must reach migrate through the environment, not the args.
    echo "url=\$CODE_VITALS_DATABASE_URL" >> "$WORK_DIR/run-env"
fi
if [ "\$1" = compose ]; then
    echo "compose env=\$CODE_VITALS_ENV host=\$SUPERSET_HOST image=\$SUPERSET_IMAGE reader=\${CODE_VITALS_READER_URI:-}" >> "$WORK_DIR/compose-env"
fi
if [ "\$1" = exec ] && [ "\$2" = -i ]; then
    cat > "$WORK_DIR/stdin-\$(echo "\$*" | md5sum | cut -c1-8)"
    echo "superset_pw=\${SUPERSET_NEW_PASSWORD:-}" >> "$WORK_DIR/exec-env"
fi
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

IMAGE=registry.example/code-vitals-collector@sha256:abc
SUPERSET=registry.example/code-vitals-superset@sha256:def
SECRETS="POSTGRES_PASSWORD=pgpw0123 CODE_VITALS_WRITER_PASSWORD=wpw CODE_VITALS_READER_PASSWORD=rpw SUPERSET_SECRET_KEY=sskey123 SUPERSET_DB_PASSWORD=sdbpw AUTHENTIK_CLIENT_SECRET=ocsecret9"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# run <env> — fresh call log; returns the script's status.
run() {
    : > "$WORK_DIR/docker-calls"
    : > "$WORK_DIR/run-env"
    : > "$WORK_DIR/compose-env"
    : > "$WORK_DIR/exec-env"
    # shellcheck disable=SC2086  # SECRETS is a list of NAME=value words
    (cd "$SCRIPT_DIR/.." && env PATH="$WORK_DIR/bin:$PATH" $SECRETS \
        sh "$UNDER_TEST" "$1" "$IMAGE" "$SUPERSET") > "$WORK_DIR/out" 2>&1
}

calls() { cat "$WORK_DIR/docker-calls"; }
# line_of <pattern> — the first call line matching, as a number (0 if none).
line_of() { grep -n -- "$1" "$WORK_DIR/docker-calls" | head -n 1 | cut -d: -f1 || true; }

# --- prod: up then migrate on the prod network, never a dump or a reset -------
if run prod \
    && calls | grep -q "^volume create code-vitals-prod-pg$" \
    && calls | grep -q "^compose -p code-vitals -f deploy/compose.yaml up -d --pull always --wait" \
    && calls | grep -q "^run --rm --network code-vitals_default -e CODE_VITALS_DATABASE_URL -e CODE_VITALS_WRITER_PASSWORD -e CODE_VITALS_READER_PASSWORD $IMAGE migrate$" \
    && ! calls | grep -q "DROP DATABASE\|pg_dump\|pg_restore"; then
    pass "prod: postgres up and migrate, no reset or restore"
else
    fail "prod: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

migrate=$(line_of "^run --rm --network code-vitals_default .* migrate$")
role=$(line_of "^exec -i -e SUPERSET_NEW_PASSWORD code-vitals-postgres-1 psql")
init=$(line_of "^compose -p code-vitals -f deploy/compose.yaml --profile init run --rm --pull always superset-init$")
up=$(line_of "^compose -p code-vitals -f deploy/compose.yaml up -d --pull always --wait --wait-timeout 300 superset$")
if [ -n "$migrate" ] && [ -n "$role" ] && [ -n "$init" ] && [ -n "$up" ] \
    && [ "$migrate" -lt "$role" ] && [ "$role" -lt "$init" ] && [ "$init" -lt "$up" ]; then
    pass "prod: superset role/db, bootstrap, then superset up, after migrate"
else
    fail "superset order: migrate=$migrate role=$role init=$init up=$up / calls: $(calls)"
fi

if grep -q "^compose env=prod host=code-vitals.desync.link image=$SUPERSET reader=postgresql+psycopg2://code_vitals_reader:rpw@postgres:5432/code_vitals$" "$WORK_DIR/compose-env" \
    && grep -qx "superset_pw=sdbpw" "$WORK_DIR/exec-env" \
    && grep -q "CREATE DATABASE superset OWNER superset" "$WORK_DIR"/stdin-* \
    && ! grep -q "sdbpw" "$WORK_DIR"/stdin-*; then
    pass "superset: environment names, reader URI and metadata password reach compose/psql by environment only"
else
    fail "superset env: $(cat "$WORK_DIR/compose-env") / $(cat "$WORK_DIR/exec-env")"
fi

if grep -qx "url=postgresql://code_vitals:pgpw0123@postgres:5432/code_vitals" "$WORK_DIR/run-env" \
    && ! calls | grep -q "pgpw0123\|wpw\|rpw\|sskey123\|sdbpw\|ocsecret9"; then
    pass "secrets reach migrate through its environment, never a command line"
else
    fail "secrets: env $(cat "$WORK_DIR/run-env") / calls: $(calls)"
fi

# --- dev with prod running: reset, dump prod, restore, then migrate ----------
touch "$WORK_DIR/prod-running"
if run dev; then
    reset=$(line_of "code-vitals-dev-postgres-1 psql .*DROP DATABASE IF EXISTS code_vitals WITH (FORCE)")
    dump=$(line_of "^exec code-vitals-postgres-1 pg_dump -U code_vitals -Fc code_vitals$")
    restore=$(line_of "^exec -i code-vitals-dev-postgres-1 pg_restore --no-owner --no-acl")
    migrate=$(line_of "^run --rm --network code-vitals-dev_default .* migrate$")
    if [ -n "$reset" ] && [ -n "$dump" ] && [ -n "$restore" ] && [ -n "$migrate" ] \
        && [ "$reset" -lt "$dump" ] && [ "$dump" -lt "$restore" ] && [ "$restore" -lt "$migrate" ]; then
        pass "dev with prod running: reset, dump prod, restore, then migrate"
    else
        fail "dev order: reset=$reset dump=$dump restore=$restore migrate=$migrate / calls: $(calls)"
    fi
else
    fail "dev with prod: $(cat "$WORK_DIR/out")"
fi

# --- dev with prod absent: still reset, so no previous dev rows survive ------
rm -f "$WORK_DIR/prod-running"
if run dev && calls | grep -q "DROP DATABASE IF EXISTS code_vitals" \
    && ! calls | grep -q "pg_dump" && grep -q "dev starts empty" "$WORK_DIR/out"; then
    pass "dev without prod: database reset, starts empty"
else
    fail "dev without prod: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

# --- refusals ----------------------------------------------------------------
: > "$WORK_DIR/docker-calls"
# shellcheck disable=SC2086
if (cd "$SCRIPT_DIR/.." && env PATH="$WORK_DIR/bin:$PATH" $SECRETS sh "$UNDER_TEST" prod "$IMAGE") > /dev/null 2>&1; then
    fail "missing superset image: expected a refusal"
elif [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "missing superset image: refused before touching docker"
else
    fail "missing superset image: docker was called: $(calls)"
fi

: > "$WORK_DIR/docker-calls"
if (cd "$SCRIPT_DIR/.." && PATH="$WORK_DIR/bin:$PATH" POSTGRES_PASSWORD=x \
    CODE_VITALS_WRITER_PASSWORD=y CODE_VITALS_READER_PASSWORD=z \
    sh "$UNDER_TEST" staging "$IMAGE" "$SUPERSET") > /dev/null 2>&1; then
    fail "unknown environment: expected a refusal"
elif [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "unknown environment: refused before touching docker"
else
    fail "unknown environment: docker was called: $(calls)"
fi

: > "$WORK_DIR/docker-calls"
if (cd "$SCRIPT_DIR/.." && PATH="$WORK_DIR/bin:$PATH" POSTGRES_PASSWORD=x \
    CODE_VITALS_WRITER_PASSWORD=y sh "$UNDER_TEST" prod "$IMAGE" "$SUPERSET") > "$WORK_DIR/out" 2>&1; then
    fail "missing secret: expected a refusal"
elif grep -q "CODE_VITALS_READER_PASSWORD is required" "$WORK_DIR/out" && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "missing secret: refused before touching docker"
else
    fail "missing secret: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

: > "$WORK_DIR/docker-calls"
if (cd "$SCRIPT_DIR/.." && PATH="$WORK_DIR/bin:$PATH" POSTGRES_PASSWORD='p@ss/word' \
    CODE_VITALS_WRITER_PASSWORD=y CODE_VITALS_READER_PASSWORD=z \
    SUPERSET_SECRET_KEY=a SUPERSET_DB_PASSWORD=b AUTHENTIK_CLIENT_SECRET=c \
    sh "$UNDER_TEST" prod "$IMAGE" "$SUPERSET") > "$WORK_DIR/out" 2>&1; then
    fail "URL-unsafe password: expected a refusal"
elif grep -q "may contain only" "$WORK_DIR/out" && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "URL-unsafe password: refused before touching docker"
else
    fail "URL-unsafe password: $(cat "$WORK_DIR/out")"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all deploy-stack tests passed"
