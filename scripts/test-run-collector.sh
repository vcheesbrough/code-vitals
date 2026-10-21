#!/bin/sh
# Tests for scripts/run-collector.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH records every call and the environment
# the container would be given; `start` exits with $WORK_DIR/exit-code when it
# exists, as `docker start --attach` passes on the container's status.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/run-collector.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
# One line per call: the container's script argument spans lines.
echo "\$*" | tr '\n' ' ' | sed 's/ *\$//' >> "$WORK_DIR/docker-calls"
echo >> "$WORK_DIR/docker-calls"
if [ "\$1" = create ]; then
    echo "url=\$CODE_VITALS_DATABASE_URL otel=\$OTEL_EXPORTER_OTLP_ENDPOINT attrs=\$OTEL_RESOURCE_ATTRIBUTES pipeline=\$CI_PIPELINE_NUMBER" >> "$WORK_DIR/create-env"
    echo cid123
fi
if [ "\$1" = start ] && [ -f "$WORK_DIR/exit-code" ]; then
    exit "\$(cat "$WORK_DIR/exit-code")"
fi
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

IMAGE=registry.example/code-vitals-collector@sha256:abc

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# run <args…> — fresh call log; returns the script's status.
run() {
    : > "$WORK_DIR/docker-calls"
    : > "$WORK_DIR/create-env"
    (cd "$SCRIPT_DIR/.." && env PATH="$WORK_DIR/bin:$PATH" CODE_VITALS_WRITER_PASSWORD=wpw0123 \
        CI_PIPELINE_NUMBER=42 sh "$UNDER_TEST" "$@") > "$WORK_DIR/out" 2>&1
}

calls() { cat "$WORK_DIR/docker-calls"; }
line_of() { grep -n -- "$1" "$WORK_DIR/docker-calls" | head -n 1 | cut -d: -f1 || true; }

# --- repo: clone, collect as backfill, check-repo, on dev's network ----------
if run dev "$IMAGE" repo bored \
    && calls | grep -q "^create -e CODE_VITALS_DATABASE_URL .* --network code-vitals-dev_default --entrypoint sh $IMAGE -c .*git clone --quiet .*code-vitals collect --repo \"\$2\" --clone /tmp/clone --trigger backfill.*code-vitals check-repo --repo \"\$2\" --clone /tmp/clone.* sh https://github.com/vcheesbrough/bored.git bored$"; then
    pass "repo: clones, collects with trigger=backfill, then check-repo, in one container"
else
    fail "repo: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

create=$(line_of "^create ")
connect=$(line_of "^network connect proxy-backend cid123$")
start=$(line_of "^start --attach cid123$")
remove=$(line_of "^rm -f cid123$")
if [ -n "$create" ] && [ -n "$connect" ] && [ -n "$start" ] && [ -n "$remove" ] \
    && [ "$create" -lt "$connect" ] && [ "$connect" -lt "$start" ] && [ "$start" -lt "$remove" ]; then
    pass "repo: joins proxy-backend for telemetry before it starts, removed afterwards"
else
    fail "order: create=$create connect=$connect start=$start rm=$remove / calls: $(calls)"
fi

if grep -qx "url=postgresql://code_vitals_writer:wpw0123@code-vitals-dev-db:5432/code_vitals otel=http://monitor-alloy:4318 attrs=deployment.environment.name=dev pipeline=42" "$WORK_DIR/create-env" \
    && ! calls | grep -q "wpw0123"; then
    pass "repo: the writer's URL and the environment's telemetry reach the container by environment only"
else
    fail "env: $(cat "$WORK_DIR/create-env") / calls: $(calls)"
fi

# --- status: check-status on prod's network ----------------------------------
if run prod "$IMAGE" status \
    && calls | grep -q "^create .* --network code-vitals_default $IMAGE check-status$" \
    && grep -q "attrs=deployment.environment.name=prod" "$WORK_DIR/create-env"; then
    pass "status: check-status as the prod writer"
else
    fail "status: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

# --- a failing container fails the step, and is still removed ----------------
echo 1 > "$WORK_DIR/exit-code"
if run prod "$IMAGE" repo v-note; then
    fail "failing check: expected the step to fail"
elif calls | grep -q "^rm -f cid123$"; then
    pass "failing check: the step fails and the container is removed"
else
    fail "failing check: container left behind / calls: $(calls)"
fi
rm -f "$WORK_DIR/exit-code"

# --- refusals ----------------------------------------------------------------
for args in "staging $IMAGE status" "dev $IMAGE repo other" "dev $IMAGE frobnicate"; do
    # shellcheck disable=SC2086  # args is a list of words
    if run $args; then
        fail "'$args': expected a refusal"
    elif [ ! -s "$WORK_DIR/docker-calls" ]; then
        pass "'$args': refused before touching docker"
    else
        fail "'$args': docker was called: $(calls)"
    fi
done

: > "$WORK_DIR/docker-calls"
if (cd "$SCRIPT_DIR/.." && env -u CODE_VITALS_WRITER_PASSWORD PATH="$WORK_DIR/bin:$PATH" \
    sh "$UNDER_TEST" dev "$IMAGE" status) > "$WORK_DIR/out" 2>&1; then
    fail "missing secret: expected a refusal"
elif grep -q "CODE_VITALS_WRITER_PASSWORD is required" "$WORK_DIR/out" && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "missing secret: refused before touching docker"
else
    fail "missing secret: $(cat "$WORK_DIR/out") / calls: $(calls)"
fi

: > "$WORK_DIR/docker-calls"
if (cd "$SCRIPT_DIR/.." && PATH="$WORK_DIR/bin:$PATH" CODE_VITALS_WRITER_PASSWORD='p@ss/word' \
    sh "$UNDER_TEST" dev "$IMAGE" status) > "$WORK_DIR/out" 2>&1; then
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
echo "all run-collector tests passed"
