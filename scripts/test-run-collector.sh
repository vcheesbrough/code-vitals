#!/bin/sh
# Tests for scripts/run-collector.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH records every call and the environment
# each container would be given. Its containers: `list-repos` prints
# $WORK_DIR/list; `backfill-repo --repo R` exits with $WORK_DIR/exit.R when it
# exists, as `docker start --attach` passes on the container's status.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/run-collector.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin" "$WORK_DIR/repo"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
case "\$1" in
  create)
    echo "url=\$CODE_VITALS_DATABASE_URL otel=\$OTEL_EXPORTER_OTLP_ENDPOINT attrs=\$OTEL_RESOURCE_ATTRIBUTES pipeline=\$CI_PIPELINE_NUMBER" >> "$WORK_DIR/create-env"
    case "\$*" in
      *" list-repos") echo cid-list ;;
      *" check-status"*) echo cid-status ;;
      *" backfill-repo --repo "*) args="\$*"; repo=\${args#* backfill-repo --repo }; echo "cid-\${repo%% *}" ;;
      *) echo cid-other ;;
    esac
    ;;
  start)
    cid=\$3
    case "\$cid" in
      cid-list) cat "$WORK_DIR/list" ;;
      cid-status) echo "check-status ran" ;;
      cid-*)
        repo=\${cid#cid-}
        echo "backfilling \$repo"
        if [ -f "$WORK_DIR/exit.\$repo" ]; then exit "\$(cat "$WORK_DIR/exit.\$repo")"; fi
        ;;
    esac
    ;;
esac
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

IMAGE=registry.example/code-vitals-collector@sha256:abc

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

# run <args…> — fresh call log, in a scratch repo root (where .collected-repos
# is written); returns the script's status.
run() {
    : > "$WORK_DIR/docker-calls"
    : > "$WORK_DIR/create-env"
    (cd "$WORK_DIR/repo" && env PATH="$WORK_DIR/bin:$PATH" CODE_VITALS_WRITER_PASSWORD=wpw0123 \
        CI_PIPELINE_NUMBER=42 sh "$UNDER_TEST" "$@") > "$WORK_DIR/out" 2>&1
}

calls() { cat "$WORK_DIR/docker-calls"; }
out() { cat "$WORK_DIR/out"; }
collected() { cat "$WORK_DIR/repo/.collected-repos"; }

cat > "$WORK_DIR/list" <<'EOF'
code-vitals: telemetry off (no OTEL_* variables set)
repo bored https://github.com/vcheesbrough/bored.git main
repo v-note https://github.com/vcheesbrough/v-note.git master
repo newcomer https://github.com/example/newcomer.git trunk
EOF

# --- repos: every registered repo backfilled, in its own container ------------
echo 3 > "$WORK_DIR/exit.v-note"  # v-note has no plugin step: skipped
if run dev "$IMAGE" repos \
    && calls | grep -q "^create -e CODE_VITALS_DATABASE_URL .* --network code-vitals-dev_default $IMAGE list-repos$" \
    && calls | grep -q "^create .* --network code-vitals-dev_default $IMAGE backfill-repo --repo bored --url https://github.com/vcheesbrough/bored.git --trunk main$" \
    && calls | grep -q "^create .* $IMAGE backfill-repo --repo v-note --url https://github.com/vcheesbrough/v-note.git --trunk master$" \
    && calls | grep -q "^create .* $IMAGE backfill-repo --repo newcomer --url https://github.com/example/newcomer.git --trunk trunk$"; then
    pass "repos: lists the registered repos, then backfills each in its own container"
else
    fail "repos: $(out) / calls: $(calls)"
fi

if [ "$(collected)" = "$(printf 'bored\nnewcomer')" ] \
    && out | grep -q "v-note: skipped (no code-vitals plugin step)" \
    && out | grep -q "^bored: backfilling bored$" \
    && out | grep -q "3 repo(s): 2 collected, 0 failed"; then
    pass "repos: a repo without the plugin step is skipped, the rest are the collected list"
else
    fail "skip: $(out) / collected: $(collected)"
fi

for repo in list bored v-note newcomer; do
    connect=$(grep -n "^network connect proxy-backend cid-$repo$" "$WORK_DIR/docker-calls" | cut -d: -f1)
    start=$(grep -n "^start --attach cid-$repo$" "$WORK_DIR/docker-calls" | cut -d: -f1)
    remove=$(grep -n "^rm -f cid-$repo$" "$WORK_DIR/docker-calls" | cut -d: -f1)
    if [ -n "$connect" ] && [ -n "$start" ] && [ -n "$remove" ] \
        && [ "$connect" -lt "$start" ] && [ "$start" -lt "$remove" ]; then
        pass "repos: $repo joins proxy-backend before it starts, removed afterwards"
    else
        fail "$repo order: connect=$connect start=$start rm=$remove / calls: $(calls)"
    fi
done

if [ "$(sort -u "$WORK_DIR/create-env")" = "url=postgresql://code_vitals_writer:wpw0123@code-vitals-dev-db:5432/code_vitals otel=http://monitor-alloy:4318 attrs=deployment.environment.name=dev pipeline=42" ] \
    && ! calls | grep -q "wpw0123"; then
    pass "repos: the writer's URL and the environment's telemetry reach every container by environment only"
else
    fail "env: $(cat "$WORK_DIR/create-env") / calls: $(calls)"
fi

# --- status: check-status over exactly the repos collected --------------------
if run prod "$IMAGE" status \
    && calls | grep -q "^create .* --network code-vitals_default $IMAGE check-status --repo bored --repo newcomer$" \
    && grep -q "attrs=deployment.environment.name=prod" "$WORK_DIR/create-env"; then
    pass "status: check-status over the collected repos, as the prod writer"
else
    fail "status: $(out) / calls: $(calls)"
fi

# --- a failing repo fails the step, after every repo ran ----------------------
echo 1 > "$WORK_DIR/exit.bored"
if run prod "$IMAGE" repos; then
    fail "failing repo: expected the step to fail"
elif out | grep -q "bored: FAILED (exit 1)" && calls | grep -q "^start --attach cid-newcomer$" \
    && calls | grep -q "^rm -f cid-bored$" && collected | grep -qx bored; then
    pass "failing repo: the step fails, the other repos still run, and every container is removed"
else
    fail "failing repo: $(out) / calls: $(calls)"
fi
rm -f "$WORK_DIR/exit.bored" "$WORK_DIR/exit.v-note"

# --- no registered repo: nothing to backfill, an empty list -------------------
printf 'code-vitals: telemetry off\n' > "$WORK_DIR/list"
if run dev "$IMAGE" repos && [ ! -s "$WORK_DIR/repo/.collected-repos" ] \
    && run dev "$IMAGE" status && calls | grep -q " $IMAGE check-status$"; then
    pass "no repos: an empty collected list, and check-status checks the measures only"
else
    fail "no repos: $(out) / calls: $(calls)"
fi

# --- a malformed repo line is refused -----------------------------------------
printf 'repo b$(x) https://example/x.git main\n' > "$WORK_DIR/list"
if run dev "$IMAGE" repos; then
    fail "malformed line: expected a refusal"
elif out | grep -q "unexpected repo line" && ! calls | grep -q backfill-repo; then
    pass "malformed line: refused before any backfill"
else
    fail "malformed line: $(out) / calls: $(calls)"
fi

# --- refusals ----------------------------------------------------------------
rm -f "$WORK_DIR/repo/.collected-repos"
for args in "staging $IMAGE repos" "dev $IMAGE repo bored" "dev $IMAGE frobnicate" "dev $IMAGE status"; do
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
if (cd "$WORK_DIR/repo" && env -u CODE_VITALS_WRITER_PASSWORD PATH="$WORK_DIR/bin:$PATH" \
    sh "$UNDER_TEST" dev "$IMAGE" repos) > "$WORK_DIR/out" 2>&1; then
    fail "missing secret: expected a refusal"
elif grep -q "CODE_VITALS_WRITER_PASSWORD is required" "$WORK_DIR/out" && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "missing secret: refused before touching docker"
else
    fail "missing secret: $(out) / calls: $(calls)"
fi

: > "$WORK_DIR/docker-calls"
if (cd "$WORK_DIR/repo" && PATH="$WORK_DIR/bin:$PATH" CODE_VITALS_WRITER_PASSWORD='p@ss/word' \
    sh "$UNDER_TEST" dev "$IMAGE" repos) > "$WORK_DIR/out" 2>&1; then
    fail "URL-unsafe password: expected a refusal"
elif grep -q "may contain only" "$WORK_DIR/out" && [ ! -s "$WORK_DIR/docker-calls" ]; then
    pass "URL-unsafe password: refused before touching docker"
else
    fail "URL-unsafe password: $(out)"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures test(s) failed" >&2
    exit 1
fi
echo "all run-collector tests passed"
