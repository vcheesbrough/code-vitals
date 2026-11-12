#!/bin/sh
# Tests for scripts/check-superset-import.sh.
#
# Plain POSIX sh with no harness dependency, run by the Dockerfile's test
# stage. A fake `docker` first on PATH records every call; FAKE_MIGRATE_EXIT
# and FAKE_START_EXIT set the exit status of the collector's `migrate` and of
# `start -a` (the import test). The gate rests on these: a failing migrate or
# import test fails the script, and the test container is removed either way.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNDER_TEST="$SCRIPT_DIR/check-superset-import.sh"

WORK_DIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK_DIR now, not at trap time
trap "rm -rf '$WORK_DIR'" EXIT

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$WORK_DIR/docker-calls"
case "\$1" in
  run) case "\$*" in *" migrate") exit "\${FAKE_MIGRATE_EXIT:-0}" ;; esac ;;
  start) exit "\${FAKE_START_EXIT:-0}" ;;
esac
exit 0
EOF
chmod +x "$WORK_DIR/bin/docker"

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1" >&2; failures=$((failures + 1)); }

URL=postgresql://code_vitals:pw@172.17.0.9:5432/code_vitals

# run <expected-exit> [env assignments...] -- [args...]: runs the script with a
# fresh call log; returns 0 if it exited as expected.
run() {
  expected=$1
  shift
  : > "$WORK_DIR/docker-calls"
  set +e
  env PATH="$WORK_DIR/bin:$PATH" "$@" > "$WORK_DIR/out" 2>&1
  status=$?
  set -e
  [ "$status" -eq "$expected" ]
}

if run 0 CODE_VITALS_TEST_DATABASE_URL="$URL" sh "$UNDER_TEST" superset:t collector:t; then
  pass "passes when migrate and the import test pass"
else
  fail "green run: $(cat "$WORK_DIR/out")"
fi
if grep -q "^run --rm -e CODE_VITALS_DATABASE_URL .* collector:t migrate$" "$WORK_DIR/docker-calls" \
  && grep -q "^create --name code-vitals-import-test-.* superset:t -u /tmp/dashboard_import_test.py$" "$WORK_DIR/docker-calls" \
  && grep -q "^cp .*/superset/dashboard_import_test.py code-vitals-import-test-.*:/tmp/dashboard_import_test.py$" "$WORK_DIR/docker-calls" \
  && grep -q "^start -a code-vitals-import-test-" "$WORK_DIR/docker-calls"; then
  pass "migrates with the collector, then runs the test in the Superset image"
else
  fail "calls: $(cat "$WORK_DIR/docker-calls")"
fi
if tail -n 1 "$WORK_DIR/docker-calls" | grep -q "^rm -f -v code-vitals-import-test-"; then
  pass "removes the test container after a pass"
else
  fail "cleanup after a pass: $(tail -n 1 "$WORK_DIR/docker-calls")"
fi

if run 1 CODE_VITALS_TEST_DATABASE_URL="$URL" FAKE_START_EXIT=1 sh "$UNDER_TEST" superset:t collector:t; then
  pass "fails when the import test fails"
else
  fail "failing test not reported: exit $status, $(cat "$WORK_DIR/out")"
fi
if tail -n 1 "$WORK_DIR/docker-calls" | grep -q "^rm -f -v code-vitals-import-test-"; then
  pass "removes the test container after a failure"
else
  fail "cleanup after a failure: $(tail -n 1 "$WORK_DIR/docker-calls")"
fi

if ! run 0 CODE_VITALS_TEST_DATABASE_URL="$URL" FAKE_MIGRATE_EXIT=1 sh "$UNDER_TEST" superset:t collector:t \
  && [ "$status" -ne 0 ] && ! grep -q "^create" "$WORK_DIR/docker-calls"; then
  pass "fails before running the test when migrate fails"
else
  fail "failing migrate: exit $status, calls: $(cat "$WORK_DIR/docker-calls")"
fi

if ! run 0 env -u CODE_VITALS_TEST_DATABASE_URL sh "$UNDER_TEST" superset:t collector:t \
  && [ ! -s "$WORK_DIR/docker-calls" ]; then
  pass "refuses to run without the test database"
else
  fail "ran without CODE_VITALS_TEST_DATABASE_URL: $(cat "$WORK_DIR/docker-calls")"
fi

if ! run 0 CODE_VITALS_TEST_DATABASE_URL="$URL" sh "$UNDER_TEST" superset:t \
  && [ ! -s "$WORK_DIR/docker-calls" ]; then
  pass "refuses to run without both images"
else
  fail "ran with one image: $(cat "$WORK_DIR/docker-calls")"
fi

if [ "$failures" -ne 0 ]; then
  echo "$failures test(s) failed" >&2
  exit 1
fi
echo "all check-superset-import tests passed"
