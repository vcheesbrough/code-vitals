#!/bin/sh
# Run a command with a throwaway Postgres for the database tests. Ported from
# bored's scripts/with-test-postgres.sh (bored card #471).
#
# Usage: with-test-postgres.sh <command> [args...]
#   CI:    with-test-postgres.sh docker build --secret id=database_url,env=CODE_VITALS_TEST_DATABASE_URL ...
#   local: with-test-postgres.sh uv run pytest
#
# Starts a sibling postgres container on the same docker daemon, waits until it
# accepts connections, exports CODE_VITALS_TEST_DATABASE_URL pointing at it,
# runs the command, and removes the container however the command ends. The
# tests create one database per test (tests/conftest.py), so nothing needs
# cleaning between runs — the container is the cleanup.
#
# The URL uses the container's bridge IP, which both the host and a BuildKit
# `RUN` step can reach. The password is a fixed test value: the container is
# unpublished and lives only for this command.

set -eu

[ "$#" -gt 0 ] || { echo "usage: $0 <command> [args...]" >&2; exit 2; }

# Same image, same digest as production (deploy/compose.yaml).
IMAGE=postgres:16-alpine@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229
PASSWORD=code-vitals-ci-test
NAME="code-vitals-test-postgres-$$-$(date +%s)"

# A cancelled CI run never reaches its trap; reap leftovers older than an hour
# (by label, so no other container is touched) before starting a new one.
for stale in $(docker ps -aq --filter label=code-vitals.test-postgres=1 \
                 --filter "until=1h" 2>/dev/null || true); do
  docker rm -f "$stale" > /dev/null 2>&1 || true
done

# Durability is worthless for a throwaway test database, and switching it off
# makes the suite's many CREATE DATABASEs fast.
docker run -d --rm --name "$NAME" --label code-vitals.test-postgres=1 \
  -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_USER=code_vitals -e POSTGRES_DB=code_vitals \
  "$IMAGE" -c fsync=off -c synchronous_commit=off -c full_page_writes=off > /dev/null
# shellcheck disable=SC2064  # expand NAME now, not at trap time
trap "docker rm -f '$NAME' > /dev/null 2>&1 || true" EXIT INT TERM

ready=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -h 127.0.0.1 -U code_vitals -d code_vitals > /dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = 1 ] || { echo "ERROR: test postgres did not become ready" >&2; exit 1; }

IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NAME")
[ -n "$IP" ] || { echo "ERROR: test postgres has no bridge IP" >&2; exit 1; }
export CODE_VITALS_TEST_DATABASE_URL="postgresql://code_vitals:$PASSWORD@$IP:5432/code_vitals"
echo "test postgres ready at $IP"

# Not `exec`: the trap must still run to remove the container.
"$@"
