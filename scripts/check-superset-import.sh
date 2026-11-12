#!/bin/sh
# Test the dashboard import against the real Superset (card #548): run
# superset/dashboard_import_test.py inside the code-vitals-superset image,
# against the throwaway Postgres scripts/with-test-postgres.sh starts:
#
#   1. the collector image migrates the server's `code_vitals` database (the
#      views the datasets read, and the reader's login), as a deploy does;
#   2. the test, copied into a container of the Superset image (it is not
#      shipped in it), creates Superset's metadata database, runs the deploy's
#      bootstrap and then imports bundle variants: alignment of the
#      connection, re-runs, replace-not-merge, rollback, stale-object removal
#      and `--check`.
#
# The push pipeline runs it on the images it is about to publish, so a broken
# import fails the push. Both images are needed, so it runs after the build
# rather than inside it as the collector's tests do.
#
# Usage: with-test-postgres.sh check-superset-import.sh <superset-image> <collector-image>
#
# Nothing here reads input: Woodpecker hands a step's script to the shell on
# stdin, and a command that read it would eat the rest of the step.

set -eu
exec < /dev/null

usage="usage: with-test-postgres.sh $0 <superset-image> <collector-image>"
superset="${1:?$usage}"
collector="${2:?$usage}"
: "${CODE_VITALS_TEST_DATABASE_URL:?run under scripts/with-test-postgres.sh}"
test_file="$(dirname "$0")/../superset/dashboard_import_test.py"

# Fixed test values: the server is unpublished and lives for this run only.
CODE_VITALS_DATABASE_URL="$CODE_VITALS_TEST_DATABASE_URL"
CODE_VITALS_WRITER_PASSWORD=writer-test-password
CODE_VITALS_READER_PASSWORD=reader-test-password
CODE_VITALS_TEST_READER_PASSWORD="$CODE_VITALS_READER_PASSWORD"
export CODE_VITALS_DATABASE_URL CODE_VITALS_WRITER_PASSWORD CODE_VITALS_READER_PASSWORD \
  CODE_VITALS_TEST_READER_PASSWORD

echo "==> migrate the test database ($collector)"
docker run --rm -e CODE_VITALS_DATABASE_URL -e CODE_VITALS_WRITER_PASSWORD \
  -e CODE_VITALS_READER_PASSWORD "$collector" migrate

echo "==> the dashboard import in $superset"
name="code-vitals-import-test-$$-$(date +%s)"
# shellcheck disable=SC2064  # expand name now, not at trap time
trap "docker rm -f -v '$name' > /dev/null 2>&1 || true" EXIT INT TERM
docker create --name "$name" -e CODE_VITALS_TEST_DATABASE_URL -e CODE_VITALS_TEST_READER_PASSWORD \
  --entrypoint python "$superset" -u /tmp/dashboard_import_test.py > /dev/null
docker cp "$test_file" "$name:/tmp/dashboard_import_test.py"
started=$(date +%s)
if ! docker start -a "$name"; then
  echo "ERROR: the dashboard import test failed" >&2
  exit 1
fi
echo "==> OK ($(($(date +%s) - started)) s)"
